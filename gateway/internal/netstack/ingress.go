package netstack

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nueces/sbx/gateway/internal/config"
	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
)

const maxDiscardedIngress = 4096

var ErrIngressBacklogLimit = errors.New("ingress backlog drain limit exhausted")

type RegisteredIngress struct {
	Rule     config.Ingress
	Listener net.Listener
}

func RegisterIngress(cfg config.Config, specs []string) (_ []RegisteredIngress, err error) {
	rules := make(map[string]config.Ingress, len(cfg.Ingress))
	for _, rule := range cfg.Ingress {
		rules[rule.ID] = rule
	}
	registered := make([]RegisteredIngress, 0, len(specs))
	defer func() {
		if err != nil {
			for _, item := range registered {
				item.Listener.Close()
			}
		}
	}()
	seenRules := make(map[string]bool, len(specs))
	seenFDs := make(map[int]bool, len(specs))
	for _, spec := range specs {
		owner, rawFD, ok := strings.Cut(spec, "=")
		parts := strings.Split(owner, "/")
		fd, parseErr := strconv.Atoi(rawFD)
		if !ok || len(parts) != 3 || parseErr != nil || fd < 3 || parts[0] != cfg.VMID || parts[1] != cfg.Generation {
			return nil, errors.New("ingress descriptor ownership mismatch")
		}
		rule, found := rules[parts[2]]
		if !found || seenRules[rule.ID] || seenFDs[fd] {
			return nil, errors.New("unknown or duplicate ingress descriptor")
		}
		seenRules[rule.ID], seenFDs[fd] = true, true
		file := os.NewFile(uintptr(fd), "ingress-"+rule.ID)
		if file == nil {
			return nil, errors.New("invalid ingress descriptor")
		}
		listener, listenerErr := net.FileListener(file)
		file.Close()
		if listenerErr != nil {
			return nil, fmt.Errorf("ingress %q: %w", rule.ID, listenerErr)
		}
		address, tcp := listener.Addr().(*net.TCPAddr)
		if !tcp {
			listener.Close()
			return nil, fmt.Errorf("ingress %q is not a TCP listener", rule.ID)
		}
		actual, addressErr := netip.ParseAddr(address.IP.String())
		expected, _ := netip.ParseAddr(rule.ListenHost)
		if addressErr != nil || actual.Unmap() != expected.Unmap() || address.Port == 0 || rule.HostPort != 0 && address.Port != int(rule.HostPort) {
			listener.Close()
			return nil, fmt.Errorf("ingress %q did not bind its configured address", rule.ID)
		}
		registered = append(registered, RegisteredIngress{Rule: rule, Listener: listener})
	}
	if len(registered) != len(rules) {
		return nil, errors.New("missing ingress descriptor")
	}
	return registered, nil
}

type IngressForwarder struct {
	stack       *Network
	listeners   []RegisteredIngress
	total       *FlowQuota
	concurrent  chan struct{}
	dial        func(context.Context, config.Ingress) (net.Conn, error)
	ctx         context.Context
	cancel      context.CancelFunc
	wg          sync.WaitGroup
	mu          sync.Mutex
	connections map[net.Conn]struct{}
	active      atomic.Int64
	startOnce   sync.Once
	closeOnce   sync.Once
}

func NewIngressForwarder(network *Network, listeners []RegisteredIngress, total *FlowQuota, limit int) *IngressForwarder {
	ctx, cancel := context.WithCancel(context.Background())
	forwarder := &IngressForwarder{
		stack: network, listeners: listeners, total: total, concurrent: make(chan struct{}, limit),
		ctx: ctx, cancel: cancel, connections: make(map[net.Conn]struct{}),
	}
	forwarder.dial = forwarder.dialGuest
	return forwarder
}

