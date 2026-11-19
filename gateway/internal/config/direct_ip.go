package config

import (
	"net/netip"
	"regexp"
	"slices"
)

var numericHost = regexp.MustCompile(`^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$`)

var directIPDenied = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"), netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"), netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("224.0.0.0/4"),
	netip.MustParsePrefix("240.0.0.0/4"),
}

func (r Rule) AllowsDirectIP(ip netip.Addr) bool {
	if !ip.Is4() {
		return false
	}
	for _, prefix := range directIPDenied {
		if prefix.Contains(ip) {
			return false
		}
	}
	switch r.Protocol {
	case "tcp":
		return r.AllowsIP(ip)
	case "http":
		return !forbiddenIP(ip)
	default:
		return false
	}
}

func (c Config) DirectIPs() []netip.Addr {
	unique := make(map[netip.Addr]bool)
	for _, rule := range c.Rules {
		if ip, err := netip.ParseAddr(rule.Host); err == nil && ip.Is4() {
			unique[ip] = true
		}
	}
	result := make([]netip.Addr, 0, len(unique))
	for ip := range unique {
		result = append(result, ip)
	}
	slices.SortFunc(result, netip.Addr.Compare)
	return result
}
