package syntheticdns

import (
	"encoding/binary"
	"errors"
	"fmt"
	"testing"
)

type allow map[string]bool

func (a allow) AllowsHost(host string) bool { return a[host] }

type allowAll struct{}

func (allowAll) AllowsHost(string) bool { return true }

func query(name string, qtype uint16) []byte {
	packet := make([]byte, 12)
	packet[0], packet[1] = 1, 2
	packet[5] = 1
	for _, label := range []string{name[:3], name[4:]} {
		packet = append(packet, byte(len(label)))
		packet = append(packet, label...)
	}
	packet = append(packet, 0, 0, 0, 0, 1)
	binary.BigEndian.PutUint16(packet[len(packet)-4:len(packet)-2], qtype)
	return packet
}

func TestResolver(t *testing.T) {
	r := New(allow{"api.example": true})
	first := r.Reply(query("api.example", 1))
	second := r.Reply(query("api.example", 1))
	if first == nil || second == nil || binary.BigEndian.Uint16(first[6:8]) != 1 || string(first[len(first)-4:]) != string(second[len(second)-4:]) {
		t.Fatal("unstable answer")
	}
	ip, _ := r.Resolve("api.example")
	if host, ok := r.Acquire(ip); !ok || host != "api.example" {
		t.Fatal("reverse mapping missing")
	}
	r.Release(ip)
	denied := r.Reply(query("bad.example", 1))
	if denied == nil || denied[3]&15 != 5 {
		t.Fatal("denied query was not refused")
	}
	if got := r.Reply(query("api.example", 28)); got == nil || binary.BigEndian.Uint16(got[6:8]) != 0 {
		t.Fatal("AAAA behavior")
	}
}

func TestActiveMappingIsNotEvicted(t *testing.T) {
	r := New(allowAll{})
	first, _ := r.Resolve("host-0.example")
	if _, ok := r.Acquire(first); !ok {
		t.Fatal("failed to activate mapping")
	}
	for i := 1; i < MaxMappings; i++ {
		if _, err := r.Resolve(fmt.Sprintf("host-%d.example", i)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := r.Resolve("new.example"); !errors.Is(err, ErrCapacity) {
		t.Fatalf("full resolver did not refuse a new mapping: %v", err)
	}
	if got, _ := r.Resolve("host-0.example"); got != first {
		t.Fatal("evicted active mapping")
	}
	r.Release(first)
}

func TestConfiguredCapacityIsPerResolver(t *testing.T) {
	a := NewWithLimit(allow{"a.example": true, "b.example": true}, 1)
	b := NewWithLimit(allow{"a.example": true, "b.example": true}, 2)
	if _, err := a.Resolve("a.example"); err != nil {
		t.Fatal(err)
	}
	first, _ := a.byName["a.example"]
	first.active = 1
	if _, err := a.Resolve("b.example"); err == nil {
		t.Fatal("resolver admitted a mapping beyond its configured active ceiling")
	}
	if _, err := b.Resolve("a.example"); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Resolve("b.example"); err != nil || b.Len() != 2 || a.Len() != 1 {
		t.Fatal("one VM's DNS mapping ceiling affected another resolver")
	}
}

func TestCachedAddressIsNeverReassignedAtCapacity(t *testing.T) {
	r := NewWithLimit(allow{"one.example": true, "two.example": true}, 1)
	first := r.Reply(query("one.example", 1))
	if first == nil || binary.BigEndian.Uint16(first[6:8]) != 1 {
		t.Fatal("first DNS answer missing")
	}
	ip, _ := r.Resolve("one.example")
	denied := r.Reply(query("two.example", 1))
	if denied == nil || denied[3]&15 != 5 || binary.BigEndian.Uint16(denied[6:8]) != 0 {
		t.Fatal("capacity exhaustion did not refuse new DNS mapping")
	}
	if host, ok := r.Acquire(ip); !ok || host != "one.example" {
		t.Fatal("cached address changed its destination")
	}
	r.Release(ip)
	if got, err := r.Resolve("one.example"); err != nil || got != ip || r.Len() != 1 {
		t.Fatal("existing cached destination stopped working at capacity")
	}
}

func TestRestoreDoesNotReuseAnAssignedAddress(t *testing.T) {
	original := New(allowAll{})
	first, _ := original.Resolve("first.example")
	replacement := New(allowAll{})
	if err := replacement.Restore(original.Snapshot()); err != nil {
		t.Fatal(err)
	}
	second, err := replacement.Resolve("second.example")
	if err != nil || first == second {
		t.Fatalf("restored address was reused: %v", err)
	}
	if host, ok := replacement.Acquire(first); !ok || host != "first.example" {
		t.Fatal("restored address lost its hostname")
	}
}

func TestSnapshotRestore(t *testing.T) {
	original := New(allow{"api.example": true})
	want, _ := original.Resolve("api.example")
	restored := New(allow{"api.example": true})
	if err := restored.Restore(original.Snapshot()); err != nil {
		t.Fatal(err)
	}
	got, err := restored.Resolve("api.example")
	if err != nil || got != want {
		t.Fatalf("restored %s, want %s: %v", got, want, err)
	}
	if err := New(allowAll{}).Restore([]Mapping{{Host: "bad", IP: "127.0.0.1"}}); err == nil {
		t.Fatal("restored out-of-pool address")
	}
}

func FuzzReply(f *testing.F) {
	f.Add(query("api.example", 1))
	r := New(allow{"api.example": true})
	f.Fuzz(func(t *testing.T, packet []byte) { _ = r.Reply(packet) })
}
