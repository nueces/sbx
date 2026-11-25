package netstack

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

type defaultHarness struct {
	gateway, guest *Network
	forwarder      *TCPForwarder
	resolver       *syntheticdns.Resolver
	mu             sync.Mutex
	dialed         []string
	lookups        map[string][]string
}

// newDefaultHarness connects a guest stack routed via the gateway address, as a VM is.
func newDefaultHarness(t *testing.T, cfg config.Config, allow bool) *defaultHarness {
	t.Helper()
	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { echo.Close() })
	go func() {
		for {
			conn, err := echo.Accept()
			if err != nil {
				return
			}
			go func() { defer conn.Close(); _, _ = io.Copy(conn, conn) }()
		}
	}()
	gateway, err := New()
	if err != nil {
		t.Fatal(err)
	}
	guest, err := New()
	if err != nil {
		t.Fatal(err)
	}
	gatewayAddr := tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4())
	if err := guest.Stack.RemoveAddress(NICID, gatewayAddr); err != nil {
		t.Fatal(err)
	}
	if err := guest.Stack.AddProtocolAddress(NICID, tcpip.ProtocolAddress{
		Protocol:          ipv4.ProtocolNumber,
		AddressWithPrefix: tcpip.AddressWithPrefix{Address: tcpip.AddrFrom4Slice(net.ParseIP(GuestIP).To4()), PrefixLen: 24},
	}, stack.AddressProperties{}); err != nil {
		t.Fatal(err)
	}
	guest.Stack.SetRouteTable([]tcpip.Route{
		{Destination: mustSubnet(t, "192.0.2.0/24"), NIC: NICID},
		{Destination: header.IPv4EmptySubnet, Gateway: gatewayAddr, NIC: NICID},
	})
	if allow {
		cfg.Default = config.DefaultAllow
		if err := gateway.EnableDefaultRouting(); err != nil {
			t.Fatal(err)
		}
	}
	if cfg.Quotas == (config.Quotas{}) {
		cfg.Quotas = config.DefaultQuotas()
	}
	h := &defaultHarness{gateway: gateway, guest: guest, lookups: map[string][]string{}}
	h.resolver = syntheticdns.New(cfg)
	for _, rule := range cfg.Rules {
		if _, err := netip.ParseAddr(rule.Host); err != nil {
			ip, err := h.resolver.Resolve(rule.Host)
			if err != nil {
				t.Fatal(err)
			}
			if err := gateway.AddLocalAddress(net.IP(ip.AsSlice())); err != nil {
				t.Fatal(err)
			}
		}
	}
	h.forwarder = gateway.ForwardTCP(cfg, h.resolver, nil, NewFlowQuota(cfg.Quotas.TCPFlows))
	h.forwarder.lookup = func(_ context.Context, _ string, host string) ([]netip.Addr, error) {
		h.mu.Lock()
		defer h.mu.Unlock()
		values, ok := h.lookups[host]
		if !ok {
			return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
		}
		var result []netip.Addr
		for _, value := range values {
			result = append(result, netip.MustParseAddr(value))
		}
		return result, nil
	}
	h.forwarder.defaultDial = func(ctx context.Context, network, address string) (net.Conn, error) {
		h.mu.Lock()
		h.dialed = append(h.dialed, address)
		h.mu.Unlock()
		var dialer net.Dialer
		return dialer.DialContext(ctx, network, echo.Addr().String())
	}
	ctx, cancel := context.WithCancel(context.Background())
	var pumps sync.WaitGroup
	for _, pair := range [][2]*Network{{gateway, guest}, {guest, gateway}} {
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
	t.Cleanup(func() {
		cancel()
		pumps.Wait()
		guest.Close()
		gateway.Close()
	})
	return h
}

func (h *defaultHarness) dial(ip string, port uint16, timeout time.Duration) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	conn, err := gonet.DialTCPWithBind(ctx, h.guest.Stack,
		tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GuestIP).To4())},
		tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(ip).To4()), Port: port},
		ipv4.ProtocolNumber)
	if err != nil {
		return nil, err // avoid a typed-nil net.Conn
	}
	return conn, nil
}

func (h *defaultHarness) dialedAddresses() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.dialed...)
}

