package netstack

import (
	"bytes"
	"testing"
)

func requestPacket(messageType byte) []byte {
	packet := make([]byte, 240)
	packet[0], packet[1], packet[2] = 1, 1, 6
	copy(packet[4:8], []byte{1, 2, 3, 4})
	copy(packet[28:34], []byte{0x52, 0x54, 0, 0x12, 0x34, 0x56})
	copy(packet[236:240], []byte{99, 130, 83, 99})
	return append(packet, 53, 1, messageType, 255)
}

func TestDHCPReply(t *testing.T) {
	for request, response := range map[byte]byte{dhcpDiscover: dhcpOffer, dhcpRequest: dhcpAck} {
		reply := dhcpReply(requestPacket(request))
		if len(reply) < 244 || reply[0] != 2 || !bytes.Equal(reply[16:20], []byte{192, 0, 2, 2}) || !bytes.Contains(reply[240:], []byte{53, 1, response}) {
			t.Fatalf("bad DHCP response: %v", reply)
		}
	}
	if dhcpReply([]byte{1}) != nil || dhcpReply(requestPacket(99)) != nil {
		t.Fatal("accepted malformed DHCP")
	}
}
