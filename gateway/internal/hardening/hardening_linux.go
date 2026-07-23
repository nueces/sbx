//go:build linux

package hardening

import (
	"errors"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
	"unsafe"

	"golang.org/x/sys/unix"
)

const (
	maxLogBytes = 100 * 1024 * 1024
	bpfLD       = 0x00
	bpfW        = 0x00
	bpfABS      = 0x20
	bpfJMP      = 0x05
	bpfJEQ      = 0x10
	bpfK        = 0x00
	bpfRET      = 0x06
)

const (
	landlockCreateRulesetVersion = 1
	landlockRulePathBeneath      = 1
	landlockRuleNetPort          = 2
	landlockAccessNetConnectTCP  = 1 << 1
)

type Status struct {
	NoNewPrivs bool
	Seccomp    bool
	Landlock   bool
	Limits     bool
}

func (s Status) Encode() string {
	values := []string{"no_new_privs-unavailable", "seccomp-unavailable", "landlock-unavailable", "rlimits-unavailable"}
	if s.NoNewPrivs {
		values[0] = "no_new_privs"
	}
	if s.Seccomp {
		values[1] = "seccomp"
	}
	if s.Landlock {
		values[2] = "landlock"
	}
	if s.Limits {
		values[3] = "rlimits"
	}
	return values[0] + "," + values[1] + "," + values[2] + "," + values[3]
}

type landlockRulesetAttr struct {
	HandledAccessFS  uint64
	HandledAccessNet uint64
	Scoped           uint64
}

type landlockPathBeneathAttr struct {
	AllowedAccess uint64
	ParentFD      int32
	_             uint32
}

type landlockNetPortAttr struct {
	AllowedAccess uint64
	Port          uint64
}

func lowerLimit(kind int, value uint64) error {
	var limit unix.Rlimit
	if err := unix.Getrlimit(kind, &limit); err != nil {
		return err
	}
	if limit.Max != unix.RLIM_INFINITY && value > limit.Max {
		value = limit.Max
	}
	limit.Cur = value
	return unix.Setrlimit(kind, &limit)
}

func limits(openFiles int) error {
	if openFiles < 32 || openFiles > 4096 {
		return errors.New("open-file limit must be between 32 and 4096")
	}
	if err := unix.Setrlimit(unix.RLIMIT_CORE, &unix.Rlimit{}); err != nil {
		return err
	}
	if err := lowerLimit(unix.RLIMIT_NOFILE, uint64(openFiles)); err != nil {
		return err
	}
	if err := lowerLimit(unix.RLIMIT_FSIZE, maxLogBytes); err != nil {
		return err
	}
	debug.SetMemoryLimit(384 * 1024 * 1024)
	return nil
}

func auditArch() (uint32, bool) {
	switch runtime.GOARCH {
	case "amd64":
		return unix.AUDIT_ARCH_X86_64, true
	case "arm64":
		return unix.AUDIT_ARCH_AARCH64, true
	default:
		return 0, false
	}
}

func seccomp() (bool, error) {
	arch, supported := auditArch()
	if !supported {
		return false, nil
	}
	denied := []uint32{
		unix.SYS_PTRACE, unix.SYS_PIVOT_ROOT, unix.SYS_MOUNT, unix.SYS_UMOUNT2,
		unix.SYS_INIT_MODULE, unix.SYS_DELETE_MODULE, unix.SYS_KEXEC_LOAD,
		unix.SYS_ADD_KEY, unix.SYS_REQUEST_KEY, unix.SYS_KEYCTL, unix.SYS_UNSHARE,
		unix.SYS_PERF_EVENT_OPEN, unix.SYS_SETNS, unix.SYS_FINIT_MODULE,
		unix.SYS_BPF, unix.SYS_USERFAULTFD,
	}
	filters := []unix.SockFilter{
		{Code: bpfLD | bpfW | bpfABS, K: 4},
		{Code: bpfJMP | bpfJEQ | bpfK, Jt: 1, K: arch},
		{Code: bpfRET | bpfK, K: unix.SECCOMP_RET_KILL_PROCESS},
		{Code: bpfLD | bpfW | bpfABS, K: 0},
	}
	for _, syscallNumber := range denied {
		filters = append(filters,
			unix.SockFilter{Code: bpfJMP | bpfJEQ | bpfK, Jf: 1, K: syscallNumber},
			unix.SockFilter{Code: bpfRET | bpfK, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EPERM)},
		)
	}
	filters = append(filters, unix.SockFilter{Code: bpfRET | bpfK, K: unix.SECCOMP_RET_ALLOW})
	program := unix.SockFprog{Len: uint16(len(filters)), Filter: &filters[0]}
	_, _, syscallErr := unix.Syscall6(
		unix.SYS_SECCOMP,
		unix.SECCOMP_SET_MODE_FILTER,
		unix.SECCOMP_FILTER_FLAG_TSYNC,
		uintptr(unsafe.Pointer(&program)),
		0,
		0,
		0,
	)
	if syscallErr == 0 {
		return true, nil
	}
	if errors.Is(syscallErr, unix.ENOSYS) || errors.Is(syscallErr, unix.EINVAL) || errors.Is(syscallErr, unix.EOPNOTSUPP) {
		return false, nil
	}
	return false, syscallErr
}

