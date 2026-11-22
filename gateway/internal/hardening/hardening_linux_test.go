//go:build linux

package hardening

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestLinuxProfileAllowsRequiredSocketsAndDeniesPrivilegeAndFiles(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	done := make(chan struct{})
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr == nil {
			conn.Close()
		}
		close(done)
	}()
	// AllThreadsSyscall deliberately rejects cgo binaries, including the race-test binary.
	binary := filepath.Join(t.TempDir(), "hardening.test")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	build := exec.CommandContext(ctx, filepath.Join(runtime.GOROOT(), "bin", "go"), "test", "-c", "-o", binary, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build pure-Go confinement test: %v\n%s", err, output)
	}
	command := exec.CommandContext(ctx, binary, "-test.run=TestLinuxHardeningChild")
	command.Env = append(os.Environ(), "SBX_HARDENING_CHILD=1", "SBX_ALLOWED_PORT="+strconv.Itoa(port))
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("hardening child failed: %v\n%s", err, output)
	}
	<-done
	t.Logf("child profile: %s", output)
	if !strings.Contains(string(output), "no_new_privs,seccomp,") || !strings.Contains(string(output), ",rlimits") {
		t.Fatalf("active profile missing: %s", output)
	}
}

func TestLinuxHardeningChild(t *testing.T) {
	if os.Getenv("SBX_HARDENING_CHILD") != "1" {
		return
	}
	port, _ := strconv.Atoi(os.Getenv("SBX_ALLOWED_PORT"))
	const threads = 4
	ready := make(chan int, threads)
	verify := make(chan Status)
	failures := make(chan error, threads*2)
	var workers sync.WaitGroup
	checkThread := func() {
		defer workers.Done()
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		ready <- unix.Gettid()
		status := <-verify
		nnp, err := unix.PrctlRetInt(unix.PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
		if err != nil || nnp != 1 {
			failures <- fmt.Errorf("thread %d is missing no_new_privs: %d, %v", unix.Gettid(), nnp, err)
			return
		}
		if status.Landlock {
			file, err := os.Open("/etc/passwd")
			if file != nil {
				file.Close()
			}
			if !errors.Is(err, os.ErrPermission) {
				failures <- fmt.Errorf("thread %d escaped Landlock: %v", unix.Gettid(), err)
			}
		}
	}
	workers.Add(threads)
	for range threads {
		go checkThread()
	}
	tids := make(map[int]bool)
	for range threads {
		tids[<-ready] = true
	}
	if len(tids) != threads {
		t.Fatal("did not establish distinct pre-existing OS threads")
	}
	status, err := Apply(128, []uint16{uint16(port)})
	if err != nil {
		t.Fatal(err)
	}
	fmt.Println(status.Encode())
	// Keep the old threads occupied while new goroutines request additional OS threads.
	workers.Add(threads)
	for range threads {
		go checkThread()
	}
	for range threads {
		tids[<-ready] = true
	}
	if len(tids) != threads*2 {
		t.Fatal("did not establish additional threads after confinement")
	}
	for range threads * 2 {
		verify <- status
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Error(err)
	}
	if !status.NoNewPrivs || !status.Seccomp || !status.Limits {
		t.Fatalf("required Linux hardening unavailable: %s", status.Encode())
	}
	conn, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("allowed loopback socket failed: %v", err)
	}
	conn.Close()
	if status.Landlock {
		if _, err := net.Dial("tcp", "127.0.0.1:1"); !errors.Is(err, os.ErrPermission) {
			t.Fatalf("unapproved TCP port was not denied: %v", err)
		}
	}
	if err := unix.Unshare(unix.CLONE_NEWNS); !errors.Is(err, unix.EPERM) {
		t.Fatalf("privileged syscall was not denied: %v", err)
	}
	if status.Landlock {
		if _, err := os.Open("/etc/passwd"); !errors.Is(err, os.ErrPermission) {
			t.Fatalf("Landlock filesystem escape was not denied: %v", err)
		}
	}
	var core unix.Rlimit
	if err := unix.Getrlimit(unix.RLIMIT_CORE, &core); err != nil || core.Cur != 0 || core.Max != 0 {
		t.Fatalf("core dumps remain enabled: %+v, %v", core, err)
	}
	statusText, err := os.ReadFile("/proc/self/status")
	if err != nil || !strings.Contains(string(statusText), "NoNewPrivs:\t1") || !strings.Contains(string(statusText), "Seccomp:\t2") {
		t.Fatalf("kernel hardening state missing: %v\n%s", err, statusText)
	}
}
