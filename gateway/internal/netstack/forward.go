package netstack

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

type Mediator struct {
	address, vmID, incarnation string
	issuer                     *capability.Issuer
	dialer                     net.Dialer
}

const (
	pendingWriteBytes      = 32 * 1024
	maxAggregateRelayBytes = 64 * 1024 * 1024
	rawTCPIdle             = 600 * time.Second
	httpIdle               = 300 * time.Second
)

type FlowQuota struct {
	tokens   chan struct{}
	rejected atomic.Uint64
}

func NewFlowQuota(limit int) *FlowQuota {
	if limit < 1 || pendingWriteBytes*limit*2 > maxAggregateRelayBytes {
		panic("flow quota exceeds aggregate relay buffer ceiling")
	}
	return &FlowQuota{tokens: make(chan struct{}, limit)}
}

func (q *FlowQuota) acquire() bool {
	select {
	case q.tokens <- struct{}{}:
		return true
	default:
		q.rejected.Add(1)
		return false
	}
}

func (q *FlowQuota) release()         { <-q.tokens }
func (q *FlowQuota) Active() int      { return len(q.tokens) }
func (q *FlowQuota) Rejected() uint64 { return q.rejected.Load() }

type TCPForwarder struct {
	config        config.Config
	resolver      *syntheticdns.Resolver
	mediator      *Mediator
	total         *FlowQuota
	mu            sync.Mutex
	perDest       map[string]int
	dialer        net.Dialer
	uploaded      atomic.Uint64
	downloaded    atomic.Uint64
	perDestDenied atomic.Uint64
}

type ForwardStats struct {
	Uploaded, Downloaded, PerDestinationRejected uint64
}

func (f *TCPForwarder) Stats() ForwardStats {
	return ForwardStats{f.uploaded.Load(), f.downloaded.Load(), f.perDestDenied.Load()}
}

func NewMediator(address string, key []byte, vmID, incarnation string) (*Mediator, error) {
	host, port, err := net.SplitHostPort(address)
	ip := net.ParseIP(host)
	portNumber, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || ip == nil || !ip.IsLoopback() || portErr != nil || portNumber == 0 {
		return nil, errors.New("mediator must use a loopback TCP endpoint")
	}
	issuer, err := capability.New(key, vmID, incarnation)
	if err != nil {
		return nil, err
	}
	return &Mediator{address: address, vmID: vmID, incarnation: incarnation, issuer: issuer, dialer: net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}}, nil
}

func (n *Network) ForwardTCP(cfg config.Config, resolver *syntheticdns.Resolver, mediator *Mediator, total *FlowQuota) *TCPForwarder {
	f := &TCPForwarder{
		config: cfg, resolver: resolver, mediator: mediator, total: total,
		perDest: make(map[string]int), dialer: net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second},
	}
	forwarder := tcp.NewForwarder(n.Stack, 0, cfg.Quotas.TCPFlows, f.handle)
	n.Stack.SetTransportProtocolHandler(tcp.ProtocolNumber, forwarder.HandlePacket)
	return f
}

func (f *TCPForwarder) handle(request *tcp.ForwarderRequest) {
	id := request.ID()
	ip, ok := netip.AddrFromSlice(id.LocalAddress.AsSlice())
	if !ok {
		request.Complete(true)
		return
	}
	host := ip.String()
	synthetic := false
	if name, found := f.resolver.Acquire(ip); found {
		host, synthetic = name, true
	}
	rule, allowed := f.config.Match("http", host, id.LocalPort)
	if !allowed {
		rule, allowed = f.config.Match("tls", host, id.LocalPort)
	}
	if !allowed {
		rule, allowed = f.config.Match("tcp", host, id.LocalPort)
	}
	if !allowed || rule.Protocol == "http" && f.mediator == nil || !f.acquire(host) {
		if synthetic {
			f.resolver.Release(ip)
		}
		request.Complete(true)
		return
	}
	go f.forward(request, rule, host, id.LocalPort, ip, synthetic)
}

