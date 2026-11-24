package config

import (
	"encoding/json"
	"net/netip"
	"os"
	"strings"
	"testing"
)

func TestDefaultAllowsIPAddressContract(t *testing.T) {
	allowed := []string{
		"93.184.216.34", "1.1.1.1", "10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.254",
		"192.168.1.10", "172.32.0.1", "::ffff:10.1.2.3", "::ffff:93.184.216.34", "2606:4700::1111",
	}
	blocked := []string{
		"100.64.0.1", "100.127.255.254", "100.100.100.200", "168.63.129.16", "169.254.169.254",
		"127.0.0.1", "0.0.0.0", "0.1.2.3", "192.0.0.1", "192.0.2.1", "192.0.2.2", "192.88.99.1",
		"198.51.100.1", "203.0.113.1", "198.18.0.1", "198.19.255.254", "240.0.0.1", "255.255.255.255",
		"224.0.0.1", "239.255.255.250", "::ffff:169.254.169.254", "::ffff:127.0.0.1",
		"fd00:ec2::254", "fc00::1", "fe80::1", "::1", "::", "64:ff9b::a9fe:a9fe", "2001:db8::1",
		"2002::1", "2001::1", "3fff::1", "ff02::1",
	}
	for _, value := range allowed {
		if !DefaultAllowsIP(netip.MustParseAddr(value)) {
			t.Errorf("%s must be allowed by default forwarding", value)
		}
	}
	for _, value := range blocked {
		if DefaultAllowsIP(netip.MustParseAddr(value)) {
			t.Errorf("%s must be blocked by default forwarding", value)
		}
	}
	if DefaultAllowsIP(netip.Addr{}) {
		t.Error("invalid address allowed")
	}
}

func TestDefaultAllowDoesNotWidenExplicitRules(t *testing.T) {
	rule := Rule{ID: "tcp", Protocol: "tcp", Host: "example.com", Ports: []uint16{22}}
	for _, value := range []string{"10.0.0.1", "192.168.1.1"} {
		if rule.AllowsIP(netip.MustParseAddr(value)) {
			t.Fatalf("explicit rule unexpectedly allows %s", value)
		}
	}
}

func TestWorkerDefaultValidationAndDigest(t *testing.T) {
	deny := validConfig()
	if err := deny.normalize(); err != nil || deny.DefaultAllows() || deny.EgressDefault() != "deny" {
		t.Fatalf("omitted default must be deny: %v", err)
	}
	denyDigest, _ := deny.ComputeDigest()
	encoded, _ := json.Marshal(deny)
	if strings.Contains(string(encoded), `"default"`) {
		t.Fatal("deny mode must omit the default field")
	}

	allow := validConfig()
	allow.Default = DefaultAllow
	if err := allow.normalize(); err != nil || !allow.DefaultAllows() || allow.EgressDefault() != "allow" {
		t.Fatalf("allow default rejected: %v", err)
	}
	allowDigest, _ := allow.ComputeDigest()
	if allowDigest == denyDigest {
		t.Fatal("mode change must change the digest")
	}
	if _, err := Parse(signed(t, allow)); err != nil {
		t.Fatal(err)
	}

	for _, value := range []string{"deny", "Allow", "open", " allow"} {
		cfg := validConfig()
		cfg.Default = value
		if err := cfg.normalize(); err == nil {
			t.Fatalf("default %q accepted", value)
		}
	}
	if _, err := Prepare([]byte(`{"version":1,"vm_id":"vm","generation":"g","default":true,"rules":[],"ingress":[],"quotas":{"tcp_flows":1,"flows_per_destination":1,"ingress_flows":1,"dns_mappings":1,"open_files":64}}`)); err == nil {
		t.Fatal("non-string default accepted")
	}
}

func TestDefaultAllowKeepsExplicitOnlyMatching(t *testing.T) {
	cfg := validConfig()
	cfg.Default = DefaultAllow
	if err := cfg.normalize(); err != nil {
		t.Fatal(err)
	}
	if cfg.AllowsHost("other.example") {
		t.Fatal("allow mode must not widen AllowsHost")
	}
	if _, ok := cfg.Match("tcp", "other.example", 443); ok {
		t.Fatal("allow mode must not create implicit rules")
	}
}

// Python and Go share these worker configs; each must verify with its recorded digest.
func TestCrossLanguageWorkerConfigFixtures(t *testing.T) {
	data, err := os.ReadFile("../../testdata/worker-config-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name   string          `json:"name"`
		Mode   string          `json:"mode"`
		Worker json.RawMessage `json:"worker"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) < 2 {
		t.Fatal("fixture must cover deny and allow")
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			cfg, err := Parse(tc.Worker)
			if err != nil {
				t.Fatal(err)
			}
			if cfg.EgressDefault() != tc.Mode {
				t.Fatalf("mode %q, want %q", cfg.EgressDefault(), tc.Mode)
			}
		})
	}
}
