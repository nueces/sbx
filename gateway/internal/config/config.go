package config

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"path"
	"slices"
	"strings"
)

const Version = 1

type Rule struct {
	ID          string   `json:"id"`
	Protocol    string   `json:"protocol"`
	Host        string   `json:"host"`
	Ports       []uint16 `json:"ports"`
	Methods     []string `json:"methods,omitempty"`
	Paths       []string `json:"paths,omitempty"`
	AllowRanges []string `json:"allow_ranges,omitempty"`
}

type Quotas struct {
	TCPFlows            int `json:"tcp_flows"`
	FlowsPerDestination int `json:"flows_per_destination"`
	DNSMappings         int `json:"dns_mappings"`
	OpenFiles           int `json:"open_files"`
}

type Config struct {
	Version    int    `json:"version"`
	VMID       string `json:"vm_id"`
	Generation string `json:"generation"`
	Digest     string `json:"digest,omitempty"`
	Rules      []Rule `json:"rules"`
	Quotas     Quotas `json:"quotas"`
}

func DefaultQuotas() Quotas {
	return Quotas{TCPFlows: 512, FlowsPerDestination: 64, DNSMappings: 1024, OpenFiles: 4096}
}

func decode(data []byte) (Config, error) {
	var c Config
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return c, fmt.Errorf("decode config: %w", err)
	}
	if err := dec.Decode(new(any)); !errors.Is(err, io.EOF) {
		return c, errors.New("decode config: trailing value")
	}
	return c, nil
}

func Prepare(data []byte) (Config, error) {
	c, err := decode(data)
	if err != nil {
		return c, err
	}
	c.Digest = ""
	if err := c.normalize(); err != nil {
		return c, err
	}
	c.Digest, err = c.ComputeDigest()
	return c, err
}

func Parse(data []byte) (Config, error) {
	input, err := decode(data)
	if err != nil {
		return input, err
	}
	prepared, err := Prepare(data)
	if err != nil {
		return input, err
	}
	if input.Digest == "" || !strings.EqualFold(input.Digest, prepared.Digest) {
		return input, errors.New("config digest mismatch")
	}
	return prepared, nil
}

func (c *Config) normalize() error {
	if c.Version != Version {
		return fmt.Errorf("unsupported config version %d", c.Version)
	}
	if !validID(c.VMID) || !validID(c.Generation) {
		return errors.New("invalid VM identity or generation")
	}
	if c.Quotas.TCPFlows < 1 || c.Quotas.TCPFlows > 512 || c.Quotas.FlowsPerDestination < 1 || c.Quotas.FlowsPerDestination > 64 || c.Quotas.DNSMappings < 1 || c.Quotas.DNSMappings > 1024 || c.Quotas.OpenFiles < 64 || c.Quotas.OpenFiles > 4096 {
		return errors.New("quota outside allowed range")
	}
	seen := make(map[string]string)
	for i := range c.Rules {
		r := &c.Rules[i]
		r.Protocol = strings.ToLower(strings.TrimSpace(r.Protocol))
		if !slices.Contains([]string{"http", "tcp", "tls", "udp"}, r.Protocol) {
			return fmt.Errorf("rule %q: invalid protocol", r.ID)
		}
		if !validID(r.ID) {
			return errors.New("invalid rule ID")
		}
		host, err := normalizeHost(r.Host)
		if err != nil {
			return fmt.Errorf("rule %q: %w", r.ID, err)
		}
		r.Host = host
		if len(r.Ports) == 0 {
			return fmt.Errorf("rule %q: no ports", r.ID)
		}
		slices.Sort(r.Ports)
		if slices.Contains(r.Ports, 0) || len(slices.Compact(r.Ports)) != len(r.Ports) {
			return fmt.Errorf("rule %q: invalid or duplicate port", r.ID)
		}
		if r.Protocol != "http" && (len(r.Methods) != 0 || len(r.Paths) != 0) {
			return fmt.Errorf("rule %q: request constraints require HTTP", r.ID)
		}
		for j := range r.Methods {
			r.Methods[j] = strings.ToUpper(strings.TrimSpace(r.Methods[j]))
			if !validToken(r.Methods[j]) {
				return fmt.Errorf("rule %q: invalid HTTP method", r.ID)
			}
		}
		slices.Sort(r.Methods)
		r.Methods = slices.Compact(r.Methods)
		for j := range r.Paths {
			raw := strings.TrimPrefix(r.Paths[j], "/")
			if slices.Contains(strings.Split(raw, "/"), "..") {
				return fmt.Errorf("rule %q: unsafe path", r.ID)
			}
			r.Paths[j] = path.Clean("/" + raw)
		}
		slices.Sort(r.Paths)
		r.Paths = slices.Compact(r.Paths)
		for j := range r.AllowRanges {
			prefix, err := netip.ParsePrefix(r.AllowRanges[j])
			if err != nil {
				return fmt.Errorf("rule %q: invalid allowed range", r.ID)
			}
			r.AllowRanges[j] = prefix.Masked().String()
		}
		slices.Sort(r.AllowRanges)
		if len(slices.Compact(r.AllowRanges)) != len(r.AllowRanges) {
			return fmt.Errorf("rule %q: duplicate allowed range", r.ID)
		}
		for _, port := range r.Ports {
			transport := r.Protocol
			if transport == "http" || transport == "tls" {
				transport = "tcp"
			}
			key := fmt.Sprintf("%s|%s|%d", transport, r.Host, port)
			if prior, ok := seen[key]; ok {
				return fmt.Errorf("rules %q and %q conflict", prior, r.ID)
			}
			seen[key] = r.ID
		}
	}
	slices.SortFunc(c.Rules, func(a, b Rule) int { return strings.Compare(a.ID, b.ID) })
	return nil
}

