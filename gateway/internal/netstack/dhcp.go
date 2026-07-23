package netstack

import (
	"encoding/binary"
	"errors"
	"net"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
	"gvisor.dev/gvisor/pkg/waiter"
)

const (
	dhcpDiscover = 1
	dhcpOffer    = 2
	dhcpRequest  = 3
	dhcpAck      = 5
)

func (n *Network) ServeDHCP() (net.PacketConn, error) {
	var wq waiter.Queue
	ep, err := n.Stack.NewEndpoint(udp.ProtocolNumber, ipv4.ProtocolNumber, &wq)
	if err != nil {
		return nil, errors.New(err.String())
	}
	ep.SocketOptions().SetBroadcast(true)
	if err := ep.Bind(tcpip.FullAddress{NIC: NICID, Port: 67}); err != nil {
		ep.Close()
		return nil, errors.New(err.String())
	}
	conn := gonet.NewUDPConn(&wq, ep)
	go serveDHCP(conn)
	return conn, nil
}

func serveDHCP(conn net.PacketConn) {
	buf := make([]byte, 1500)
	for {
		n, _, err := conn.ReadFrom(buf)
		if err != nil {
			return
		}
		if reply := dhcpReply(buf[:n]); reply != nil {
			_, _ = conn.WriteTo(reply, &net.UDPAddr{IP: net.IPv4bcast, Port: 68})
		}
	}
}

func dhcpReply(request []byte) []byte {
	if len(request) < 240 || request[0] != 1 || request[1] != 1 || request[2] != 6 || string(request[236:240]) != "\x63\x82\x53\x63" {
		return nil
	}
	messageType := byte(0)
	for i := 240; i < len(request); {
		code := request[i]
		i++
		if code == 255 {
			break
		}
		if code == 0 {
			continue
		}
		if i >= len(request) || i+1+int(request[i]) > len(request) {
			return nil
		}
		length := int(request[i])
		i++
		if code == 53 && length == 1 {
			messageType = request[i]
		}
		i += length
	}
	responseType := byte(0)
	switch messageType {
	case dhcpDiscover:
		responseType = dhcpOffer
	case dhcpRequest:
		responseType = dhcpAck
	default:
		return nil
	}
	reply := make([]byte, 240, 300)
	reply[0], reply[1], reply[2] = 2, 1, 6
	copy(reply[4:8], request[4:8])
	copy(reply[10:12], request[10:12])
	copy(reply[16:20], net.ParseIP(GuestIP).To4())
	copy(reply[20:24], net.ParseIP(GatewayIP).To4())
	copy(reply[28:44], request[28:44])
	copy(reply[236:240], []byte{99, 130, 83, 99})
	reply = appendOption(reply, 53, []byte{responseType})
	reply = appendOption(reply, 54, net.ParseIP(GatewayIP).To4())
	lease := make([]byte, 4)
	binary.BigEndian.PutUint32(lease, uint32(time.Hour/time.Second))
	reply = appendOption(reply, 51, lease)
	reply = appendOption(reply, 1, net.IPv4Mask(255, 255, 255, 0))
	reply = appendOption(reply, 3, net.ParseIP(GatewayIP).To4())
	reply = appendOption(reply, 6, net.ParseIP(GatewayIP).To4())
	mtu := make([]byte, 2)
	binary.BigEndian.PutUint16(mtu, MTU)
	reply = appendOption(reply, 26, mtu)
	return append(reply, 255)
}

func appendOption(packet []byte, code byte, value []byte) []byte {
	return append(packet, append([]byte{code, byte(len(value))}, value...)...)
}
