package qemustream

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"
)

type shortWriter struct{ bytes.Buffer }

func (w *shortWriter) Write(p []byte) (int, error) {
	if len(p) > 2 {
		p = p[:2]
	}
	return w.Buffer.Write(p)
}

func framed(payload []byte) []byte {
	b := make([]byte, 4+len(payload))
	binary.BigEndian.PutUint32(b, uint32(len(payload)))
	copy(b[4:], payload)
	return b
}

func TestReadFrame(t *testing.T) {
	for name, input := range map[string][]byte{
		"zero":      {0, 0, 0, 0},
		"truncated": {0, 0, 0, 2, 1},
		"oversized": {0, 1, 0, 0},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := New(bytes.NewBuffer(input), 0).ReadFrame(); err == nil {
				t.Fatal("accepted invalid frame")
			}
		})
	}
	want := []byte{1, 2, 3}
	stream := New(bytes.NewBuffer(framed(want)), 0)
	got, err := stream.ReadFrame()
	if err != nil || !bytes.Equal(got, want) || stream.Stats().Read != 1 {
		t.Fatalf("got %v, %v, %+v", got, err, stream.Stats())
	}
}

func TestWriteFrameHandlesShortWrites(t *testing.T) {
	w := new(shortWriter)
	if err := New(w, 0).WriteFrame([]byte{1, 2, 3}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(w.Bytes(), framed([]byte{1, 2, 3})) {
		t.Fatalf("bad packet: %v", w.Bytes())
	}
}

func TestClosed(t *testing.T) {
	s := New(new(bytes.Buffer), 0)
	s.Close()
	if _, err := s.ReadFrame(); !errors.Is(err, ErrClosed) {
		t.Fatalf("read: %v", err)
	}
	if err := s.WriteFrame([]byte{1}); !errors.Is(err, ErrClosed) {
		t.Fatalf("write: %v", err)
	}
}

func FuzzReadFrame(f *testing.F) {
	f.Add(framed([]byte{1, 2, 3}))
	f.Fuzz(func(t *testing.T, input []byte) {
		frame, err := New(bytes.NewBuffer(input), 0).ReadFrame()
		if err == nil && (len(frame) == 0 || len(frame) > MaxFrameSize) {
			t.Fatalf("accepted length %d", len(frame))
		}
		if err != nil && errors.Is(err, io.EOF) {
			return
		}
	})
}
