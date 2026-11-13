package netstack

import (
	"context"
	"io"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
)

func TestForwarderReconnectsReusedGuestPort(t *testing.T) {
	listener, err := net.ListenTCP("tcp", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		for {
			conn, err := listener.AcceptTCP()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
				_, _ = conn.Write([]byte("ok"))
				_ = conn.CloseWrite()
				_, _ = io.Copy(io.Discard, conn)
			}()
		}
	}()
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
	guestIP := net.IPv4(192, 0, 2, 2)
	if err := guest.AddLocalAddress(guestIP); err != nil {
		t.Fatal(err)
	}
	port := uint16(listener.Addr().(*net.TCPAddr).Port)
	cfg := config.Config{Rules: []config.Rule{{ID: "local-fixture", Protocol: "tcp", Host: "localhost", Ports: []uint16{port}, AllowRanges: []string{"127.0.0.0/8"}}}, Quotas: config.DefaultQuotas()}
	resolver := syntheticdns.New(cfg)
	address, err := resolver.Resolve("localhost")
	if err != nil {
		t.Fatal(err)
	}
	if err := network.AddLocalAddress(net.IP(address.AsSlice())); err != nil {
		t.Fatal(err)
	}
	quota := NewFlowQuota(2)
	network.ForwardTCP(cfg, resolver, nil, quota)
	ctx, cancel := context.WithCancel(context.Background())
	var pumps sync.WaitGroup
	for _, pair := range [][2]*Network{{network, guest}, {guest, network}} {
		pumps.Add(1)
		go func(source, destination *Network) {
			defer pumps.Done()
			for {
				frame := source.Read(ctx)
				if frame == nil {
					return
				}
				destination.Inject(frame)
			}
		}(pair[0], pair[1])
	}
	defer func() { cancel(); pumps.Wait() }()
	for attempt := range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		conn, err := gonet.DialTCPWithBind(ctx, guest.Stack,
			tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(guestIP.To4()), Port: 40000},
			tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(address.AsSlice()), Port: port}, ipv4.ProtocolNumber)
		cancel()
		if err != nil {
			t.Fatalf("connection %d using the same guest port: %v", attempt, err)
		}
		_ = conn.SetDeadline(time.Now().Add(time.Second))
		body, err := io.ReadAll(conn)
		conn.Close()
		if err != nil || string(body) != "ok" {
			t.Fatalf("connection %d response %q: %v", attempt, body, err)
		}
		deadline := time.Now().Add(time.Second)
		for quota.Active() != 0 && time.Now().Before(deadline) {
			time.Sleep(time.Millisecond)
		}
		if quota.Active() != 0 {
			t.Fatal("completed relay did not release its flow quota")
		}
	}
}
