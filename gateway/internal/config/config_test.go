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

func TestWildcardMatchesOneLabel(t *testing.T) {
	c := validConfig()
	c.Rules[0].Host = "*.example.com"
	parsed, err := Parse(signed(t, c))
	if err != nil {
		t.Fatal(err)
	}
	if !parsed.Allows("http", "api.example.com", 443) || parsed.Allows("http", "deep.api.example.com", 443) || parsed.Allows("http", "example.com", 443) {
		t.Fatal("wildcard widened unexpectedly")
	}
}

func FuzzParse(f *testing.F) {
	f.Add([]byte(`{}`))
	f.Fuzz(func(t *testing.T, data []byte) { _, _ = Parse(data) })
}
