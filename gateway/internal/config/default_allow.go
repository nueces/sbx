package config

import "net/netip"

var defaultPrivate = []netip.Prefix{
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.168.0.0/16"),
}

// Known metadata/host-agent endpoints outside ranges already rejected by forbiddenIP
// (which covers CGNAT 100.64.0.0/10, including Alibaba metadata 100.100.100.200).
var defaultDenied = []netip.Prefix{
	netip.MustParsePrefix("168.63.129.16/32"), // Azure wireserver
}

// DefaultAllowsIP reports whether default (unmatched) forwarding may reach ip.
// Public unicast and IPv4 RFC1918 addresses are permitted; every forbiddenIP class,
// CGNAT, known metadata endpoints and all non-global IPv6 (including ULA) are not.
// Explicit-rule checks are separate and are not widened by this function.
func DefaultAllowsIP(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() {
		return false
	}
	for _, prefix := range defaultDenied {
		if prefix.Contains(ip) {
			return false
		}
	}
	if ip.Is4() {
		for _, prefix := range defaultPrivate {
			if prefix.Contains(ip) {
				return true
			}
		}
	}
	return !forbiddenIP(ip)
}