func (c Config) ComputeDigest() (string, error) {
	c.Digest = ""
	data, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

func (c Config) AllowsHost(host string) bool {
	host, err := normalizeHost(host)
	if err != nil {
		return false
	}
	for _, rule := range c.Rules {
		if hostMatches(rule.Host, host) {
			return true
		}
	}
	return false
}

func (c Config) Match(protocol, host string, port uint16) (Rule, bool) {
	host, err := normalizeHost(host)
	if err != nil {
		return Rule{}, false
	}
	protocol = strings.ToLower(protocol)
	for _, wildcard := range []bool{false, true} {
		for _, rule := range c.Rules {
			if strings.HasPrefix(rule.Host, "*.") != wildcard {
				continue
			}
			if rule.Protocol == protocol && hostMatches(rule.Host, host) && slices.Contains(rule.Ports, port) {
				return rule, true
			}
		}
	}
	return Rule{}, false
}

func (c Config) Allows(protocol, host string, port uint16) bool {
	_, ok := c.Match(protocol, host, port)
	return ok
}

func (r Rule) AllowsIP(ip netip.Addr) bool {
	for _, value := range r.AllowRanges {
		prefix, _ := netip.ParsePrefix(value)
		if prefix.Contains(ip.Unmap()) {
			return true
		}
	}
	return !forbiddenIP(ip)
}

func forbiddenIP(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() || ip.IsUnspecified() || ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() {
		return true
	}
	for _, value := range []string{"192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15"} {
		prefix := netip.MustParsePrefix(value)
		if prefix.Contains(ip) {
			return true
		}
	}
	return false
}

func normalizeHost(value string) (string, error) {
	host := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(value), "."))
	if addr, err := netip.ParseAddr(host); err == nil {
		return addr.Unmap().String(), nil
	}
	wildcard := strings.HasPrefix(host, "*.")
	if wildcard {
		host = strings.TrimPrefix(host, "*.")
	}
	if host == "" || strings.ContainsAny(host, "/:@") || strings.Contains(host, "..") {
		return "", errors.New("invalid host")
	}
	for _, label := range strings.Split(host, ".") {
		if label == "" || strings.HasPrefix(label, "-") || strings.HasSuffix(label, "-") {
			return "", errors.New("invalid host")
		}
		for _, ch := range label {
			if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '-') {
				return "", errors.New("invalid host")
			}
		}
	}
	if wildcard {
		if !strings.Contains(host, ".") {
			return "", errors.New("wildcard must retain a registrable suffix")
		}
		return "*." + host, nil
	}
	return host, nil
}

func hostMatches(rule, host string) bool {
	if !strings.HasPrefix(rule, "*.") {
		return rule == host
	}
	suffix := strings.TrimPrefix(rule, "*")
	return strings.HasSuffix(host, suffix) && len(strings.TrimSuffix(host, suffix)) > 0 && !strings.Contains(strings.TrimSuffix(host, suffix), ".")
}

func validToken(value string) bool {
	if value == "" {
		return false
	}
	for _, ch := range value {
		if ch <= 32 || ch >= 127 || strings.ContainsRune("()<>@,;:\\\"/[]?={}", ch) {
			return false
		}
	}
	return true
}

func validID(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for _, ch := range value {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '-' || ch == '_') {
			return false
		}
	}
	return true
}
