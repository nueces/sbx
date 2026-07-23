//go:build linux

package hardening

import (
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"

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
	command := exec.Command(os.Args[0], "-test.run=TestLinuxHardeningChild")
	command.Env = append(os.Environ(), "SBX_HARDENING_CHILD=1", "SBX_ALLOWED_PORT="+strconv.Itoa(port))
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("hardening child failed: %v\n%s", err, output)
	}
	<-done
	if !strings.Contains(string(output), "no_new_privs,seccomp,") || !strings.Contains(string(output), ",rlimits") {
		t.Fatalf("active profile missing: %s", output)
	}
}

func TestLinuxHardeningChild(t *testing.T) {
	if os.Getenv("SBX_HARDENING_CHILD") != "1" {
		return
	}
	port, _ := strconv.Atoi(os.Getenv("SBX_ALLOWED_PORT"))
	status, err := Apply(128, []uint16{uint16(port)})
	if err != nil {
		t.Fatal(err)
	}
	fmt.Println(status.Encode())
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
