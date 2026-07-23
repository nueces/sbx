package netstack

import (
	"encoding/binary"
	"net"

	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
)

func (n *Network) ServeDNS(resolver *syntheticdns.Resolver) (net.PacketConn, error) {
	conn, err := gonet.DialUDP(n.Stack, &tcpip.FullAddress{
		NIC: NICID, Addr: tcpip.AddrFrom4Slice(net.ParseIP(GatewayIP).To4()), Port: 53,
	}, nil, ipv4.ProtocolNumber)
	if err != nil {
		return nil, err
	}
	go func() {
		buf := make([]byte, 1232)
		known := make(map[string]bool)
		for {
			read, peer, err := conn.ReadFrom(buf)
			if err != nil {
				return
			}
			reply := resolver.Reply(buf[:read])
			if len(reply) == 0 {
				continue
			}
			if binary.BigEndian.Uint16(reply[6:8]) == 1 {
				ip := net.IP(reply[len(reply)-4:]).String()
				if !known[ip] {
					if err := n.AddLocalAddress(net.ParseIP(ip)); err != nil {
						continue
					}
					known[ip] = true
				}
			}
			_, _ = conn.WriteTo(reply, peer)
		}
	}()
	return conn, nil
}
