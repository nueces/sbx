package config

import (
	"encoding/json"
	"net/netip"
	"testing"
)

func signed(t *testing.T, c Config) []byte {
	t.Helper()
	if err := c.normalize(); err != nil {
		t.Fatal(err)
	}
	var err error
	c.Digest, err = c.ComputeDigest()
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func validConfig() Config {
	return Config{
		Version: Version, VMID: "vm-1", Generation: "gen-1", Quotas: DefaultQuotas(),
		Rules: []Rule{{ID: "web", Protocol: "HTTP", Host: "Example.COM.", Ports: []uint16{443}}},
	}
}

func TestParseAndDefaultDeny(t *testing.T) {
	c, err := Parse(signed(t, validConfig()))
	if err != nil {
		t.Fatal(err)
	}
	if !c.Allows("http", "example.com", 443) || c.Allows("tcp", "example.com", 443) || c.Allows("http", "example.com", 80) {
		t.Fatal("policy mismatch")
	}
	empty := validConfig()
	empty.Rules = nil
	c, err = Parse(signed(t, empty))
	if err != nil || c.Allows("tcp", "example.com", 443) {
		t.Fatal("empty policy enabled egress")
	}
}

func TestParseRejectsUnsafeInput(t *testing.T) {
	cases := map[string][]byte{
		"unknown": []byte(`{"version":1,"vm_id":"v","generation":"g","digest":"x","rules":[],"quotas":{},"extra":1}`),
		"digest":  signed(t, validConfig()),
	}
	cases["digest"][len(cases["digest"])-2] ^= 1
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse(data); err == nil {
				t.Fatal("accepted unsafe config")
			}
		})
	}
	conflict := validConfig()
	conflict.Rules = append(conflict.Rules, Rule{ID: "other", Protocol: "http", Host: "example.com", Ports: []uint16{443}})
	if err := conflict.normalize(); err == nil {
		t.Fatal("accepted conflicting rules")
	}
}

func TestWildcardMatchesOneLabelAndExactWins(t *testing.T) {
	c := validConfig()
	c.Rules = []Rule{
		{ID: "wildcard", Protocol: "http", Host: "*.example.com", Ports: []uint16{443}},
		{ID: "exact", Protocol: "http", Host: "api.example.com", Ports: []uint16{443}},
	}
	parsed, err := Parse(signed(t, c))
	if err != nil {
		t.Fatal(err)
	}
	rule, ok := parsed.Match("http", "api.example.com", 443)
	if !ok || rule.ID != "exact" || parsed.Allows("http", "deep.api.example.com", 443) || parsed.Allows("http", "example.com", 443) {
		t.Fatal("wildcard widened or beat exact rule")
	}
}

func TestRejectsAmbiguousProtocolsAndWildcards(t *testing.T) {
	c := validConfig()
	c.Rules = append(c.Rules, Rule{ID: "passthrough", Protocol: "tls", Host: "example.com", Ports: []uint16{443}})
	if err := c.normalize(); err == nil {
		t.Fatal("accepted ambiguous protocol rules")
	}
	c = validConfig()
	c.Rules[0].Host = "*.com"
	if err := c.normalize(); err == nil {
		t.Fatal("accepted unsafe wildcard")
	}
	c.Rules[0] = Rule{ID: "passthrough", Protocol: "tls", Host: "*.example.com", Ports: []uint16{443}}
	if err := c.normalize(); err == nil {
		t.Fatal("accepted non-exact TLS passthrough host")
	}
	c.Rules[0].Host = "203.0.113.1"
	if err := c.normalize(); err == nil {
		t.Fatal("accepted IP literal TLS passthrough host")
	}
}

func TestPassthroughRejectsHTTPFeaturesAndSecretInjection(t *testing.T) {
	c := validConfig()
	c.Rules[0] = Rule{ID: "passthrough", Protocol: "tls", Host: "example.com", Ports: []uint16{443}, Methods: []string{"GET"}}
	if err := c.normalize(); err == nil {
		t.Fatal("accepted HTTP inspection on TLS passthrough")
	}
	for _, field := range []string{`"hooks":["inspect"]`, `"secrets":["token"]`} {
		data := []byte(`{"version":1,"vm_id":"vm","generation":"gen","rules":[{"id":"passthrough","protocol":"tls","host":"example.com","ports":[443],` + field + `}],"quotas":{"tcp_flows":512,"flows_per_destination":64,"dns_mappings":1024,"open_files":4096}}`)
		if _, err := Prepare(data); err == nil {
			t.Fatalf("accepted passthrough feature %s", field)
		}
	}
}

func TestCanonicalDigestIsDeterministic(t *testing.T) {
	a := validConfig()
	a.Rules[0].Ports = []uint16{443, 80}
	a.Rules[0].Methods = []string{"HEAD", "GET"}
	b := validConfig()
	b.Rules[0].Ports = []uint16{80, 443}
	b.Rules[0].Methods = []string{"GET", "HEAD"}
	if err := a.normalize(); err != nil {
		t.Fatal(err)
	}
	if err := b.normalize(); err != nil {
		t.Fatal(err)
	}
	ad, _ := a.ComputeDigest()
	bd, _ := b.ComputeDigest()
	if ad != bd {
		t.Fatalf("non-deterministic digest: %s != %s", ad, bd)
	}
}

func TestSpecialUseDestinationsRequireExplicitRanges(t *testing.T) {
	for _, value := range []string{
		"0.1.2.3", "100.64.0.1", "100.100.100.200", "100.127.255.255",
		"127.0.0.1", "169.254.169.254", "192.0.0.1", "192.88.99.1",
		"192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1", "240.0.0.1",
		"255.255.255.255", "::1", "fc00::1", "fe80::1", "64:ff9b::a9fe:a9fe",
		"100::1", "2001::1", "2001:db8::1", "2002:7f00:1::", "3fff::1",
		"::ffff:100.100.100.200",
	} {
		t.Run(value, func(t *testing.T) {
			ip := netip.MustParseAddr(value)
			if (Rule{}).AllowsIP(ip) {
				t.Fatal("special-use destination allowed without an exception")
			}
			prefix := netip.PrefixFrom(ip.Unmap(), ip.Unmap().BitLen()).String()
			if !(Rule{AllowRanges: []string{prefix}}).AllowsIP(ip) {
				t.Fatal("explicit destination exception was ignored")
			}
		})
	}
	if !(Rule{AllowRanges: []string{"100.64.0.0/10"}}).AllowsIP(netip.MustParseAddr("100.100.100.200")) {
		t.Fatal("explicit shared-address range was ignored")
	}
	for _, value := range []string{"8.8.8.8", "100.63.255.255", "100.128.0.1", "2606:4700:4700::1111"} {
		if !(Rule{}).AllowsIP(netip.MustParseAddr(value)) {
			t.Fatalf("public address denied: %s", value)
		}
	}
	if (Rule{}).AllowsIP(netip.Addr{}) {
		t.Fatal("invalid address allowed")
	}
}

func FuzzParse(f *testing.F) {
	f.Add([]byte(`{}`))
	f.Fuzz(func(t *testing.T, data []byte) { _, _ = Parse(data) })
}
