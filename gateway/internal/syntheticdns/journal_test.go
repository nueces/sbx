package syntheticdns

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"golang.org/x/sys/unix"
)

func journalFile(t *testing.T) *os.File {
	t.Helper()
	file, err := os.OpenFile(filepath.Join(t.TempDir(), "dns.jsonl"), os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { file.Close() })
	if err := json.NewEncoder(file).Encode(journalHeader{1, "vm", "generation", "digest"}); err != nil {
		t.Fatal(err)
	}
	return file
}

func TestJournalRestoresCachedWildcardIdentities(t *testing.T) {
	file := journalFile(t)
	original, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest")
	if err != nil {
		t.Fatal(err)
	}
	first, _ := original.Resolve("first.wild.example")
	second, _ := original.Resolve("second.wild.example")
	want := original.Snapshot()
	path := file.Name()
	file.Close() // Simulate worker loss; no graceful snapshot/flush operation is required.
	reopened, err := os.OpenFile(path, os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	replacement, err := NewWithJournal(allowAll{}, 4, reopened, "vm", "generation", "digest")
	if err != nil || !reflect.DeepEqual(replacement.Snapshot(), want) {
		t.Fatalf("replacement lost its mappings: %v", err)
	}
	for ip, host := range map[string]string{first.String(): "first.wild.example", second.String(): "second.wild.example"} {
		if entry := replacement.byName[host]; entry == nil || entry.ip.String() != ip {
			t.Fatal("cached mapping not restored before any new guest lookup")
		}
	}
	third, err := replacement.Resolve("third.wild.example")
	if err != nil || third == first || third == second {
		t.Fatalf("new lookup reassigned a cached address: %v", err)
	}
}

func TestJournalRefusesAnOverlappingWorker(t *testing.T) {
	file := journalFile(t)
	if _, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest"); err != nil {
		t.Fatal(err)
	}
	other, err := os.OpenFile(file.Name(), os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if _, err := NewWithJournal(allowAll{}, 4, other, "vm", "generation", "digest"); !errors.Is(err, unix.EWOULDBLOCK) {
		t.Fatalf("overlapping worker acquired DNS state: %v", err)
	}
}

func TestJournalFailureIsFatalAndDoesNotPublishMapping(t *testing.T) {
	file := journalFile(t)
	resolver, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest")
	if err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := resolver.Resolve("api.example"); !errors.Is(err, ErrPersistence) {
		t.Fatalf("persistence failure was masked: %v", err)
	}
	select {
	case err := <-resolver.Errors():
		if !errors.Is(err, ErrPersistence) {
			t.Fatal(err)
		}
	default:
		t.Fatal("worker did not receive a fatal DNS state failure")
	}
	if resolver.Len() != 0 {
		t.Fatal("unpersisted mapping was published")
	}
	reply := resolver.Reply(query("api.example", 1))
	if reply[3]&15 != 2 || reply[7] != 0 {
		t.Fatal("failed persistence did not yield SERVFAIL without an address")
	}
}

func TestJournalStorageBoundDoesNotReassignExistingMapping(t *testing.T) {
	file := journalFile(t)
	resolver, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest")
	if err != nil {
		t.Fatal(err)
	}
	ip, err := resolver.Resolve("api.example")
	if err != nil {
		t.Fatal(err)
	}
	resolver.journal.size = MaxJournalBytes
	if _, err := resolver.Resolve("new.example"); !errors.Is(err, ErrCapacity) {
		t.Fatalf("journal bound was not enforced: %v", err)
	}
	if existing, err := resolver.Resolve("api.example"); err != nil || existing != ip {
		t.Fatal("existing mapping changed after journal exhaustion")
	}
}

func TestJournalRejectsWrongIdentityAndCorruption(t *testing.T) {
	for _, identity := range [][3]string{{"other", "generation", "digest"}, {"vm", "other", "digest"}, {"vm", "generation", "other"}} {
		if _, err := NewWithJournal(allowAll{}, 4, journalFile(t), identity[0], identity[1], identity[2]); err == nil {
			t.Fatal("cross-generation/VM/config journal was accepted")
		}
	}
	for _, suffix := range []string{
		`{"host":`,
		"{\"host\":\"api.example\",\"ip\":\"198.18.0.1\",\"extra\":1}\n",
		"{\"host\":\"api.example\",\"ip\":\"127.0.0.1\"}\n",
		"{\"host\":\"API.example\",\"ip\":\"198.18.0.1\"}\n",
	} {
		file := journalFile(t)
		if _, err := file.WriteString(suffix); err != nil {
			t.Fatal(err)
		}
		if _, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest"); err == nil {
			t.Fatal("corrupt journal was silently accepted")
		}
	}
	file := journalFile(t)
	if err := file.Chmod(0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := NewWithJournal(allowAll{}, 4, file, "vm", "generation", "digest"); err == nil {
		t.Fatal("non-private journal accepted")
	}
}
