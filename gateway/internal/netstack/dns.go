package netstack

import (
	"encoding/binary"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
)

const (
	dnsQueriesPerSecond = 100
	dnsQueryBurst       = 200
	maxOutstandingDNS   = 64
	dnsQueryTimeout     = 5 * time.Second
)

type dnsLimiter struct {
	mu     sync.Mutex
	last   time.Time
	tokens float64
	denied atomic.Uint64
}

func newDNSLimiter(now time.Time) *dnsLimiter {
	return &dnsLimiter{last: now, tokens: dnsQueryBurst}
}

func (l *dnsLimiter) allow(now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.tokens = min(dnsQueryBurst, l.tokens+now.Sub(l.last).Seconds()*dnsQueriesPerSecond)
	l.last = now
	if l.tokens < 1 {
		l.denied.Add(1)
		return false
	}
	l.tokens--
	return true
}

type DNSService struct {
	net.PacketConn
	limiter *dnsLimiter
	active  atomic.Int64
}

func (s *DNSService) Rejected() uint64 { return s.limiter.denied.Load() }
func (s *DNSService) Active() int64    { return s.active.Load() }

func (n *Network) ServeDNS(resolver *syntheticdns.Resolver) (*DNSService, error) {
	conn, err := gonet.DialUDP(n.Stack, &tcpip.FullAddress{
		NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4()), Port: 53,
	}, nil, ipv4.ProtocolNumber)
	if err != nil {
		return nil, err
	}
	service := &DNSService{PacketConn: conn, limiter: newDNSLimiter(time.Now())}
	outstanding := make(chan struct{}, maxOutstandingDNS)
	var addresses sync.Mutex
	known := make(map[string]bool)
	for _, mapping := range resolver.Snapshot() {
		known[mapping.IP] = true
	}
	go func() {
		buf := make([]byte, 1232)
		for {
			read, peer, err := conn.ReadFrom(buf)
			if err != nil {
				return
			}
			if !service.limiter.allow(time.Now()) {
				continue
			}
			select {
			case outstanding <- struct{}{}:
			default:
				service.limiter.denied.Add(1)
				continue
			}
			packet := append([]byte(nil), buf[:read]...)
			service.active.Add(1)
			go func() {
				defer func() { <-outstanding; service.active.Add(-1) }()
				deadline := time.Now().Add(dnsQueryTimeout)
				reply := resolver.Reply(packet)
				if len(reply) == 0 || time.Now().After(deadline) {
					return
				}
				if binary.BigEndian.Uint16(reply[6:8]) == 1 {
					ip := net.IP(reply[len(reply)-4:]).String()
					addresses.Lock()
					if !known[ip] {
						if err := n.AddLocalAddress(net.ParseIP(ip)); err != nil {
							addresses.Unlock()
							return
						}
						known[ip] = true
					}
					addresses.Unlock()
				}
				_ = conn.SetWriteDeadline(deadline)
				_, _ = conn.WriteTo(reply, peer)
			}()
		}
	}()
	return service, nil
}
