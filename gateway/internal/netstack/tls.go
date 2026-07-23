package netstack

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
)

const (
	maxTLSHandshake = 256 * 1024
	maxTLSRecord    = 16*1024 + 256
)

func readClientHello(conn net.Conn) ([]byte, string, error) {
	var raw, handshake []byte
	for {
		if len(handshake) >= 4 {
			handshakeSize := int(handshake[1])<<16 | int(handshake[2])<<8 | int(handshake[3])
			if len(handshake) >= 4+handshakeSize {
				break
			}
		}
		var header [5]byte
		if _, err := io.ReadFull(conn, header[:]); err != nil {
			return nil, "", errors.New("TLS ClientHello missing")
		}
		size := int(binary.BigEndian.Uint16(header[3:]))
		if header[0] != 22 || header[1] != 3 || size == 0 || size > maxTLSRecord || len(raw)+5+size > maxTLSHandshake {
			return nil, "", errors.New("invalid TLS ClientHello record")
		}
		payload := make([]byte, size)
		if _, err := io.ReadFull(conn, payload); err != nil {
			return nil, "", errors.New("truncated TLS ClientHello")
		}
		raw = append(raw, header[:]...)
		raw = append(raw, payload...)
		handshake = append(handshake, payload...)
		if len(handshake) >= 4 {
			handshakeSize := int(handshake[1])<<16 | int(handshake[2])<<8 | int(handshake[3])
			if handshake[0] != 1 || handshakeSize == 0 || handshakeSize+4 > maxTLSHandshake {
				return nil, "", errors.New("invalid TLS ClientHello")
			}
		}
	}
	handshakeSize := int(handshake[1])<<16 | int(handshake[2])<<8 | int(handshake[3])
	sni, err := clientHelloSNI(handshake[4 : 4+handshakeSize])
	return raw, sni, err
}

func clientHelloSNI(hello []byte) (string, error) {
	take := func(size int) ([]byte, bool) {
		if size < 0 || len(hello) < size {
			return nil, false
		}
		value := hello[:size]
		hello = hello[size:]
		return value, true
	}
	if _, ok := take(34); !ok { // legacy version and random
		return "", errors.New("truncated TLS ClientHello")
	}
	sessionSize, ok := take(1)
	if !ok {
		return "", errors.New("truncated TLS ClientHello")
	}
	if _, ok = take(int(sessionSize[0])); !ok {
		return "", errors.New("truncated TLS ClientHello")
	}
	cipherSize, ok := take(2)
	if !ok {
		return "", errors.New("truncated TLS ClientHello")
	}
	cipherBytes := int(binary.BigEndian.Uint16(cipherSize))
	if cipherBytes == 0 || cipherBytes%2 != 0 {
		return "", errors.New("invalid TLS cipher suites")
	}
	if _, ok = take(cipherBytes); !ok {
		return "", errors.New("truncated TLS ClientHello")
	}
	compressionSize, ok := take(1)
	if !ok || compressionSize[0] == 0 {
		return "", errors.New("invalid TLS compression methods")
	}
	if _, ok = take(int(compressionSize[0])); !ok {
		return "", errors.New("truncated TLS ClientHello")
	}
	extensionSize, ok := take(2)
	if !ok || int(binary.BigEndian.Uint16(extensionSize)) != len(hello) {
		return "", errors.New("invalid TLS extensions")
	}
	var sni string
	for len(hello) != 0 {
		header, ok := take(4)
		if !ok {
			return "", errors.New("truncated TLS extension")
		}
		size := int(binary.BigEndian.Uint16(header[2:]))
		value, ok := take(size)
		if !ok {
			return "", errors.New("truncated TLS extension")
		}
		if binary.BigEndian.Uint16(header[:2]) != 0 {
			continue
		}
		if sni != "" || len(value) < 5 || int(binary.BigEndian.Uint16(value[:2])) != len(value)-2 {
			return "", errors.New("invalid TLS SNI")
		}
		names := value[2:]
		for len(names) != 0 {
			if len(names) < 3 {
				return "", errors.New("invalid TLS SNI")
			}
			size := int(binary.BigEndian.Uint16(names[1:3]))
			if names[0] != 0 || size == 0 || len(names) < 3+size || sni != "" {
				return "", errors.New("invalid TLS SNI")
			}
			sni = string(names[3 : 3+size])
			names = names[3+size:]
		}
	}
	if sni == "" {
		return "", errors.New("TLS passthrough requires SNI")
	}
	return sni, nil
}
