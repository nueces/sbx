package capability

import (
	"strings"
	"testing"
	"time"
)

func TestCapabilityBindingAndReplay(t *testing.T) {
	issuer, _ := New([]byte(strings.Repeat("a", 32)))
	now := time.Unix(1000, 0)
	issuer.now = func() time.Time { return now }
	meta := Metadata{VMID: "vm1", Incarnation: "worker1", FlowID: "flow1", RuleID: "web", Host: "Example.com", Port: 443}
	user, pass, err := issuer.Issue(meta)
	if err != nil {
		t.Fatal(err)
	}
	got, err := issuer.Verify(user, pass, "example.com", 443)
	if err != nil || got.VMID != "vm1" || got.Host != "example.com" {
		t.Fatalf("verify: %+v, %v", got, err)
	}
	if _, err := issuer.Verify(user, pass, "example.com", 443); err == nil {
		t.Fatal("accepted replay")
	}
	other, _ := New([]byte(strings.Repeat("b", 32)))
	other.now = issuer.now
	if _, err := other.Verify(user, pass, "example.com", 443); err == nil {
		t.Fatal("accepted cross-VM key")
	}
}

func TestCapabilityRejectsDestinationAndExpiry(t *testing.T) {
	issuer, _ := New([]byte(strings.Repeat("a", 32)))
	now := time.Unix(1000, 0)
	issuer.now = func() time.Time { return now }
	meta := Metadata{VMID: "vm1", Incarnation: "worker1", FlowID: "flow1", RuleID: "web", Host: "example.com", Port: 443}
	user, pass, _ := issuer.Issue(meta)
	for _, destination := range []struct {
		host string
		port uint16
	}{{"other.example", 443}, {"example.com", 80}} {
		if _, err := issuer.Verify(user, pass, destination.host, destination.port); err == nil {
			t.Fatal("accepted altered destination")
		}
	}
	now = now.Add(Lifetime + time.Second)
	if _, err := issuer.Verify(user, pass, "example.com", 443); err == nil {
		t.Fatal("accepted expired capability")
	}
}
