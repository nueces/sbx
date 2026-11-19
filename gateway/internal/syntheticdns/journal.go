package syntheticdns

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

const MaxJournalBytes = 1 << 20

var ErrPersistence = errors.New("synthetic DNS persistence failed")

type journalHeader struct {
	Version    int    `json:"version"`
	VMID       string `json:"vm_id"`
	Generation string `json:"generation"`
	Digest     string `json:"digest"`
}

type journalState struct {
	file   *os.File
	size   int
	failed error
	errors chan error
}

// NewWithJournal restores generation-owned identities before the resolver is exposed.
// The caller retains ownership of the inherited file descriptor.
func NewWithJournal(policy Policy, limit int, file *os.File, vmID, generation, digest string) (*Resolver, error) {
	if file == nil {
		return nil, errors.New("missing synthetic DNS journal")
	}
	var info unix.Stat_t
	if err := unix.Fstat(int(file.Fd()), &info); err != nil {
		return nil, fmt.Errorf("stat synthetic DNS journal: %w", err)
	}
	flags, err := unix.FcntlInt(file.Fd(), unix.F_GETFL, 0)
	if err != nil {
		return nil, fmt.Errorf("inspect synthetic DNS journal: %w", err)
	}
	if info.Mode&unix.S_IFMT != unix.S_IFREG || info.Uid != uint32(os.Getuid()) || info.Mode&0o777 != 0o600 || info.Size > MaxJournalBytes || flags&unix.O_APPEND == 0 || flags&unix.O_ACCMODE != unix.O_RDWR {
		return nil, errors.New("unsafe synthetic DNS journal")
	}
	// A replacement must not race a previous worker that has not actually exited.
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return nil, fmt.Errorf("lock synthetic DNS journal: %w", err)
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("seek synthetic DNS journal: %w", err)
	}
	data, err := io.ReadAll(io.LimitReader(file, MaxJournalBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read synthetic DNS journal: %w", err)
	}
	if len(data) > MaxJournalBytes {
		return nil, errors.New("synthetic DNS journal exceeds limit")
	}
	if len(data) == 0 || data[len(data)-1] != '\n' {
		return nil, errors.New("unterminated synthetic DNS journal")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var header journalHeader
	if err := decoder.Decode(&header); err != nil {
		return nil, fmt.Errorf("decode synthetic DNS journal identity: %w", err)
	}
	if header != (journalHeader{1, vmID, generation, digest}) {
		return nil, errors.New("synthetic DNS journal identity mismatch")
	}
	mappings := []Mapping{}
	for {
		var mapping Mapping
		err := decoder.Decode(&mapping)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("decode synthetic DNS mapping: %w", err)
		}
		if len(mappings) >= limit {
			return nil, ErrCapacity
		}
		mappings = append(mappings, mapping)
	}
	resolver := NewWithLimit(policy, limit)
	if err := resolver.Restore(mappings); err != nil {
		return nil, fmt.Errorf("restore synthetic DNS mappings: %w", err)
	}
	resolver.journal = &journalState{file: file, size: len(data), errors: make(chan error, 1)}
	return resolver, nil
}

func (r *Resolver) Errors() <-chan error {
	if r.journal == nil {
		return nil
	}
	return r.journal.errors
}

// Called under the resolver lock; publication follows successful append and sync.
func (r *Resolver) persist(mapping Mapping) error {
	j := r.journal
	if j == nil {
		return nil
	}
	if j.failed != nil {
		return j.failed
	}
	raw, err := json.Marshal(mapping)
	raw = append(raw, '\n')
	if err == nil && j.size+len(raw) > MaxJournalBytes {
		return ErrCapacity
	}
	if err == nil {
		var n int
		n, err = j.file.Write(raw)
		if err == nil && n != len(raw) {
			err = io.ErrShortWrite
		}
	}
	if err == nil {
		err = j.file.Sync()
	}
	if err != nil {
		j.failed = fmt.Errorf("%w: %w", ErrPersistence, err)
		j.errors <- j.failed
		return j.failed
	}
	j.size += len(raw)
	return nil
}
