package netstack

import (
	"context"
	"net"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip/header"
)

func TestNetworkLifecycle(t *testing.T) {
	network, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	if err := network.AddLocalAddress(net.ParseIP("198.18.0.1")); err != nil {
		t.Fatal(err)
	}
	network.Inject([]byte{0}) // malformed Ethernet input must not panic.
}

func TestProbeGuestRequestsIdleGuestAddress(t *testing.T) {
	network, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	if err := network.ProbeGuest(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	frame := network.Read(ctx)
	if len(frame) < header.EthernetMinimumSize+header.ARPSize || header.Ethernet(frame).Type() != header.ARPProtocolNumber {
		t.Fatal("idle guest probe did not emit an ARP request")
	}
	probe := header.ARP(frame[header.EthernetMinimumSize:])
	if probe.Op() != header.ARPRequest || !net.IP(probe.ProtocolAddressSender()).Equal(net.ParseIP(GatewayIP)) || !net.IP(probe.ProtocolAddressTarget()).Equal(net.ParseIP(GuestIP)) {
		t.Fatal("probe did not ask the guest for its address from the gateway")
	}
}

func FuzzInjectEthernetFrame(f *testing.F) {
	f.Add([]byte{0})
	f.Add(make([]byte, 14+20))
	f.Fuzz(func(t *testing.T, frame []byte) {
		if len(frame) > 65535 {
			return
		}
		network, err := New()
		if err != nil {
			t.Fatal(err)
		}
		network.Inject(frame)
		network.Close()
	})
}
