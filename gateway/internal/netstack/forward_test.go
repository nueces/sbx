package netstack

import (
	"context"
	"io"
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

func tcpPair(t *testing.T) (*net.TCPConn, *net.TCPConn) {
	t.Helper()
	listener, err := net.ListenTCP("tcp", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	accepted := make(chan *net.TCPConn, 1)
	go func() { conn, _ := listener.AcceptTCP(); accepted <- conn }()
	client, err := net.DialTCP("tcp", nil, listener.Addr().(*net.TCPAddr))
	if err != nil {
		t.Fatal(err)
	}
	server := <-accepted
	listener.Close()
	return client, server
}

func TestRelayStreamsAndHalfCloses(t *testing.T) {
	guestApp, guestRelay := tcpPair(t)
	upstreamRelay, upstreamApp := tcpPair(t)
	result := make(chan [2]uint64, 1)
	go func() { up, down := relay(guestRelay, upstreamRelay); result <- [2]uint64{up, down} }()
	_, _ = guestApp.Write([]byte("upload"))
	_ = guestApp.CloseWrite()
	if got, _ := io.ReadAll(upstreamApp); string(got) != "upload" {
		t.Fatalf("upload %q", got)
	}
	_, _ = upstreamApp.Write([]byte("download"))
	_ = upstreamApp.CloseWrite()
	if got, _ := io.ReadAll(guestApp); string(got) != "download" {
		t.Fatalf("download %q", got)
	}
	if counts := <-result; counts != [2]uint64{6, 8} {
		t.Fatalf("counts %v", counts)
	}
	guestApp.Close()
	upstreamApp.Close()
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
