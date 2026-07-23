//go:build linux

package main

import (
	"errors"
	"net"

	"golang.org/x/sys/unix"
)

func peerPID(conn net.Conn) (int, error) {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, errors.New("QEMU stream is not a Unix connection")
	}
	raw, err := unixConn.SyscallConn()
	if err != nil {
		return 0, err
	}
	var credentials *unix.Ucred
	var controlErr error
	if err := raw.Control(func(fd uintptr) {
		credentials, controlErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if controlErr != nil || credentials == nil || credentials.Uid != uint32(unix.Getuid()) || credentials.Pid <= 0 {
		return 0, errors.New("QEMU Unix peer credentials mismatch")
	}
	return int(credentials.Pid), nil
}