func echoes(t *testing.T, conn net.Conn) {
	t.Helper()
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 4)
	if _, err := io.ReadFull(conn, reply); err != nil || string(reply) != "ping" {
		t.Fatalf("relay failed: %q %v", reply, err)
	}
}

func mustSubnet(t *testing.T, value string) tcpip.Subnet {
	t.Helper()
	prefix := netip.MustParsePrefix(value)
	subnet, err := tcpip.NewSubnet(tcpip.AddrFrom4(prefix.Addr().As4()), tcpip.MaskFromBytes(net.CIDRMask(prefix.Bits(), 32)))
	if err != nil {
		t.Fatal(err)
	}
	return subnet
}

func TestDefaultAllowForwardsArbitraryLiteralIPv4(t *testing.T) {
	h := newDefaultHarness(t, config.Config{}, true)
	for _, target := range []string{"93.184.216.34", "10.1.2.3", "192.168.7.8", "172.16.9.9"} {
		conn, err := h.dial(target, 8443, time.Second)
		if err != nil {
			t.Fatalf("%s: %v", target, err)
		}
		echoes(t, conn)
	}
	got := h.dialedAddresses()
	want := []string{"93.184.216.34:8443", "10.1.2.3:8443", "192.168.7.8:8443", "172.16.9.9:8443"}
	if !slices.Equal(got, want) {
		t.Fatalf("dialed %v, want %v", got, want)
	}
	if h.forwarder.Stats().DefaultForwarded != 4 || h.resolver.Len() != 0 {
		t.Fatalf("stats %+v mappings %d", h.forwarder.Stats(), h.resolver.Len())
	}
}

func TestDefaultAllowRejectsProtectedLiterals(t *testing.T) {
	h := newDefaultHarness(t, config.Config{}, true)
	for _, target := range []string{
		"169.254.169.254", "100.100.100.200", "100.64.1.1", "168.63.129.16", "198.18.0.77",
		"192.0.2.50", "192.0.2.1", "0.1.2.3", "240.0.0.1",
	} {
		conn, err := h.dial(target, 80, time.Second)
		if conn != nil {
			conn.Close()
		}
		if err == nil {
			t.Fatalf("%s was admitted", target)
		}
	}
	if dialed := h.dialedAddresses(); len(dialed) != 0 {
		t.Fatalf("protected destinations reached the upstream dialer: %v", dialed)
	}
	if h.forwarder.Stats().DefaultDenied == 0 {
		t.Fatal("default denials not counted")
	}
}

