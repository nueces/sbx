//go:build !linux

package main

import "net"

// Darwin remains build-verified but runtime peer credential handling is unverified.
func peerPID(_ net.Conn) (int, error) { return 0, nil }
