package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/nueces/sbx/gateway/internal/config"
	gatewaynet "github.com/nueces/sbx/gateway/internal/netstack"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
)

func TestIncarnationProofBindsEveryIdentity(t *testing.T) {
	token := []byte("01234567890123456789012345678901")
	proof := incarnationProof(token, "vm1", "g1", "digest", "worker1")
	for _, values := range [][4]string{
		{"vm2", "g1", "digest", "worker1"},
		{"vm1", "g2", "digest", "worker1"},
		{"vm1", "g1", "changed", "worker1"},
		{"vm1", "g1", "digest", "worker0"},
	} {
		if proof == incarnationProof(token, values[0], values[1], values[2], values[3]) {
			t.Fatal("incarnation proof did not bind all worker identity fields")
		}
	}
	if proof == incarnationProof([]byte("abcdefghijabcdefghijabcdefghijkl"), "vm1", "g1", "digest", "worker1") {
		t.Fatal("incarnation proof accepted another token")
	}
}

func TestReadConfigRejectsSymlinkPermissionsAndOversize(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "policy.json")
	if err := os.WriteFile(path, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfig(path, true); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfig(path, true); err == nil {
		t.Fatal("accepted non-private runtime config")
	}
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(directory, "link.json")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfig(link, true); err == nil {
		t.Fatal("accepted symlink config")
	}
	if err := os.Truncate(path, 1<<20+1); err != nil {
		t.Fatal(err)
	}
	if _, err := readConfig(path, true); err == nil {
		t.Fatal("accepted oversized config")
	}
}

func TestPrepareDNSRestoresWildcardAddressesBeforeTraffic(t *testing.T) {
	cfg := config.Config{VMID: "vm", Generation: "generation", Digest: "digest", Quotas: config.DefaultQuotas(), Rules: []config.Rule{
		{ID: "wild", Protocol: "tcp", Host: "*.wild.example", Ports: []uint16{443}},
		{ID: "exact", Protocol: "tcp", Host: "exact.example", Ports: []uint16{443}},
	}}
	path := filepath.Join(t.TempDir(), "dns.jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(file).Encode(map[string]any{"version": 1, "vm_id": cfg.VMID, "generation": cfg.Generation, "digest": cfg.Digest}); err != nil {
		t.Fatal(err)
	}
	original, err := syntheticdns.NewWithJournal(cfg, cfg.Quotas.DNSMappings, file, cfg.VMID, cfg.Generation, cfg.Digest)
	if err != nil {
		t.Fatal(err)
	}
	first, _ := original.Resolve("one.wild.example")
	second, _ := original.Resolve("two.wild.example")
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
	replacement, err := prepareDNS(network, cfg, file)
	if err != nil {
		t.Fatal(err)
	}
	for ip, host := range map[string]string{first.String(): "one.wild.example", second.String(): "two.wild.example"} {
		found := false
		for _, local := range network.Stack.AllAddresses()[gatewaynet.NICID] {
			found = found || local.AddressWithPrefix.Address.String() == ip
		}
		if !found {
			t.Fatalf("cached wildcard address not registered at startup: %s (%s)", ip, host)
		}
	}
	if host, ok := replacement.Acquire(first); !ok || host != "one.wild.example" {
		t.Fatal("cached wildcard connection lost its destination")
	}
	newIP, err := replacement.Resolve("new.wild.example")
	if err != nil || newIP == first || newIP == second {
		t.Fatalf("new wildcard lookup overwrote a restored destination: %v", err)
	}
}

func TestValidateIdentity(t *testing.T) {
	cfg := config.Config{VMID: "vm1", Generation: "g1", Digest: "abc"}
	if err := validateIdentity(cfg, "vm1", "g1", "abc"); err != nil {
		t.Fatal(err)
	}
	for _, values := range [][3]string{{"vm2", "g1", "abc"}, {"vm1", "g2", "abc"}, {"vm1", "g1", "def"}} {
		if err := validateIdentity(cfg, values[0], values[1], values[2]); err == nil {
			t.Fatal("accepted identity mismatch")
		}
	}
}