func TestDefaultAllowResolvesSyntheticNamesAndFiltersRebinding(t *testing.T) {
	cfg := config.Config{Rules: []config.Rule{
		{ID: "git", Protocol: "tcp", Host: "api.example", Ports: []uint16{22}},
		{ID: "rebind", Protocol: "tcp", Host: "rebind.example", Ports: []uint16{22}},
	}}
	h := newDefaultHarness(t, cfg, true)
	h.lookups["api.example"] = []string{"127.0.0.1", "169.254.169.254", "93.184.216.34"}
	h.lookups["rebind.example"] = []string{"169.254.169.254", "127.0.0.1", "100.100.100.200"}
	api, _ := h.resolver.Resolve("api.example")
	rebind, _ := h.resolver.Resolve("rebind.example")

	conn, err := h.dial(api.String(), 443, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	echoes(t, conn)
	if dialed := h.dialedAddresses(); len(dialed) != 1 || dialed[0] != "93.184.216.34:443" {
		t.Fatalf("hostname default dialed %v", dialed)
	}

	// Resolution happens before the handshake completes; a protected-only answer is refused.
	if conn, err := h.dial(rebind.String(), 443, time.Second); err == nil {
		conn.Close()
		t.Fatal("rebinding destination was admitted")
	}
	if dialed := h.dialedAddresses(); len(dialed) != 1 {
		t.Fatalf("protected rebinding answer was dialed: %v", dialed)
	}
	if h.forwarder.Stats().DefaultDenied == 0 {
		t.Fatal("rebinding denial not counted")
	}
}

func TestDefaultAllowRefusesUnreachableDestinations(t *testing.T) {
	h := newDefaultHarness(t, config.Config{}, true)
	h.forwarder.defaultDial = func(context.Context, string, string) (net.Conn, error) {
		return nil, errors.New("connection refused")
	}
	if conn, err := h.dial("10.0.0.5", 5432, 2*time.Second); err == nil {
		conn.Close()
		t.Fatal("a dead port on a permitted address looked open to the guest")
	}
	if stats := h.forwarder.Stats(); stats.DefaultForwarded != 0 || stats.DefaultDenied != 0 {
		t.Fatalf("unreachable is neither forwarded nor a policy denial: %+v", stats)
	}
}

func TestExplicitMatchesNeverFallThroughToDefault(t *testing.T) {
	cfg := config.Config{Rules: []config.Rule{
		{ID: "api", Protocol: "http", Host: "api.example", Ports: []uint16{443}},
		{ID: "pinned", Protocol: "tls", Host: "pinned.example", Ports: []uint16{443}},
	}}
	h := newDefaultHarness(t, cfg, true)
	h.lookups["api.example"] = []string{"93.184.216.34"}
	h.lookups["pinned.example"] = []string{"93.184.216.34"}
	api, _ := h.resolver.Resolve("api.example")
	pinned, _ := h.resolver.Resolve("pinned.example")

	// HTTP rule without a mediator: rejected, never forwarded token-free.
	if conn, err := h.dial(api.String(), 443, time.Second); err == nil {
		conn.Close()
		t.Fatal("HTTP rule without mediator was admitted")
	}
	// TLS rule with a non-TLS/SNI-mismatched stream: closed by passthrough, not defaulted.
	conn, err := h.dial(pinned.String(), 443, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.SetDeadline(time.Now().Add(12 * time.Second))
	_, _ = conn.Write([]byte("not a client hello"))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("SNI mismatch relayed data")
	}
	conn.Close()
	if dialed := h.dialedAddresses(); len(dialed) != 0 {
		t.Fatalf("explicit failures fell through to the default route: %v", dialed)
	}
	// The same host on an unmatched port uses the default route.
	conn, err = h.dial(api.String(), 80, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	echoes(t, conn)
	if dialed := h.dialedAddresses(); len(dialed) != 1 || dialed[0] != "93.184.216.34:80" {
		t.Fatalf("unmatched port dialed %v", dialed)
	}
}

func TestDefaultAllowKeepsPerDestinationQuota(t *testing.T) {
	quotas := config.DefaultQuotas()
	quotas.FlowsPerDestination = 1
	h := newDefaultHarness(t, config.Config{Quotas: quotas}, true)
	first, err := h.dial("93.184.216.34", 443, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if second, err := h.dial("93.184.216.34", 443, time.Second); err == nil {
		second.Close()
		t.Fatal("per-destination quota ignored by default forwarding")
	}
	other, err := h.dial("93.184.216.35", 443, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	echoes(t, other)
}

func TestDenyModeKeepsDroppingUnregisteredDestinations(t *testing.T) {
	h := newDefaultHarness(t, config.Config{}, false)
	conn, err := h.dial("93.184.216.34", 443, 300*time.Millisecond)
	if conn != nil {
		conn.Close()
	}
	if err == nil {
		t.Fatal("deny mode admitted an unregistered destination")
	}
	// Deny mode drops silently (no RST): the dial times out rather than being refused.
	if !errors.Is(err, context.DeadlineExceeded) && !isTimeout(err) {
		t.Fatalf("deny mode changed packet behavior: %v", err)
	}
	if len(h.dialedAddresses()) != 0 || h.forwarder.Stats().DefaultDenied != 0 {
		t.Fatal("deny mode used the default route")
	}
}

func TestDefaultAllowDropsUnmatchedUDPSilently(t *testing.T) {
	h := newDefaultHarness(t, config.Config{}, true)
	conn, err := gonet.DialUDP(h.guest.Stack,
		&tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GuestIP).To4())},
		&tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP("93.184.216.34").To4()), Port: 9999},
		ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	for range 3 {
		if _, err := conn.Write([]byte("probe")); err != nil {
			t.Fatal(err)
		}
	}
	time.Sleep(100 * time.Millisecond)
	if sent := h.gateway.Stack.Stats().ICMP.V4.PacketsSent.DstUnreachable.Value(); sent != 0 {
		t.Fatalf("gateway emitted %d unreachable replies for arbitrary UDP destinations", sent)
	}
}