func landlockAccess(abi int) uint64 {
	access := uint64((1 << 13) - 1)
	if abi >= 2 {
		access |= unix.LANDLOCK_ACCESS_FS_REFER
	}
	if abi >= 3 {
		access |= unix.LANDLOCK_ACCESS_FS_TRUNCATE
	}
	if abi >= 5 {
		access |= unix.LANDLOCK_ACCESS_FS_IOCTL_DEV
	}
	return access
}

func landlockReadPaths(ruleset int, handled uint64) error {
	paths := []string{"/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/ssl", "/proc/self/status"}
	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		allowed := uint64(unix.LANDLOCK_ACCESS_FS_READ_FILE)
		if info.IsDir() {
			allowed |= unix.LANDLOCK_ACCESS_FS_EXECUTE | unix.LANDLOCK_ACCESS_FS_READ_DIR
		}
		fd, err := unix.Open(path, unix.O_PATH|unix.O_CLOEXEC, 0)
		if err != nil {
			return err
		}
		attr := landlockPathBeneathAttr{AllowedAccess: allowed & handled, ParentFD: int32(fd)}
		_, _, syscallErr := unix.Syscall6(
			unix.SYS_LANDLOCK_ADD_RULE, uintptr(ruleset), landlockRulePathBeneath,
			uintptr(unsafe.Pointer(&attr)), 0, 0, 0,
		)
		unix.Close(fd)
		if syscallErr != 0 {
			return syscallErr
		}
	}
	return nil
}

func landlock(ports []uint16) (bool, error) {
	abi, _, syscallErr := unix.Syscall6(
		unix.SYS_LANDLOCK_CREATE_RULESET, 0, 0, landlockCreateRulesetVersion, 0, 0, 0,
	)
	if syscallErr != 0 {
		if errors.Is(syscallErr, unix.ENOSYS) || errors.Is(syscallErr, unix.EINVAL) || errors.Is(syscallErr, unix.EOPNOTSUPP) || errors.Is(syscallErr, unix.EPERM) {
			return false, nil
		}
		return false, syscallErr
	}
	handledFS := landlockAccess(int(abi))
	handledNet := uint64(0)
	if abi >= 4 {
		handledNet = landlockAccessNetConnectTCP
	}
	attr := landlockRulesetAttr{HandledAccessFS: handledFS, HandledAccessNet: handledNet}
	ruleset, _, syscallErr := unix.Syscall6(
		unix.SYS_LANDLOCK_CREATE_RULESET,
		uintptr(unsafe.Pointer(&attr)),
		unsafe.Sizeof(attr),
		0, 0, 0, 0,
	)
	if syscallErr != 0 {
		return false, syscallErr
	}
	defer unix.Close(int(ruleset))
	if err := landlockReadPaths(int(ruleset), handledFS); err != nil {
		return false, err
	}
	if handledNet != 0 {
		seen := make(map[uint16]bool)
		for _, port := range ports {
			if port == 0 || seen[port] {
				continue
			}
			seen[port] = true
			netAttr := landlockNetPortAttr{AllowedAccess: handledNet, Port: uint64(port)}
			_, _, syscallErr = unix.Syscall6(
				unix.SYS_LANDLOCK_ADD_RULE, ruleset, landlockRuleNetPort,
				uintptr(unsafe.Pointer(&netAttr)), 0, 0, 0,
			)
			if syscallErr != 0 {
				return false, syscallErr
			}
		}
	}
	_, _, syscallErr = unix.Syscall6(unix.SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0, 0, 0, 0, 0)
	if syscallErr != 0 {
		return false, syscallErr
	}
	return true, nil
}

func Apply(openFiles int, ports []uint16) (Status, error) {
	if err := limits(openFiles); err != nil {
		return Status{}, fmt.Errorf("resource limits: %w", err)
	}
	if err := unix.Prctl(unix.PR_SET_DUMPABLE, 0, 0, 0, 0); err != nil {
		return Status{}, fmt.Errorf("disable dumps: %w", err)
	}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return Status{}, fmt.Errorf("no_new_privs: %w", err)
	}
	landlocked, err := landlock(ports)
	if err != nil {
		return Status{}, fmt.Errorf("landlock: %w", err)
	}
	filtered, err := seccomp()
	if err != nil {
		return Status{}, fmt.Errorf("seccomp: %w", err)
	}
	return Status{NoNewPrivs: true, Seccomp: filtered, Landlock: landlocked, Limits: true}, nil
}
