"""Rootless process hardening for the pinned mediator launcher."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import resource
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

MAX_LOG_BYTES = 100 * 1024 * 1024
MAX_ADDRESS_SPACE = 512 * 1024 * 1024

_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_K = 0x00
_BPF_RET = 0x06
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000

_LANDLOCK_EXECUTE = 1 << 0
_LANDLOCK_WRITE_FILE = 1 << 1
_LANDLOCK_READ_FILE = 1 << 2
_LANDLOCK_READ_DIR = 1 << 3
_LANDLOCK_REMOVE_DIR = 1 << 4
_LANDLOCK_REMOVE_FILE = 1 << 5
_LANDLOCK_MAKE_CHAR = 1 << 6
_LANDLOCK_MAKE_DIR = 1 << 7
_LANDLOCK_MAKE_REG = 1 << 8
_LANDLOCK_MAKE_SOCK = 1 << 9
_LANDLOCK_MAKE_FIFO = 1 << 10
_LANDLOCK_MAKE_BLOCK = 1 << 11
_LANDLOCK_MAKE_SYM = 1 << 12
_LANDLOCK_REFER = 1 << 13
_LANDLOCK_TRUNCATE = 1 << 14
_LANDLOCK_IOCTL_DEV = 1 << 15
_LANDLOCK_BASE = (1 << 13) - 1

_ARCHES = {
    "x86_64": (
        0xC000003E,
        (101, 155, 165, 166, 175, 176, 246, 248, 249, 250, 272, 298, 308, 313, 321, 323),
        (444, 445, 446),
    ),
    "aarch64": (
        0xC00000B7,
        (39, 40, 41, 97, 104, 105, 106, 117, 217, 218, 219, 241, 268, 273, 280, 282),
        (444, 445, 446),
    ),
}


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
        ("reserved", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class Status:
    no_new_privs: bool
    seccomp: bool
    landlock: bool
    limits: bool

    def encode(self) -> str:
        return ",".join(
            (
                "no_new_privs" if self.no_new_privs else "no_new_privs-unavailable",
                "seccomp" if self.seccomp else "seccomp-unavailable",
                "landlock" if self.landlock else "landlock-unavailable",
                "rlimits" if self.limits else "rlimits-unavailable",
            )
        )


def _lower_limit(kind: int, value: int) -> None:
    soft, hard = resource.getrlimit(kind)
    selected = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(kind, (selected, hard))


def _resource_limits(open_files: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _lower_limit(resource.RLIMIT_NOFILE, max(32, open_files))
    _lower_limit(resource.RLIMIT_FSIZE, MAX_LOG_BYTES)
    _lower_limit(resource.RLIMIT_AS, MAX_ADDRESS_SPACE)


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _seccomp() -> bool:
    selected = _ARCHES.get(platform.machine().lower())
    if selected is None:
        return False
    audit_arch, denied, _ = selected
    instructions = [
        _SockFilter(_BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4),
        _SockFilter(_BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0),
    ]
    for syscall_number in denied:
        instructions.extend(
            (
                _SockFilter(_BPF_JMP | _BPF_JEQ | _BPF_K, 0, 1, syscall_number),
                _SockFilter(_BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
            )
        )
    instructions.append(_SockFilter(_BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ALLOW))
    array = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(array), array)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        error = ctypes.get_errno()
        if error in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            return False
        raise OSError(error, os.strerror(error))
    return True


def _landlock_access(abi: int) -> int:
    access = _LANDLOCK_BASE
    if abi >= 2:
        access |= _LANDLOCK_REFER
    if abi >= 3:
        access |= _LANDLOCK_TRUNCATE
    if abi >= 5:
        access |= _LANDLOCK_IOCTL_DEV
    return access


def _landlock(paths: tuple[tuple[Path, bool], ...]) -> bool:
    selected = _ARCHES.get(platform.machine().lower())
    if selected is None:
        return False
    create_ruleset, add_rule, restrict_self = selected[2]
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(create_ruleset, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 1:
        error = ctypes.get_errno()
        if error in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM}:
            return False
        raise OSError(error, os.strerror(error))
    handled = _landlock_access(abi)
    attr = _LandlockRulesetAttr(handled, 0, 0)
    ruleset = libc.syscall(create_ruleset, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        for raw_path, writable in paths:
            try:
                path = raw_path.resolve(strict=True)
                info = path.stat()
            except OSError:
                continue
            allowed = handled if writable else _LANDLOCK_READ_FILE
            if stat.S_ISDIR(info.st_mode):
                allowed = (
                    handled
                    if writable
                    else _LANDLOCK_EXECUTE | _LANDLOCK_READ_FILE | _LANDLOCK_READ_DIR
                )
            fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            try:
                path_attr = _LandlockPathBeneathAttr(allowed, fd, 0)
                added = libc.syscall(
                    add_rule,
                    ruleset,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(path_attr),
                    0,
                )
                if added != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
            finally:
                os.close(fd)
        if libc.syscall(restrict_self, ruleset, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        os.close(ruleset)
    return True


def apply(confdir: Path, open_files: int, executable: Path) -> Status:
    """Apply controls that require no privilege; unsupported Linux Landlock is explicit."""
    _resource_limits(open_files)
    if sys.platform != "linux":
        return Status(False, False, False, True)
    _prctl(_PR_SET_DUMPABLE, 0)
    _prctl(_PR_SET_NO_NEW_PRIVS, 1)
    roots = (
        (confdir.parent, True),
        (executable.parent, False),
        (Path(sys.executable).parent.parent, False),
        (Path(sys.prefix), False),
        (Path(sys.base_prefix), False),
        (Path(__file__).parent, False),
        (Path("/etc/ssl"), False),
        (Path("/etc/resolv.conf"), False),
        (Path("/etc/hosts"), False),
        (Path("/etc/nsswitch.conf"), False),
        (Path("/etc/gai.conf"), False),
        (Path("/usr/share/zoneinfo"), False),
        (Path("/proc/self/status"), False),
    )
    landlock = _landlock(roots)
    return Status(True, _seccomp(), landlock, True)


def sanitized_environment(confdir: Path, status: Status, values: dict[str, str]) -> dict[str, str]:
    environment = {
        "HOME": str(confdir),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONUTF8": "1",
        "TZ": "UTC",
        "SBX_GATEWAY_HARDENING": status.encode(),
    }
    for name, value in values.items():
        if not value or "\x00" in value:
            raise RuntimeError("invalid mediator launch environment")
        environment[name] = value
    return environment


def close_unexpected_fds(preserve: set[int]) -> None:
    try:
        descriptors = [int(name) for name in os.listdir("/proc/self/fd") if name.isdigit()]
    except OSError:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        descriptors = list(range(3, min(soft, 65536)))
    for fd in descriptors:
        if fd > 2 and fd not in preserve:
            with suppress(OSError):
                os.close(fd)
