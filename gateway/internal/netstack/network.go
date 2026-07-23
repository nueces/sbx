package netstack

import (
	"context"
	"errors"
	"fmt"
	"net"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/link/ethernet"
	"gvisor.dev/gvisor/pkg/tcpip/network/arp"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
)

const (
	NICID     = 1
	MTU       = 1500
	GatewayIP = "192.0.2.1"
	GuestIP   = "192.0.2.2"
)

var GatewayMAC = tcpip.LinkAddress("\x52\x54\x00\x12\x34\x01")

type Network struct {
	Stack *stack.Stack
	link  *channel.Endpoint
}

func New() (*Network, error) {
	s := stack.New(stack.Options{
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol, arp.NewProtocol},
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol, udp.NewProtocol},
	})
	link := channel.New(1024, MTU, GatewayMAC)
	if err := s.CreateNIC(NICID, ethernet.New(link)); err != nil {
		s.Destroy()
		return nil, errors.New(err.String())
	}
	gateway := tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4())
	if err := s.AddProtocolAddress(NICID, tcpip.ProtocolAddress{
		Protocol:          ipv4.ProtocolNumber,
		AddressWithPrefix: tcpip.AddressWithPrefix{Address: gateway, PrefixLen: 24},
	}, stack.AddressProperties{}); err != nil {
		s.Destroy()
		return nil, errors.New(err.String())
	}
	s.SetRouteTable([]tcpip.Route{{Destination: header.IPv4EmptySubnet, NIC: NICID}})
	return &Network{Stack: s, link: link}, nil
}

func (n *Network) Close() {
	n.link.Close()
	n.Stack.Destroy()
}

func (n *Network) AddLocalAddress(ip net.IP) error {
	addr := tcpip.AddrFrom4Slice(ip.To4())
	if addr.BitLen() == 0 {
		return fmt.Errorf("invalid IPv4 address %q", ip)
	}
	if err := n.Stack.AddProtocolAddress(NICID, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber, AddressWithPrefix: addr.WithPrefix()}, stack.AddressProperties{}); err != nil {
		return errors.New(err.String())
	}
	return nil
}

func (n *Network) Inject(frame []byte) {
	pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(frame)})
	n.link.InjectInbound(0, pkt)
	pkt.DecRef()
}

func (n *Network) Read(ctx context.Context) []byte {
	pkt := n.link.ReadContext(ctx)
	if pkt == nil {
		return nil
	}
	defer pkt.DecRef()
	views := pkt.AsSlices()
	total := 0
	for _, view := range views {
		total += len(view)
	}
	out := make([]byte, 0, total)
	for _, view := range views {
		out = append(out, view...)
	}
	return out
}