func isTimeout(err error) bool {
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

func TestDefaultAllowDNSServesRealAnswersWithoutRegisteringThem(t *testing.T) {
	cfg := config.Config{Rules: []config.Rule{{ID: "git", Protocol: "tcp", Host: "api.example", Ports: []uint16{22}}}}
	h := newDefaultHarness(t, cfg, true)
	h.resolver.SetDefaultLookup(func(context.Context, string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("93.184.216.34")}, nil
	}, config.DefaultAllowsIP)
	service, err := h.gateway.ServeDNS(h.resolver)
	if err != nil {
		t.Fatal(err)
	}
	defer service.Close()
	conn, err := gonet.DialUDP(h.guest.Stack,
		&tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GuestIP).To4())},
		&tcpip.FullAddress{NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4()), Port: 53},
		ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	ask := func(name string) []byte {
		t.Helper()
		packet := []byte{0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0}
		for _, label := range []string{name[:len(name)-len(".example")], "example"} {
			packet = append(packet, byte(len(label)))
			packet = append(packet, label...)
		}
		packet = append(packet, 0, 0, 1, 0, 1)
		if _, err := conn.Write(packet); err != nil {
			t.Fatal(err)
		}
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		reply := make([]byte, 512)
		n, err := conn.Read(reply)
		if err != nil {
			t.Fatal(err)
		}
		return reply[:n]
	}
	real := ask("open.example")
	if real[7] != 1 || [4]byte(real[len(real)-4:]) != [4]byte{93, 184, 216, 34} {
		t.Fatalf("real answer %v", real)
	}
	synthetic := ask("api.example")
	if synthetic[7] != 1 || synthetic[len(synthetic)-4] != 198 {
		t.Fatalf("policy name lost its synthetic answer: %v", synthetic)
	}
	for _, address := range h.gateway.Stack.AllAddresses()[NICID] {
		if address.AddressWithPrefix.Address == tcpip.AddrFrom4([4]byte{93, 184, 216, 34}) {
			t.Fatal("real answer was registered as a stack address")
		}
	}
	if h.resolver.Len() != 1 {
		t.Fatalf("mappings = %d", h.resolver.Len())
	}
}

type countingListener struct {
	net.Listener
	accepted atomic.Int64
}

func (l *countingListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err == nil {
		l.accepted.Add(1)
	}
	return conn, err
}

func TestDefaultRouteNeverContactsMediator(t *testing.T) {
	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	listener := &countingListener{Listener: raw}
	defer listener.Close()
	key := []byte(strings.Repeat("k", capability.KeySize))
	results := make(chan error, 8)
	// One SOCKS session at most; closing the listener ends the server otherwise.
	go fakeSOCKSServer(listener, key, results)
	mediator, err := NewMediator(raw.Addr().String(), key, "vm1", "worker1")
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{Rules: []config.Rule{{ID: "api", Protocol: "http", Host: "api.example", Ports: []uint16{443}}}}
	h := newDefaultHarness(t, cfg, true)
	h.forwarder.mediator = mediator
	h.lookups["api.example"] = []string{"93.184.216.34"}
	h.lookups["open.example"] = []string{"93.184.216.35"}
	api, _ := h.resolver.Resolve("api.example")

	// Unmatched literal and unmatched port on a policy host: raw default route only.
	for _, target := range []struct {
		ip   string
		port uint16
	}{{"93.184.216.34", 443}, {"10.1.2.3", 443}, {api.String(), 8443}} {
		conn, err := h.dial(target.ip, target.port, time.Second)
		if err != nil {
			t.Fatalf("%s:%d: %v", target.ip, target.port, err)
		}
		echoes(t, conn)
	}
	if got := listener.accepted.Load(); got != 0 {
		t.Fatalf("default route contacted the mediator %d times", got)
	}
	if forwarded := h.forwarder.Stats().DefaultForwarded; forwarded != 3 {
		t.Fatalf("default forwarded %d flows", forwarded)
	}

	// Positive control: the matched HTTP-mediated flow does reach the mediator.
	conn, err := h.dial(api.String(), 443, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	select {
	case err := <-results:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("matched HTTP flow never reached the mediator")
	}
	if got := listener.accepted.Load(); got != 1 {
		t.Fatalf("mediator accepted %d connections, want 1", got)
	}
	if dialed := h.dialedAddresses(); len(dialed) != 3 {
		t.Fatalf("matched flow used the default dialer: %v", dialed)
	}
}
