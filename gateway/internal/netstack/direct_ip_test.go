package netstack

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
)

func TestLiteralPacketAdmissionWithoutDNS(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	key := []byte(strings.Repeat("k", capability.KeySize))
	result := make(chan error, 1)
	go fakeSOCKSServer(listener, key, result)
	mediator, err := NewMediator(listener.Addr().String(), key, "vm1", "worker1")
	if err != nil {
		t.Fatal(err)
	}
	network, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	guest, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer guest.Close()
	if err := guest.Stack.RemoveAddress(NICID, tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4())); err != nil {
		t.Fatal(err)
	}
	if err := guest.AddLocalAddress(net.ParseIP(GuestIP)); err != nil {
		t.Fatal(err)
	}
	if err := network.AddLocalAddress(net.ParseIP("93.184.216.34")); err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{Quotas: config.DefaultQuotas(), Rules: []config.Rule{{ID: "ip", Protocol: "http", Host: "93.184.216.34", Ports: []uint16{443}}}}
	resolver := syntheticdns.New(cfg)
	network.ForwardTCP(cfg, resolver, mediator, NewFlowQuota(4))
	defer pumpFrames(network, guest)()
	for _, tc := range []struct {
		host    string
		port    uint16
		allowed bool
	}{
		{"93.184.216.34", 443, true}, {"93.184.216.34", 444, false}, {"93.184.216.35", 443, false},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), 250*time.Millisecond)
		conn, err := gonet.DialTCPWithBind(ctx, guest.Stack, tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GuestIP).To4())}, tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(tc.host).To4()), Port: tc.port}, ipv4.ProtocolNumber)
		cancel()
		if conn != nil {
			conn.Close()
		}
		if (err == nil) != tc.allowed {
			t.Fatalf("%s:%d allowed=%v error=%v", tc.host, tc.port, tc.allowed, err)
		}
	}
	select {
	case err := <-result:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("IP packet never reached destination-bound SOCKS admission")
	}
	if resolver.Len() != 0 {
		t.Fatal("literal forwarding allocated a DNS identity")
	}
}
