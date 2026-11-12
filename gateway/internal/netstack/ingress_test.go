package netstack

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/config"
)

func tcpListener(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return listener
}

func ingressRule(id string, guestPort int) config.Ingress {
	return config.Ingress{ID: id, ListenHost: "127.0.0.1", GuestAddress: GuestIP, GuestPort: uint16(guestPort)}
}

func TestRegisterIngressBindsDescriptorsToVM(t *testing.T) {
	listener := tcpListener(t)
	defer listener.Close()
	file, err := listener.(*net.TCPListener).File()
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	cfg := config.Config{VMID: "vm1", Generation: "g1", Ingress: []config.Ingress{{ID: "dev", ListenHost: "127.0.0.1", HostPort: uint16(port), GuestAddress: GuestIP, GuestPort: 3000}}}
	spec := fmt.Sprintf("vm1/g1/dev=%d", file.Fd())
	registered, err := RegisterIngress(cfg, []string{spec})
	if err != nil {
		t.Fatal(err)
	}
	defer registered[0].Listener.Close()
	if registered[0].Listener.Addr().String() != listener.Addr().String() {
		t.Fatal("registered listener address changed")
	}

	other := tcpListener(t)
	defer other.Close()
	otherFile, _ := other.(*net.TCPListener).File()
	defer otherFile.Close()
	if _, err := RegisterIngress(cfg, []string{fmt.Sprintf("vm2/g1/dev=%d", otherFile.Fd())}); err == nil || !strings.Contains(err.Error(), "ownership") {
		t.Fatal("cross-VM listener registration succeeded")
	}

	unix, err := net.ListenUnix("unix", &net.UnixAddr{Name: t.TempDir() + "/listener"})
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close()
	unixFile, _ := unix.File()
	if _, err := RegisterIngress(cfg, []string{fmt.Sprintf("vm1/g1/dev=%d", unixFile.Fd())}); err == nil || !strings.Contains(err.Error(), "not a TCP listener") {
		t.Fatal("non-TCP ingress descriptor was accepted")
	}
}

func TestIngressDevServerOAuthAndNotReady(t *testing.T) {
	token := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/callback" {
			token <- r.URL.Query().Get("code")
		}
		_, _ = io.WriteString(w, "guest:"+r.URL.Path)
	}))
	defer server.Close()
	_, rawPort, _ := net.SplitHostPort(server.Listener.Addr().String())
	guestPort, _ := net.LookupPort("tcp", rawPort)

	dev, oauth := tcpListener(t), tcpListener(t)
	rules := []config.Ingress{ingressRule("dev", guestPort), ingressRule("oauth", guestPort)}
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{Rule: rules[0], Listener: dev}, {Rule: rules[1], Listener: oauth}}, NewFlowQuota(4), 4)
	var dials atomic.Int64
	forwarder.dial = func(ctx context.Context, _ config.Ingress) (net.Conn, error) {
		dials.Add(1)
		return (&net.Dialer{}).DialContext(ctx, "tcp", server.Listener.Addr().String())
	}
	defer forwarder.Close()

	queued, err := net.DialTimeout("tcp", dev.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = queued.Write([]byte("GET /dev HTTP/1.0\r\n\r\n"))
	time.Sleep(50 * time.Millisecond)
	if dials.Load() != 0 {
		t.Fatal("guest connection opened before stack readiness")
	}
	forwarder.Start()
	_ = queued.SetReadDeadline(time.Now().Add(time.Second))
	if response, _ := io.ReadAll(queued); !strings.Contains(string(response), "guest:/dev") {
		t.Fatalf("development server response %q", response)
	}
	queued.Close()

	client := &http.Client{Timeout: time.Second}
	response, err := client.Get("http://" + oauth.Addr().String() + "/oauth/callback?code=guest-visible")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	select {
	case got := <-token:
		if got != "guest-visible" {
			t.Fatalf("OAuth token %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("OAuth callback did not reach guest workload")
	}
}

func TestReplacementDiscardsQueuedIngressBeforeReadiness(t *testing.T) {
	listener := tcpListener(t)
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{
		Rule: ingressRule("dev", 3000), Listener: listener,
	}}, NewFlowQuota(1), 1)
	defer forwarder.Close()
	clients := []net.Conn{}
	for range 3 {
		client, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
		if err != nil {
			t.Fatal(err)
		}
		defer client.Close()
		clients = append(clients, client)
	}
	if err := forwarder.DiscardPending(); err != nil {
		t.Fatal(err)
	}
	for _, client := range clients {
		_ = client.SetReadDeadline(time.Now().Add(time.Second))
		var data [1]byte
		if n, err := client.Read(data[:]); n != 0 || (!errors.Is(err, io.EOF) && !errors.Is(err, syscall.ECONNRESET)) {
			t.Fatalf("stale connection was not closed (timeouts are not closure): %v", err)
		}
	}
	forwarder.dial = func(context.Context, config.Ingress) (net.Conn, error) {
		host, guest := net.Pipe()
		go func() { defer guest.Close(); _, _ = io.Copy(guest, guest) }()
		return host, nil
	}
	forwarder.Start()
	fresh, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Close()
	_ = fresh.SetDeadline(time.Now().Add(time.Second))
	if _, err := fresh.Write([]byte("new")); err != nil {
		t.Fatal(err)
	}
	var echoed [3]byte
	if _, err := io.ReadFull(fresh, echoed[:]); err != nil || string(echoed[:]) != "new" {
		t.Fatalf("fresh ingress failed after draining backlog: %q %v", echoed, err)
	}
}

