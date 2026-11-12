package syntheticdns

import (
	"encoding/binary"
	"errors"
	"net/netip"
	"slices"
	"strings"
	"sync"
)

const MaxMappings = 1024

var ErrCapacity = errors.New("synthetic DNS capacity exhausted")

type Policy interface {
	AllowsHost(string) bool
}

type entry struct {
	host   string
	ip     netip.Addr
	active int
	used   uint64
}

type Mapping struct {
	Host string `json:"host"`
	IP   string `json:"ip"`
}

type Resolver struct {
	policy  Policy
	limit   int
	mu      sync.Mutex
	byName  map[string]*entry
	byIP    map[netip.Addr]*entry
	next    uint32
	clock   uint64
	journal *journalState
}

func New(policy Policy) *Resolver { return NewWithLimit(policy, MaxMappings) }

func NewWithLimit(policy Policy, limit int) *Resolver {
	if limit < 1 || limit > MaxMappings {
		panic("invalid synthetic DNS mapping limit")
	}
	return &Resolver{policy: policy, limit: limit, byName: make(map[string]*entry), byIP: make(map[netip.Addr]*entry)}
}

func (r *Resolver) Snapshot() []Mapping {
	r.mu.Lock()
	defer r.mu.Unlock()
	result := make([]Mapping, 0, len(r.byName))
	for _, item := range r.byName {
		result = append(result, Mapping{Host: item.host, IP: item.ip.String()})
	}
	slices.SortFunc(result, func(a, b Mapping) int { return strings.Compare(a.Host, b.Host) })
	return result
}

func (r *Resolver) Restore(mappings []Mapping) error {
	if len(mappings) > r.limit {
		return errors.New("too many synthetic DNS mappings")
	}
	byName := make(map[string]*entry, len(mappings))
	byIP := make(map[netip.Addr]*entry, len(mappings))
	pool := netip.MustParsePrefix("198.18.0.0/15")
	for index, mapping := range mappings {
		ip, err := netip.ParseAddr(mapping.IP)
		if err != nil || !pool.Contains(ip) || mapping.Host != strings.ToLower(strings.TrimSuffix(mapping.Host, ".")) || !r.policy.AllowsHost(mapping.Host) || byName[mapping.Host] != nil || byIP[ip] != nil {
			return errors.New("invalid synthetic DNS mapping")
		}
		item := &entry{host: mapping.Host, ip: ip, used: uint64(index + 1)}
		byName[mapping.Host], byIP[ip] = item, item
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.byName) != 0 {
		return errors.New("cannot restore over active synthetic DNS state")
	}
	r.byName, r.byIP, r.clock = byName, byIP, uint64(len(mappings))
	return nil
}

func (r *Resolver) Acquire(ip netip.Addr) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	item, ok := r.byIP[ip.Unmap()]
	if !ok {
		return "", false
	}
	item.active++
	r.clock++
	item.used = r.clock
	return item.host, true
}

func (r *Resolver) Release(ip netip.Addr) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if item := r.byIP[ip.Unmap()]; item != nil && item.active > 0 {
		item.active--
	}
}

func (r *Resolver) Resolve(host string) (netip.Addr, error) {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if !r.policy.AllowsHost(host) {
		return netip.Addr{}, errors.New("destination denied")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.journal != nil && r.journal.failed != nil {
		return netip.Addr{}, r.journal.failed
	}
	r.clock++
	if item := r.byName[host]; item != nil {
		item.used = r.clock
		return item.ip, nil
	}
	// Guest/application caches may outlive the advertised TTL: never rebind an issued IP.
	if len(r.byName) >= r.limit {
		return netip.Addr{}, ErrCapacity
	}
	var ip netip.Addr
	next := r.next
	for {
		next++
		value := uint32(198)<<24 | uint32(18+(next>>16))<<16 | next&0xffff
		ip = netip.AddrFrom4([4]byte{byte(value >> 24), byte(value >> 16), byte(value >> 8), byte(value)})
		if r.byIP[ip] == nil {
			break
		}
	}
	if err := r.persist(Mapping{Host: host, IP: ip.String()}); err != nil {
		return netip.Addr{}, err
	}
	r.next = next
	item := &entry{host: host, ip: ip, used: r.clock}
	r.byName[host], r.byIP[ip] = item, item
	return ip, nil
}

func (r *Resolver) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.byName)
}

func (r *Resolver) Reply(query []byte) []byte {
	name, qtype, questionEnd, err := parseQuery(query)
	if err != nil {
		return nil
	}
	response := append([]byte(nil), query[:questionEnd]...)
	response[2], response[3] = 0x81, 0x80
	binary.BigEndian.PutUint16(response[6:8], 0)
	binary.BigEndian.PutUint16(response[8:10], 0)
	binary.BigEndian.PutUint16(response[10:12], 0)
	if qtype != 1 {
		return response
	}
	ip, err := r.Resolve(name)
	if err != nil {
		response[3] = 0x85 // authoritative refusal
		if errors.Is(err, ErrPersistence) {
			response[3] = 0x82 // SERVFAIL; the worker also receives the fatal persistence error.
		}
		return response
	}
	binary.BigEndian.PutUint16(response[6:8], 1)
	answer := []byte{0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4}
	answer = append(answer, ip.AsSlice()...)
	return append(response, answer...)
}

func parseQuery(packet []byte) (string, uint16, int, error) {
	if len(packet) < 17 || len(packet) > 1232 || packet[2]&0x80 != 0 || binary.BigEndian.Uint16(packet[4:6]) != 1 || binary.BigEndian.Uint16(packet[6:8]) != 0 || binary.BigEndian.Uint16(packet[8:10]) != 0 || binary.BigEndian.Uint16(packet[10:12]) != 0 {
		return "", 0, 0, errors.New("invalid DNS header")
	}
	labels := make([]string, 0, 8)
	i := 12
	for {
		if i >= len(packet) {
			return "", 0, 0, errors.New("truncated DNS name")
		}
		n := int(packet[i])
		i++
		if n == 0 {
			break
		}
		if n > 63 || n&0xc0 != 0 || i+n > len(packet) || len(labels) >= 20 {
			return "", 0, 0, errors.New("invalid DNS label")
		}
		label := strings.ToLower(string(packet[i : i+n]))
		for _, ch := range label {
			if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '-') {
				return "", 0, 0, errors.New("invalid DNS name")
			}
		}
		labels = append(labels, label)
		i += n
	}
	if len(labels) == 0 || i+4 > len(packet) || binary.BigEndian.Uint16(packet[i+2:i+4]) != 1 || i+4 != len(packet) {
		return "", 0, 0, errors.New("invalid DNS question")
	}
	return strings.Join(labels, "."), binary.BigEndian.Uint16(packet[i : i+2]), i + 4, nil
}
