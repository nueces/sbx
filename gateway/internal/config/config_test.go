package config

import (
	"encoding/json"
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

func FuzzParse(f *testing.F) {
	f.Add([]byte(`{}`))
	f.Fuzz(func(t *testing.T, data []byte) { _, _ = Parse(data) })
}
