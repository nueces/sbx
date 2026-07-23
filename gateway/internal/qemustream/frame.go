package qemustream

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"
)

const MaxFrameSize = 65535

var ErrClosed = errors.New("qemu stream closed")

type deadlineReader interface {
	SetReadDeadline(time.Time) error
}

type deadlineWriter interface {
	SetWriteDeadline(time.Time) error
}

type Stats struct {
	Read, Written, Rejected uint64
}

type Stream struct {
	rw       io.ReadWriter
	timeout  time.Duration
	rmu      sync.Mutex
	wmu      sync.Mutex
	closed   atomic.Bool
	read     atomic.Uint64
	written  atomic.Uint64
	rejected atomic.Uint64
}

func New(rw io.ReadWriter, timeout time.Duration) *Stream {
	return &Stream{rw: rw, timeout: timeout}
}

func (s *Stream) Close() {
	s.closed.Store(true)
}

func (s *Stream) Stats() Stats {
	return Stats{Read: s.read.Load(), Written: s.written.Load(), Rejected: s.rejected.Load()}
}

func (s *Stream) ReadFrame() ([]byte, error) {
	s.rmu.Lock()
	defer s.rmu.Unlock()
	if s.closed.Load() {
		return nil, ErrClosed
	}
	if d, ok := s.rw.(deadlineReader); ok && s.timeout > 0 {
		if err := d.SetReadDeadline(time.Now().Add(s.timeout)); err != nil {
			return nil, fmt.Errorf("set read deadline: %w", err)
		}
	}
	var header [4]byte
	if _, err := io.ReadFull(s.rw, header[:]); err != nil {
		return nil, fmt.Errorf("read frame length: %w", err)
	}
	n := binary.BigEndian.Uint32(header[:])
	if n == 0 || n > MaxFrameSize {
		s.rejected.Add(1)
		return nil, fmt.Errorf("invalid frame length %d", n)
	}
	frame := make([]byte, n)
	if _, err := io.ReadFull(s.rw, frame); err != nil {
		s.rejected.Add(1)
		return nil, fmt.Errorf("read frame payload: %w", err)
	}
	s.read.Add(1)
	return frame, nil
}

func (s *Stream) WriteFrame(frame []byte) error {
	s.wmu.Lock()
	defer s.wmu.Unlock()
	if s.closed.Load() {
		return ErrClosed
	}
	if len(frame) == 0 || len(frame) > MaxFrameSize {
		s.rejected.Add(1)
		return fmt.Errorf("invalid frame length %d", len(frame))
	}
	if d, ok := s.rw.(deadlineWriter); ok && s.timeout > 0 {
		if err := d.SetWriteDeadline(time.Now().Add(s.timeout)); err != nil {
			return fmt.Errorf("set write deadline: %w", err)
		}
	}
	packet := make([]byte, 4+len(frame))
	binary.BigEndian.PutUint32(packet, uint32(len(frame)))
	copy(packet[4:], frame)
	for len(packet) > 0 {
		n, err := s.rw.Write(packet)
		if err != nil {
			return fmt.Errorf("write frame: %w", err)
		}
		if n == 0 {
			return fmt.Errorf("write frame: %w", io.ErrShortWrite)
		}
		packet = packet[n:]
	}
	s.written.Add(1)
	return nil
}
