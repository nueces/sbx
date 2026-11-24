package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/nueces/sbx/gateway/internal/config"
	gatewaynet "github.com/nueces/sbx/gateway/internal/netstack"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
)

func dnsQuery(name string) []byte {
	packet := []byte{0, 7, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0}
	for _, label := range strings.Split(name, ".") {
		packet = append(packet, byte(len(label)))
		packet = append(packet, label...)
	}
	return append(packet, 0, 0, 1, 0, 1)
}

func registered(network *gatewaynet.Network, ip netip.Addr) bool {
	for _, local := range network.Stack.AllAddresses()[gatewaynet.NICID] {
		if local.AddressWithPrefix.Address.String() == ip.String() {
			return true
		}
	}
	return false
}

func TestAllowModeReplacementRestoresPolicyMappingsAndServesRealAnswers(t *testing.T) {
	cfg := config.Config{Version: config.Version, VMID: "vm", Generation: "generation", Default: config.DefaultAllow, Quotas: config.DefaultQuotas(), Rules: []config.Rule{
		{ID: "api", Protocol: "tcp", Host: "api.example", Ports: []uint16{22}},
		{ID: "wild", Protocol: "tcp", Host: "*.wild.example", Ports: []uint16{443}},
	}}
	var err error
	if cfg.Digest, err = cfg.ComputeDigest(); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	reparsed, err := config.Parse(encoded)
	if err != nil || reparsed.EgressDefault() != "allow" {
		t.Fatalf("allow mode lost across serialization: %v", err)
	}
	path := filepath.Join(t.TempDir(), "dns.jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(file).Encode(map[string]any{"version": 1, "vm_id": cfg.VMID, "generation": cfg.Generation, "digest": cfg.Digest}); err != nil {
		t.Fatal(err)
	}
	original, err := syntheticdns.NewWithJournal(reparsed, cfg.Quotas.DNSMappings, file, cfg.VMID, cfg.Generation, cfg.Digest)
	if err != nil {
		t.Fatal(err)
	}
	cached, err := original.Resolve("one.wild.example")
	if err != nil {
		t.Fatal(err)
	}
	file.Close()

	file, err = os.OpenFile(path, os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	network, err := gatewaynet.New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	replacement, err := prepareDNS(network, reparsed, file)
	if err != nil {
		t.Fatal(err)
	}
	lookups := 0
	err = enableDefaultAllow(network, replacement, func(_ context.Context, host string) ([]netip.Addr, error) {
		lookups++
		if host != "open.example" {
			t.Errorf("unexpected host lookup %q", host)
		}
		return []netip.Addr{netip.MustParseAddr("93.184.216.34"), netip.MustParseAddr("169.254.169.254")}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	journal, _ := os.ReadFile(path)

	if !registered(network, cached) {
		t.Fatal("restored policy mapping not registered after replacement")
	}
	if host, ok := replacement.Acquire(cached); !ok || host != "one.wild.example" {
		t.Fatal("restored policy mapping changed destination")
	}
	replacement.Release(cached)
	if again, err := replacement.Resolve("one.wild.example"); err != nil || again != cached {
		t.Fatal("restored policy name received a new identity")
	}

	answer := replacement.Answer(context.Background(), dnsQuery("open.example"))
	if answer.Synthetic.IsValid() || binary.BigEndian.Uint16(answer.Packet[6:8]) != 1 || !bytes.Equal(answer.Packet[len(answer.Packet)-4:], []byte{93, 184, 216, 34}) {
		t.Fatalf("unmatched name did not receive the filtered real answer: %v", answer.Packet)
	}
	if lookups != 1 || registered(network, netip.MustParseAddr("93.184.216.34")) {
		t.Fatal("real answer used the wrong path or was registered")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(journal, after) {
		t.Fatal("unmatched DNS answer changed the journal")
	}
}

func TestReadinessPayloadReportsEgressDefault(t *testing.T) {
	cfg := config.Config{VMID: "vm", Generation: "g", Digest: "d", Rules: []config.Rule{
		{ID: "one", Protocol: "tcp", Host: "93.184.216.34", Ports: []uint16{443}},
		{ID: "two", Protocol: "tcp", Host: "example.com", Ports: []uint16{22}},
	}}
	deny := readinessPayload(cfg, "worker1", "proof", false, 2, "hardening")
	if deny["egress_default"] != "deny" || deny["direct_ip_routes"] != 1 || deny["status"] != "ready" || deny["ingress"] != 2 || deny["mediator"] != false {
		t.Fatalf("deny readiness %v", deny)
	}
	cfg.Default = config.DefaultAllow
	allow := readinessPayload(cfg, "worker1", "proof", true, 0, "hardening")
	if allow["egress_default"] != "allow" || allow["direct_ip_routes"] != 1 {
		t.Fatalf("allow readiness %v", allow)
	}
	want := []string{"digest", "direct_ip_routes", "egress_default", "generation", "hardening", "incarnation", "incarnation_proof", "ingress", "mediator", "status", "version", "vm_id"}
	if len(allow) != len(want) {
		t.Fatalf("readiness keys changed: %v", allow)
	}
	for _, key := range want {
		if _, ok := allow[key]; !ok {
			t.Fatalf("readiness missing %q", key)
		}
	}
}
