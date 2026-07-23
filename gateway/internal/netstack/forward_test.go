package netstack

import (
	"context"
	"net"
	"testing"

	"github.com/nueces/sbx/gateway/internal/config"
)

func TestForwarderQuotas(t *testing.T) {
	f := &TCPForwarder{config: config.Config{Quotas: config.Quotas{FlowsPerDestination: 1}}, total: make(chan struct{}, 1), perDest: make(map[string]int)}
	if !f.acquire("example.com") || f.acquire("example.com") {
		t.Fatal("flow quota not enforced")
	}
	f.release("example.com")
	if !f.acquire("example.com") {
		t.Fatal("flow quota was not released")
	}
	f.release("example.com")
}

func TestDialRejectsForbiddenAddressUnlessExplicit(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	f := &TCPForwarder{}
	if conn, err := f.dial(context.Background(), config.Rule{}, "127.0.0.1", uint16(port)); err == nil {
		conn.Close()
		t.Fatal("dialed forbidden loopback")
	}
	go func() {
		conn, _ := listener.Accept()
		if conn != nil {
			conn.Close()
		}
	}()
	conn, err := f.dial(context.Background(), config.Rule{AllowRanges: []string{"127.0.0.0/8"}}, "127.0.0.1", uint16(port))
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
}
