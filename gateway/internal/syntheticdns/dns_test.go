package syntheticdns

import (
	"encoding/binary"
	"testing"
)

type allow map[string]bool

func (a allow) AllowsHost(host string) bool { return a[host] }

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
	if host, ok := r.Host(ip); !ok || host != "api.example" {
		t.Fatal("reverse mapping missing")
	}
	denied := r.Reply(query("bad.example", 1))
	if denied == nil || denied[3]&15 != 5 {
		t.Fatal("denied query was not refused")
	}
	if got := r.Reply(query("api.example", 28)); got == nil || binary.BigEndian.Uint16(got[6:8]) != 0 {
		t.Fatal("AAAA behavior")
	}
}

func FuzzReply(f *testing.F) {
	f.Add(query("api.example", 1))
	r := New(allow{"api.example": true})
	f.Fuzz(func(t *testing.T, packet []byte) { _ = r.Reply(packet) })
}
