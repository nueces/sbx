package syntheticdns

import (
	"encoding/binary"
	"errors"
	"net/netip"
	"strings"
	"sync"
)

const MaxMappings = 1024

type Policy interface {
	AllowsHost(string) bool
}

type Resolver struct {
	policy Policy
	mu     sync.Mutex
	byName map[string]netip.Addr
	byIP   map[netip.Addr]string
	next   uint32
}

func New(policy Policy) *Resolver {
	return &Resolver{policy: policy, byName: make(map[string]netip.Addr), byIP: make(map[netip.Addr]string)}
}

func (r *Resolver) Host(ip netip.Addr) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	host, ok := r.byIP[ip.Unmap()]
	return host, ok
}

func (r *Resolver) Resolve(host string) (netip.Addr, error) {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if !r.policy.AllowsHost(host) {
		return netip.Addr{}, errors.New("destination denied")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if ip, ok := r.byName[host]; ok {
		return ip, nil
	}
	if len(r.byName) >= MaxMappings {
		// Refusal is safer than evicting a mapping that may still identify an active flow.
		return netip.Addr{}, errors.New("synthetic DNS capacity exhausted")
	}
	r.next++
	value := uint32(198)<<24 | uint32(18+(r.next>>16))<<16 | r.next&0xffff
	ip := netip.AddrFrom4([4]byte{byte(value >> 24), byte(value >> 16), byte(value >> 8), byte(value)})
	r.byName[host], r.byIP[ip] = ip, host
	return ip, nil
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
