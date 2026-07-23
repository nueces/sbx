package capability

import (
	"bytes"
	"os"
	"strings"
	"testing"
	"time"
)

func testIssuer(t *testing.T, key, vm, incarnation string, now time.Time) *Issuer {
	t.Helper()
	issuer, err := New([]byte(strings.Repeat(key, KeySize)), vm, incarnation)
	if err != nil {
		t.Fatal(err)
	}
	issuer.now = func() time.Time { return now }
	return issuer
}

func TestCapabilityBindingAndReplay(t *testing.T) {
	now := time.Unix(1000, 0)
	issuer := testIssuer(t, "a", "vm1", "worker1", now)
	meta := Metadata{VMID: "vm1", Incarnation: "worker1", FlowID: "flow1", RuleID: "web", Host: "Example.com", Port: 443}
	user, pass, err := issuer.Issue(meta)
	if err != nil {
		t.Fatal(err)
	}
	got, err := issuer.Verify(user, pass, "example.com", 443)
	if err != nil || got.VMID != "vm1" || got.Host != "example.com" || !got.Issued.Equal(now) {
		t.Fatalf("verify: %+v, %v", got, err)
	}
	if _, err := issuer.Verify(user, pass, "example.com", 443); err == nil {
		t.Fatal("accepted replay")
	}
	for name, other := range map[string]*Issuer{
		"cross VM":          testIssuer(t, "a", "vm2", "worker1", now),
		"prior incarnation": testIssuer(t, "b", "vm1", "worker0", now),
	} {
		if _, err := other.Verify(user, pass, "example.com", 443); err == nil {
			t.Fatalf("accepted %s capability", name)
		}
	}
}

func TestCapabilityRejectsAlterationAndExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	issuer := testIssuer(t, "a", "vm1", "worker1", now)
	meta := Metadata{VMID: "vm1", Incarnation: "worker1", FlowID: "flow1", RuleID: "web", Host: "example.com", Port: 443}
	user, pass, _ := issuer.Issue(meta)
	for _, credential := range []struct {
		user, pass, host string
		port             uint16
	}{
		{user, pass, "other.example", 443},
		{user, pass, "example.com", 80},
		{strings.Replace(user, "flow1", "flow2", 1), pass, "example.com", 443},
		{user, pass + "x", "example.com", 443},
	} {
		if _, err := issuer.Verify(credential.user, credential.pass, credential.host, credential.port); err == nil {
			t.Fatal("accepted altered capability")
		}
	}
	now = now.Add(Lifetime + time.Second)
	issuer.now = func() time.Time { return now }
	if _, err := issuer.Verify(user, pass, "example.com", 443); err == nil {
		t.Fatal("accepted expired capability")
	}
}

func FuzzVerify(f *testing.F) {
	f.Add("1|vm1|worker1|flow|rule|1|2|AAAAAAAAAAAAAAAA|example.com|443", "password", "example.com", uint16(443))
	f.Fuzz(func(t *testing.T, username, password, host string, port uint16) {
		if len(username) > 1024 || len(password) > 1024 || len(host) > 1024 {
			return
		}
		issuer := testIssuer(t, "a", "vm1", "worker1", time.Unix(1, 0))
		_, _ = issuer.Verify(username, password, host, port)
	})
}

func TestCapabilityKeyDescriptorHelpers(t *testing.T) {
	key, err := GenerateKey()
	if err != nil || len(key) != KeySize {
		t.Fatalf("generated key: %d bytes, %v", len(key), err)
	}
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := write.Write(key); err != nil {
		t.Fatal(err)
	}
	write.Close()
	got, err := ReadKey(read)
	read.Close()
	if err != nil || !bytes.Equal(got, key) {
		t.Fatal("inherited descriptor key transfer failed")
	}
	for _, invalid := range [][]byte{key[:KeySize-1], append(key, 0)} {
		if _, err := ReadKey(bytes.NewReader(invalid)); err == nil {
			t.Fatal("accepted invalid descriptor key")
		}
	}
}
