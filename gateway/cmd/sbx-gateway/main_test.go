package main

import (
	"testing"

	"github.com/nueces/sbx/gateway/internal/config"
)

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
