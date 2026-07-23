package main

import (
	"testing"

	"github.com/nueces/sbx/gateway/internal/config"
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
