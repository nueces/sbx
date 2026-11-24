package syntheticdns

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"testing"
)

// permitPublicOnly stands in for config.DefaultAllowsIP: it rejects loopback and link-local.
func permitPublicOnly(ip netip.Addr) bool {
	return !ip.IsLoopback() && !ip.IsLinkLocalUnicast() && !netip.MustParsePrefix("198.18.0.0/15").Contains(ip)
}

func answers(t *testing.T, reply []byte) []netip.Addr {
	t.Helper()
	count := int(binary.BigEndian.Uint16(reply[6:8]))
	result := make([]netip.Addr, 0, count)
	for i := range count {
		end := len(reply) - (count-1-i)*16
		result = append(result, netip.AddrFrom4([4]byte(reply[end-4:end])))
	}
	return result
}

func upstreamTable(table map[string][]string, err map[string]error, calls *int) LookupFunc {
	return func(_ context.Context, host string) ([]netip.Addr, error) {
		*calls++
		if failure := err[host]; failure != nil {
			return nil, failure
		}
		var result []netip.Addr
		for _, value := range table[host] {
			result = append(result, netip.MustParseAddr(value))
		}
		return result, nil
	}
}

func TestUnmatchedNamesReceiveFilteredRealAnswers(t *testing.T) {
	calls := 0
	r := NewWithLimit(allow{"api.example": true}, 4)
	r.SetDefaultLookup(upstreamTable(map[string][]string{
		"open.example": {"93.184.216.34", "127.0.0.1", "10.0.0.5", "93.184.216.34", "2606:4700::1", "169.254.169.254", "198.18.0.9"},
		"many.example": {"1.0.0.1", "1.0.0.2", "1.0.0.3", "1.0.0.4", "1.0.0.5", "1.0.0.6", "1.0.0.7", "1.0.0.8", "1.0.0.9", "1.0.0.10"},
		"meta.example": {"169.254.169.254", "127.0.0.1"},
	}, map[string]error{
		"missing.example": &net.DNSError{Err: "no such host", Name: "missing.example", IsNotFound: true},
		"broken.example":  &net.DNSError{Err: "timeout", Name: "broken.example", IsTimeout: true},
		"other.example":   errors.New("boom"),
	}, &calls), permitPublicOnly)

	answer := r.Answer(context.Background(), query("open.example", 1))
	got := answers(t, answer.Packet)
	if answer.Synthetic.IsValid() || len(got) != 2 || got[0] != netip.MustParseAddr("93.184.216.34") || got[1] != netip.MustParseAddr("10.0.0.5") {
		t.Fatalf("filtered real answers = %v synthetic=%v", got, answer.Synthetic)
	}
	if reply := r.Reply(query("many.example", 1)); len(answers(t, reply)) != MaxUpstreamAnswers {
		t.Fatal("upstream answers not bounded")
	}
	if reply := r.Reply(query("meta.example", 1)); reply[3]&15 != 0 || binary.BigEndian.Uint16(reply[6:8]) != 0 {
		t.Fatal("protected-only answers must be NODATA")
	}
	if reply := r.Reply(query("missing.example", 1)); reply[3]&15 != 3 {
		t.Fatal("missing name must be NXDOMAIN")
	}
	for _, name := range []string{"broken.example", "other.example"} {
		if reply := r.Reply(query(name, 1)); reply[3]&15 != 2 {
			t.Fatalf("%s must be SERVFAIL", name)
		}
	}
	if r.Len() != 0 {
		t.Fatal("real answers consumed synthetic capacity")
	}
	policy := r.Answer(context.Background(), query("api.example", 1))
	if !policy.Synthetic.IsValid() || r.Len() != 1 {
		t.Fatal("policy name must keep a synthetic identity")
	}
	before := calls
	if reply := r.Reply(query("api.example", 1)); binary.BigEndian.Uint16(reply[6:8]) != 1 || calls != before {
		t.Fatal("policy name must not use host resolution")
	}
	if reply := r.Reply(query("open.example", 28)); binary.BigEndian.Uint16(reply[6:8]) != 0 || reply[3]&15 != 0 {
		t.Fatal("non-A queries stay empty")
	}
	if reply := r.Reply(query("93.184.216.34", 1)); reply[3]&15 != 5 {
		t.Fatal("numeric names stay refused")
	}
	if r.UpstreamAnswers() == 0 {
		t.Fatal("upstream answers not counted")
	}
}

func TestUnmatchedNamesNeverExhaustOrJournal(t *testing.T) {
	file := journalFile(t)
	r, err := NewWithJournal(allow{"api.example": true}, 2, file, "vm", "generation", "digest")
	if err != nil {
		t.Fatal(err)
	}
	r.SetDefaultLookup(func(context.Context, string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("93.184.216.34")}, nil
	}, permitPublicOnly)
	info, _ := file.Stat()
	size := info.Size()
	for i := range 5000 {
		reply := r.Reply(query(fmt.Sprintf("n%d.example", i), 1))
		if binary.BigEndian.Uint16(reply[6:8]) != 1 {
			t.Fatalf("name %d not answered", i)
		}
	}
	info, _ = file.Stat()
	if r.Len() != 0 || info.Size() != size {
		t.Fatal("unmatched names grew mappings or the journal")
	}
	if reply := r.Reply(query("api.example", 1)); binary.BigEndian.Uint16(reply[6:8]) != 1 {
		t.Fatal("policy name lost capacity")
	}
}

func TestDenyModeRefusesUnmatchedNames(t *testing.T) {
	r := New(allow{"api.example": true})
	if reply := r.Reply(query("open.example", 1)); reply[3]&15 != 5 || r.UpstreamAnswers() != 0 {
		t.Fatal("deny mode answered an unmatched name")
	}
}

func TestRestoreKeepsExplicitPolicyValidationInAllowMode(t *testing.T) {
	r := New(allow{"api.example": true})
	r.SetDefaultLookup(func(context.Context, string) ([]netip.Addr, error) { return nil, nil }, permitPublicOnly)
	if err := r.Restore([]Mapping{{Host: "open.example", IP: "198.18.0.1"}}); err == nil {
		t.Fatal("restore accepted a mapping outside explicit policy")
	}
}
