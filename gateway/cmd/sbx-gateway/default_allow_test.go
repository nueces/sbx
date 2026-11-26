package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"net/netip"
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

// The startup wiring must hand the default address filter to DNS.
func TestEnableDefaultAllowFiltersRealAnswers(t *testing.T) {
	cfg := config.Config{Default: config.DefaultAllow, Quotas: config.DefaultQuotas(), Rules: []config.Rule{
		{ID: "api", Protocol: "tcp", Host: "api.example", Ports: []uint16{22}},
	}}
	network, err := gatewaynet.New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	resolver := syntheticdns.New(cfg)
	if err := enableDefaultAllow(network, resolver, func(context.Context, string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("93.184.216.34"), netip.MustParseAddr("169.254.169.254")}, nil
	}); err != nil {
		t.Fatal(err)
	}
	answer := resolver.Answer(context.Background(), dnsQuery("open.example"))
	if answer.Synthetic.IsValid() || binary.BigEndian.Uint16(answer.Packet[6:8]) != 1 || !bytes.Equal(answer.Packet[len(answer.Packet)-4:], []byte{93, 184, 216, 34}) {
		t.Fatalf("unmatched name did not receive the filtered real answer: %v", answer.Packet)
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
