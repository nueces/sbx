package netstack

import (
	"context"
	"io"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
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

func fakeSOCKSServer(listener net.Listener, key []byte, result chan<- error) {
	conn, err := listener.Accept()
	if err != nil {
		result <- err
		return
	}
	defer conn.Close()
	greeting := make([]byte, 3)
	if _, err = io.ReadFull(conn, greeting); err != nil || string(greeting) != "\x05\x01\x02" {
		result <- io.ErrUnexpectedEOF
		return
	}
	_, _ = conn.Write([]byte{5, 2})
	header := make([]byte, 2)
	if _, err = io.ReadFull(conn, header); err != nil || header[0] != 1 {
		result <- io.ErrUnexpectedEOF
		return
	}
	username := make([]byte, int(header[1]))
	_, err = io.ReadFull(conn, username)
	var size [1]byte
	if err == nil {
		_, err = io.ReadFull(conn, size[:])
	}
	password := make([]byte, int(size[0]))
	if err == nil {
		_, err = io.ReadFull(conn, password)
	}
	parts := strings.Split(string(username), "|")
	if err != nil || len(parts) != 10 {
		result <- io.ErrUnexpectedEOF
		return
	}
	port, _ := strconv.ParseUint(parts[9], 10, 16)
	verifier, _ := capability.New(key, "vm1", "worker1")
	if _, err = verifier.Verify(string(username), string(password), parts[8], uint16(port)); err != nil {
		result <- err
		return
	}
	_, _ = conn.Write([]byte{1, 0})
	request := make([]byte, 5)
	if _, err = io.ReadFull(conn, request); err != nil || string(request[:4]) != "\x05\x01\x00\x03" {
		result <- io.ErrUnexpectedEOF
		return
	}
	destination := make([]byte, int(request[4])+2)
	if _, err = io.ReadFull(conn, destination); err != nil || string(destination[:len(destination)-2]) != parts[8] {
		result <- io.ErrUnexpectedEOF
		return
	}
	_, _ = conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0})
	result <- nil
}

func TestMediatorDialUsesAuthenticatedDestinationBoundSOCKS(t *testing.T) {
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
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	conn, err := mediator.Dial(ctx, config.Rule{ID: "web", Protocol: "http"}, "example.com", 443)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	if err := <-result; err != nil {
		t.Fatal(err)
	}
}

func TestMediatorCrashFailsClosed(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	key := []byte(strings.Repeat("k", capability.KeySize))
	mediator, err := NewMediator(address, key, "vm1", "worker1")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if conn, err := mediator.Dial(ctx, config.Rule{ID: "web", Protocol: "http"}, "example.com", 443); err == nil {
		conn.Close()
		t.Fatal("mediator outage did not fail closed")
	}
	if _, err := NewMediator("0.0.0.0:8080", key, "vm1", "worker1"); err == nil {
		t.Fatal("accepted non-loopback mediator")
	}
}