func (f *TCPForwarder) acquire(host string) bool {
	if !f.total.acquire() {
		return false
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.perDest[host] >= f.config.Quotas.FlowsPerDestination {
		f.perDestDenied.Add(1)
		f.total.release()
		return false
	}
	f.perDest[host]++
	return true
}

func (f *TCPForwarder) release(host string) {
	f.mu.Lock()
	f.perDest[host]--
	if f.perDest[host] == 0 {
		delete(f.perDest, host)
	}
	f.mu.Unlock()
	f.total.release()
}

func (f *TCPForwarder) forward(request *tcp.ForwarderRequest, rule config.Rule, host string, port uint16, syntheticIP netip.Addr, synthetic bool) {
	defer f.release(host)
	if synthetic {
		defer f.resolver.Release(syntheticIP)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var wq waiter.Queue
	endpoint, tcpErr := request.CreateEndpoint(&wq)
	request.Complete(tcpErr != nil)
	if tcpErr != nil {
		return
	}
	guest := gonet.NewTCPConn(&wq, endpoint)
	if rule.Protocol == "tls" {
		uploaded, downloaded, err := f.passthrough(ctx, guest, rule, host, port)
		if err == nil {
			f.uploaded.Add(uploaded)
			f.downloaded.Add(downloaded)
		}
		guest.Close()
		return
	}
	upstream, err := f.dial(ctx, rule, host, port)
	if err != nil {
		guest.Close()
		return
	}
	idle := rawTCPIdle
	if rule.Protocol == "http" {
		idle = httpIdle
	}
	uploaded, downloaded := relayIdle(guest, upstream, idle)
	f.uploaded.Add(uploaded)
	f.downloaded.Add(downloaded)
}

func (f *TCPForwarder) passthrough(ctx context.Context, guest net.Conn, rule config.Rule, host string, port uint16) (uint64, uint64, error) {
	if deadline, ok := ctx.Deadline(); ok {
		_ = guest.SetReadDeadline(deadline)
	}
	prefix, sni, err := readClientHello(guest)
	if err != nil || !strings.EqualFold(strings.TrimSuffix(sni, "."), host) {
		return 0, 0, errors.New("TLS passthrough SNI mismatch")
	}
	upstream, err := f.dial(ctx, rule, host, port)
	if err != nil {
		return 0, 0, err
	}
	_ = guest.SetReadDeadline(time.Time{})
	if err := writeAll(upstream, prefix); err != nil {
		upstream.Close()
		return 0, 0, err
	}
	uploaded, downloaded := relayIdle(guest, upstream, rawTCPIdle)
	return uploaded + uint64(len(prefix)), downloaded, nil
}

func (f *TCPForwarder) dial(ctx context.Context, rule config.Rule, host string, port uint16) (net.Conn, error) {
	if rule.Protocol == "http" {
		return f.mediator.Dial(ctx, rule, host, port)
	}
	addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return nil, err
	}
	for _, ip := range addresses {
		if !rule.AllowsIP(ip) {
			continue
		}
		conn, err := f.dialer.DialContext(ctx, "tcp", net.JoinHostPort(ip.String(), strconv.Itoa(int(port))))
		if err == nil {
			return conn, nil
		}
	}
	return nil, fmt.Errorf("no permitted address for %s", host)
}

func (m *Mediator) Dial(ctx context.Context, rule config.Rule, host string, port uint16) (net.Conn, error) {
	conn, err := m.dialer.DialContext(ctx, "tcp", m.address)
	if err != nil {
		return nil, err
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	var nonce [12]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		conn.Close()
		return nil, err
	}
	username, password, err := m.issuer.Issue(capability.Metadata{
		VMID: m.vmID, Incarnation: m.incarnation, FlowID: base64.RawURLEncoding.EncodeToString(nonce[:]),
		RuleID: rule.ID, Host: host, Port: port,
	})
	if err != nil || len(username) > 255 || len(password) > 255 || len(host) > 255 {
		conn.Close()
		return nil, errors.New("cannot create mediator capability")
	}
	packets := [][]byte{
		{5, 1, 2},
		append(append([]byte{1, byte(len(username))}, username...), append([]byte{byte(len(password))}, password...)...),
		append(append([]byte{5, 1, 0, 3, byte(len(host))}, host...), byte(port>>8), byte(port)),
	}
	responses := [][]byte{{5, 2}, {1, 0}}
	for index, packet := range packets {
		if err := writeAll(conn, packet); err != nil {
			conn.Close()
			return nil, err
		}
		if index < len(responses) {
			response := make([]byte, len(responses[index]))
			if _, err := io.ReadFull(conn, response); err != nil || !bytes.Equal(response, responses[index]) {
				conn.Close()
				return nil, errors.New("mediator authentication failed")
			}
		}
	}
	if err := readSOCKSReply(conn); err != nil {
		conn.Close()
		return nil, err
	}
	_ = conn.SetDeadline(time.Time{})
	return conn, nil
}

func writeAll(writer io.Writer, data []byte) error {
	for len(data) != 0 {
		n, err := writer.Write(data)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		data = data[n:]
	}
	return nil
}

func readSOCKSReply(reader io.Reader) error {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil || header[0] != 5 || header[1] != 0 || header[2] != 0 {
		return errors.New("mediator denied destination")
	}
	length := 0
	switch header[3] {
	case 1:
		length = 4
	case 3:
		var size [1]byte
		if _, err := io.ReadFull(reader, size[:]); err != nil {
			return err
		}
		length = int(size[0])
	case 4:
		length = 16
	default:
		return errors.New("invalid mediator response")
	}
	var discard [258]byte
	_, err := io.ReadFull(reader, discard[:length+2])
	return err
}

type closeWriter interface{ CloseWrite() error }

func relay(guest, upstream net.Conn) (uint64, uint64) {
	return relayIdle(guest, upstream, rawTCPIdle)
}

func relayIdle(guest, upstream net.Conn, idle time.Duration) (uint64, uint64) {
	var wg sync.WaitGroup
	var uploaded, downloaded uint64
	var activity sync.Mutex
	touch := func() {
		activity.Lock()
		defer activity.Unlock()
		deadline := time.Now().Add(idle)
		_ = guest.SetReadDeadline(deadline)
		_ = upstream.SetReadDeadline(deadline)
	}
	touch()
	copyOne := func(dst, src net.Conn, count *uint64) {
		defer wg.Done()
		*count, _ = copyIdle(dst, src, touch)
		if closer, ok := dst.(closeWriter); ok {
			_ = closer.CloseWrite()
		}
	}
	wg.Add(2)
	go copyOne(upstream, guest, &uploaded)
	go copyOne(guest, upstream, &downloaded)
	wg.Wait()
	_ = guest.Close()
	_ = upstream.Close()
	return uploaded, downloaded
}

func copyIdle(dst, src net.Conn, touch func()) (uint64, error) {
	buffer := make([]byte, pendingWriteBytes)
	var copied uint64
	for {
		n, err := src.Read(buffer)
		if n != 0 {
			touch()
			_ = dst.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if _, writeErr := io.CopyN(dst, bytes.NewReader(buffer[:n]), int64(n)); writeErr != nil {
				return copied, writeErr
			}
			copied += uint64(n)
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return copied, nil
			}
			return copied, err
		}
	}
}