func (f *IngressForwarder) DiscardPending() error {
	for _, item := range f.listeners {
		listener, ok := item.Listener.(*net.TCPListener)
		if !ok {
			return fmt.Errorf("ingress %q has a non-TCP listener", item.Rule.ID)
		}
		raw, err := listener.SyscallConn()
		if err != nil {
			return fmt.Errorf("access ingress %q: %w", item.Rule.ID, err)
		}
		var drainErr error
		err = raw.Control(func(fd uintptr) {
			flags, err := unix.FcntlInt(fd, unix.F_GETFL, 0)
			if err != nil {
				drainErr = err
				return
			}
			if flags&unix.O_NONBLOCK == 0 {
				drainErr = errors.New("ingress listener is blocking")
				return
			}
			for range maxDiscardedIngress {
				accepted, _, err := unix.Accept(int(fd))
				if errors.Is(err, unix.EINTR) || errors.Is(err, unix.ECONNABORTED) {
					continue // Interrupted or already-aborted accepts expose no live connection.
				}
				if errors.Is(err, unix.EAGAIN) || errors.Is(err, unix.EWOULDBLOCK) {
					return // Only a genuinely empty kernel backlog completes the drain.
				}
				if err != nil {
					drainErr = err
					return
				}
				if err := unix.Close(accepted); err != nil {
					drainErr = err
					return
				}
			}
			drainErr = ErrIngressBacklogLimit
		})
		if err != nil {
			return fmt.Errorf("control ingress %q: %w", item.Rule.ID, err)
		}
		if drainErr != nil {
			return fmt.Errorf("drain ingress %q: %w", item.Rule.ID, drainErr)
		}
	}
	return nil
}

func (f *IngressForwarder) Start() {
	f.startOnce.Do(func() {
		for _, item := range f.listeners {
			f.wg.Add(1)
			go f.accept(item)
		}
	})
}

func (f *IngressForwarder) accept(item RegisteredIngress) {
	defer f.wg.Done()
	for {
		conn, err := item.Listener.Accept()
		if err != nil {
			return
		}
		select {
		case <-f.ctx.Done():
			conn.Close()
			return
		case f.concurrent <- struct{}{}:
		default:
			conn.Close()
			continue
		}
		if !f.total.acquire() {
			<-f.concurrent
			conn.Close()
			continue
		}
		f.track(conn, true)
		f.active.Add(1)
		f.wg.Add(1)
		go f.forward(conn, item.Rule)
	}
}

func (f *IngressForwarder) forward(host net.Conn, rule config.Ingress) {
	defer f.wg.Done()
	defer f.total.release()
	defer func() { <-f.concurrent }()
	defer f.active.Add(-1)
	defer f.track(host, false)
	ctx, cancel := context.WithTimeout(f.ctx, 10*time.Second)
	guest, err := f.dial(ctx, rule)
	cancel()
	if err != nil {
		host.Close()
		return
	}
	f.track(guest, true)
	relay(host, guest)
	f.track(guest, false)
}

func (f *IngressForwarder) dialGuest(ctx context.Context, rule config.Ingress) (net.Conn, error) {
	address := net.ParseIP(rule.GuestAddress).To4()
	if address == nil {
		return nil, errors.New("invalid guest ingress address")
	}
	return gonet.DialContextTCP(ctx, f.stack.Stack, tcpip.FullAddress{
		NIC: NICID, Addr: tcpip.AddrFrom4Slice(address), Port: rule.GuestPort,
	}, ipv4.ProtocolNumber)
}

func (f *IngressForwarder) track(conn net.Conn, add bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if add {
		// Cancellation can race with an accepted connection or guest dial completing.
		if f.ctx.Err() != nil {
			conn.Close()
			return
		}
		f.connections[conn] = struct{}{}
	} else {
		delete(f.connections, conn)
	}
}

func (f *IngressForwarder) Active() int { return int(f.active.Load()) }

func (f *IngressForwarder) Close() {
	f.closeOnce.Do(func() {
		f.cancel()
		for _, item := range f.listeners {
			item.Listener.Close()
		}
		f.mu.Lock()
		connections := make([]net.Conn, 0, len(f.connections))
		for conn := range f.connections {
			connections = append(connections, conn)
		}
		f.mu.Unlock()
		for _, conn := range connections {
			conn.Close()
		}
		f.wg.Wait()
	})
}
