package syntheticdns

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
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
	for _, label := range strings.Split(name, ".") {
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

func TestNumericNamesNeverReceiveOrRestoreSyntheticIdentity(t *testing.T) {
	r := New(allowAll{})
	for _, kind := range []uint16{1, 28} {
		reply := r.Reply(query("93.184.216.34", kind))
		if reply == nil || reply[3]&15 != 5 || binary.BigEndian.Uint16(reply[6:8]) != 0 {
			t.Fatalf("numeric query type %d was not refused", kind)
		}
	}
	if r.Len() != 0 {
		t.Fatal("numeric query allocated state")
	}
	if err := r.Restore([]Mapping{{Host: "93.184.216.34", IP: "198.18.0.1"}}); err == nil {
		t.Fatal("restored numeric DNS identity")
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

// withAdditional appends raw additional-section bytes and sets the header's record count.
func withAdditional(count byte, extra ...byte) []byte {
	packet := query("api.example", 1)
	packet[11] = count
	return append(packet, extra...)
}

func TestQueryMayCarryOneEDNSRecord(t *testing.T) {
	opt := []byte{0, 0, 41, 0x04, 0xd0, 0, 0, 0, 0, 0, 0} // root name, type OPT, 1232-byte payload, no options
	withOption := append(append([]byte(nil), opt[:9]...), 0, 4, 0, 10, 0, 0)
	r := New(allow{"api.example": true})
	plain := r.Reply(query("api.example", 1))
	if plain == nil {
		t.Fatal("plain query not answered")
	}
	for name, packet := range map[string][]byte{
		"bare OPT":         withAdditional(1, opt...),
		"OPT with options": withAdditional(1, withOption...),
	} {
		if reply := r.Reply(packet); !bytes.Equal(reply, plain) {
			t.Errorf("%s: reply %v, want the plain reply %v", name, reply, plain)
		}
	}
	notOPT := append([]byte(nil), opt...)
	notOPT[2] = 1
	longData := append([]byte(nil), opt...)
	longData[10] = 4
	for name, packet := range map[string][]byte{
		"two records":             withAdditional(2, append(append([]byte(nil), opt...), opt...)...),
		"record is not OPT":       withAdditional(1, notOPT...),
		"data length past packet": withAdditional(1, longData...),
		"non-root name":           withAdditional(1, append([]byte{1, 'a'}, opt...)...),
		"count without record":    withAdditional(1),
		"stray byte, count zero":  withAdditional(0, 0),
	} {
		if reply := r.Reply(packet); reply != nil {
			t.Errorf("%s: malformed query was answered: %v", name, reply)
		}
	}
}

func FuzzReply(f *testing.F) {
	f.Add(query("api.example", 1))
	f.Add(withAdditional(1, 0, 0, 41, 0x04, 0xd0, 0, 0, 0, 0, 0, 0))
	r := New(allow{"api.example": true})
	f.Fuzz(func(t *testing.T, packet []byte) { _ = r.Reply(packet) })
}
