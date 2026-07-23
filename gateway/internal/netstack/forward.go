package netstack

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

type TCPForwarder struct {
	config     config.Config
	resolver   *syntheticdns.Resolver
	total      chan struct{}
	mu         sync.Mutex
	perDest    map[string]int
	dialer     net.Dialer
	uploaded   atomic.Uint64
	downloaded atomic.Uint64
}

type ForwardStats struct {
	Uploaded, Downloaded uint64
}

func (f *TCPForwarder) Stats() ForwardStats {
	return ForwardStats{Uploaded: f.uploaded.Load(), Downloaded: f.downloaded.Load()}
}

func (n *Network) ForwardTCP(cfg config.Config, resolver *syntheticdns.Resolver) *TCPForwarder {
	f := &TCPForwarder{
		config: cfg, resolver: resolver, total: make(chan struct{}, cfg.Quotas.TCPFlows),
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
	rule, allowed := f.config.Match("tcp", host, id.LocalPort)
	if !allowed || !f.acquire(host) {
		if synthetic {
			f.resolver.Release(ip)
		}
		request.Complete(true)
		return
	}
	go f.forward(request, rule, host, id.LocalPort, ip, synthetic)
}

func (f *TCPForwarder) acquire(host string) bool {
	select {
	case f.total <- struct{}{}:
	default:
		return false
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.perDest[host] >= f.config.Quotas.FlowsPerDestination {
		<-f.total
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
	<-f.total
}

func (f *TCPForwarder) forward(request *tcp.ForwarderRequest, rule config.Rule, host string, port uint16, syntheticIP netip.Addr, synthetic bool) {
	defer f.release(host)
	if synthetic {
		defer f.resolver.Release(syntheticIP)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	upstream, err := f.dial(ctx, rule, host, port)
	if err != nil {
		request.Complete(true)
		return
	}
	var wq waiter.Queue
	endpoint, tcpErr := request.CreateEndpoint(&wq)
	request.Complete(tcpErr != nil)
	if tcpErr != nil {
		upstream.Close()
		return
	}
	guest := gonet.NewTCPConn(&wq, endpoint)
	uploaded, downloaded := relay(guest, upstream)
	f.uploaded.Add(uploaded)
	f.downloaded.Add(downloaded)
}

func (f *TCPForwarder) dial(ctx context.Context, rule config.Rule, host string, port uint16) (net.Conn, error) {
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

type closeWriter interface{ CloseWrite() error }

func relay(guest, upstream net.Conn) (uint64, uint64) {
	var wg sync.WaitGroup
	var uploaded, downloaded uint64
	copyOne := func(dst, src net.Conn, count *uint64) {
		defer wg.Done()
		*count, _ = copyIdle(dst, src, 600*time.Second)
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

func copyIdle(dst, src net.Conn, idle time.Duration) (uint64, error) {
	buffer := make([]byte, 32*1024)
	var copied uint64
	for {
		_ = src.SetReadDeadline(time.Now().Add(idle))
		n, err := src.Read(buffer)
		if n != 0 {
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