func TestDiscardPendingReportsClosedListener(t *testing.T) {
	listener := tcpListener(t)
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{Listener: listener}}, NewFlowQuota(1), 1)
	defer forwarder.Close()
	listener.Close()
	if err := forwarder.DiscardPending(); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("closed-listener drain failure was masked: %v", err)
	}
}

func TestDiscardPendingEmptyBacklogDoesNotWait(t *testing.T) {
	listener := tcpListener(t)
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{Listener: listener}}, NewFlowQuota(1), 1)
	defer forwarder.Close()
	done := make(chan error, 1)
	go func() { done <- forwarder.DiscardPending() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("empty ingress backlog blocked readiness")
	}
}

type observedListener struct {
	net.Listener
	accepting chan struct{}
}

func (l *observedListener) Accept() (net.Conn, error) {
	select {
	case l.accepting <- struct{}{}:
	default:
	}
	return l.Listener.Accept()
}

func TestIdleIngressDoesNotConsumeOutboundCapacity(t *testing.T) {
	quota := NewFlowQuota(1)
	listeners := []RegisteredIngress{}
	for _, name := range []string{"ssh", "dev"} {
		listener := &observedListener{tcpListener(t), make(chan struct{}, 1)}
		listeners = append(listeners, RegisteredIngress{Rule: ingressRule(name, 22), Listener: listener})
	}
	forwarder := NewIngressForwarder(nil, listeners, quota, 2)
	defer forwarder.Close()
	forwarder.Start()
	for _, item := range listeners {
		select {
		case <-item.Listener.(*observedListener).accepting:
		case <-time.After(time.Second):
			t.Fatal("idle listener never reached Accept")
		}
	}
	outbound := &TCPForwarder{config: config.Config{Quotas: config.DefaultQuotas()}, total: quota, perDest: make(map[string]int)}
	if !outbound.acquire("example.com") {
		t.Fatal("idle ingress exhausted tcp_flows=1 outbound capacity")
	}
	outbound.release("example.com")
	if quota.Active() != 0 || forwarder.Active() != 0 || len(forwarder.concurrent) != 0 {
		t.Fatal("idle ingress consumed active flow capacity")
	}
}

func TestIngressRejectsWhenOutboundOwnsTotalCapacity(t *testing.T) {
	quota := NewFlowQuota(1)
	if !quota.acquire() {
		t.Fatal("could not reserve outbound flow")
	}
	defer quota.release()
	listener := tcpListener(t)
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{Rule: ingressRule("ssh", 22), Listener: listener}}, quota, 2)
	forwarder.dial = func(context.Context, config.Ingress) (net.Conn, error) {
		t.Error("exhausted ingress reached guest dial")
		return nil, context.Canceled
	}
	defer forwarder.Close()
	forwarder.Start()
	client, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetReadDeadline(time.Now().Add(time.Second))
	var data [1]byte
	_, err = client.Read(data[:])
	if err != io.EOF {
		t.Fatalf("exhausted ingress was not rejected: %v", err)
	}
	if quota.Active() != 1 || len(forwarder.concurrent) != 0 {
		t.Fatal("rejected ingress leaked quota")
	}
}

func TestIngressClosesConnectionThatCompletesAfterShutdown(t *testing.T) {
	forwarder := NewIngressForwarder(nil, nil, NewFlowQuota(1), 1)
	forwarder.Close()
	host, peer := net.Pipe()
	defer peer.Close()
	forwarder.track(host, true)
	_ = peer.SetReadDeadline(time.Now().Add(time.Second))
	var data [1]byte
	if _, err := peer.Read(data[:]); err != io.EOF {
		t.Fatalf("late connection survived shutdown: %v", err)
	}
	if len(forwarder.connections) != 0 {
		t.Fatal("closed forwarder retained a late connection")
	}
}

func TestIngressQuotaAndWorkerLossAreBounded(t *testing.T) {
	listener := tcpListener(t)
	rule := ingressRule("dev", 3000)
	forwarder := NewIngressForwarder(nil, []RegisteredIngress{{Rule: rule, Listener: listener}}, NewFlowQuota(1), 1)
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	forwarder.dial = func(context.Context, config.Ingress) (net.Conn, error) {
		host, guest := net.Pipe()
		started <- struct{}{}
		go func() { <-release; guest.Close() }()
		return host, nil
	}
	forwarder.Start()
	first, _ := net.Dial("tcp", listener.Addr().String())
	defer first.Close()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("first ingress flow did not start")
	}
	second, _ := net.Dial("tcp", listener.Addr().String())
	defer second.Close()
	select {
	case <-started:
		t.Fatal("ingress quota admitted a second flow")
	case <-time.After(50 * time.Millisecond):
	}
	_ = second.SetReadDeadline(time.Now().Add(time.Second))
	var data [1]byte
	if _, err := second.Read(data[:]); err != io.EOF {
		t.Fatalf("exhausted ingress was queued instead of rejected: %v", err)
	}

	done := make(chan struct{})
	go func() { forwarder.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("worker loss did not close ingress promptly")
	}
	close(release)
	if _, err := net.DialTimeout("tcp", listener.Addr().String(), 100*time.Millisecond); err == nil {
		t.Fatal("worker loss left ingress accepting")
	}
}
