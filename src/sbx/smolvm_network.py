"""Typed SmolVM compatibility facade for one external QEMU network backend."""

from __future__ import annotations

import os
import platform
import re
import stat
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from smolvm.exceptions import SmolVMError
from smolvm.runtime.guest_platforms import GuestPlatformSpec, get_guest_platform
from smolvm.runtime.qemu import QEMU_ROOT_NODE_NAME, QemuRuntimeAdapter
from smolvm.runtime.qemu_args import build_qemu_argv as _build_smolvm_qemu_argv
from smolvm.types import VMInfo, VMState

from sbx.constants import GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
from sbx.runtime import ConfigError

_MINIMUM_QEMU_VERSION = (7, 2, 0)
_MAC_PATTERN = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")
_QEMU_VERSION_PATTERN = re.compile(r"QEMU emulator version (\d+)\.(\d+)(?:\.(\d+))?")
_QEMU_SANDBOX = "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"


@dataclass(frozen=True, slots=True)
class ExternalNetworkBackend:
    """A controller-owned Unix stream to which QEMU reconnects."""

    kind: Literal["unix-stream"]
    socket_path: Path
    role: Literal["qemu-connects"]
    mac: str


def _capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"could not inspect QEMU external-network support: {exc}") from exc


def _validate_socket_path(path: Path) -> None:
    value = str(path)
    if not path.is_absolute():
        raise ConfigError("gateway QEMU socket path must be absolute")
    if "\0" in value or "," in value:
        raise ConfigError("gateway QEMU socket path cannot contain NUL or ','")
    if len(os.fsencode(path)) > GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES:
        raise ConfigError(
            "gateway QEMU socket path exceeds "
            f"{GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES} filesystem bytes"
        )
    try:
        info = path.lstat()
        parent = path.parent.lstat()
    except OSError as exc:
        raise ConfigError(f"gateway QEMU socket is not ready: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise ConfigError(f"gateway QEMU socket path is not a Unix socket: {path}")
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ConfigError("gateway QEMU socket and directory must be owner-only")


def _validate_backend(backend: ExternalNetworkBackend) -> None:
    if backend.kind != "unix-stream":
        raise ConfigError(f"unsupported external network backend: {backend.kind!r}")
    if backend.role != "qemu-connects":
        raise ConfigError(f"unsupported external network role: {backend.role!r}")
    _validate_socket_path(backend.socket_path)
    if not _MAC_PATTERN.fullmatch(backend.mac):
        raise ConfigError(f"invalid external network MAC address: {backend.mac!r}")
    first_octet = int(backend.mac[:2], 16)
    if first_octet & 1 or not first_octet & 2:
        raise ConfigError("external network MAC must be locally administered and unicast")


def require_external_network_support(qemu_bin: Path) -> tuple[int, int, int]:
    """Fail before launch unless QEMU supports stream netdevs."""
    result = _capture([str(qemu_bin), "--version"])
    match = _QEMU_VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or match is None:
        raise ConfigError(f"could not determine QEMU version for gateway networking: {qemu_bin}")
    version = tuple(int(value or 0) for value in match.groups())
    if version < _MINIMUM_QEMU_VERSION:
        found = ".".join(str(value) for value in version)
        raise ConfigError(f"gateway networking requires QEMU 7.2 or newer; found {found}")

    result = _capture([str(qemu_bin), "-netdev", "help"])
    backends = {line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines()}
    if result.returncode != 0 or "stream" not in backends:
        raise ConfigError(f"QEMU does not support the required 'stream' netdev backend: {qemu_bin}")
    return version


def _qemu_sandbox_supported(qemu_bin: Path) -> bool:
    result = _capture([str(qemu_bin), "--help"])
    return result.returncode == 0 and "-sandbox" in result.stdout


def _use_tcg_without_kvm(argv: list[str]) -> list[str]:
    result = list(argv)
    machine = [index + 1 for index, value in enumerate(result[:-1]) if value == "-machine"]
    cpu = [index + 1 for index, value in enumerate(result[:-1]) if value == "-cpu"]
    smp = [index + 1 for index, value in enumerate(result[:-1]) if value == "-smp"]
    if (
        len(machine) != 1
        or "accel=kvm" not in result[machine[0]]
        or len(cpu) != 1
        or result[cpu[0]] != "host"
        or len(smp) != 1
    ):
        raise ConfigError("SmolVM QEMU accelerator argv is incompatible with TCG fallback")
    result[machine[0]] = result[machine[0]].replace("accel=kvm", "accel=tcg")
    if result[machine[0]].startswith("microvm,"):
        result[machine[0]] = (
            result[machine[0]].replace("pic=off", "pic=on").replace("pit=off", "pit=on")
        )
    result[cpu[0]] = "max" if result[machine[0]].startswith("virt,") else "qemu64"
    # ponytail: one TCG vCPU avoids microvm clock calibration stalls; revisit with SmolVM TCG.
    result[smp[0]] = "1"
    return result


def _replace_network(argv: list[str], backend: ExternalNetworkBackend) -> list[str]:
    netdev_values = [index + 1 for index, value in enumerate(argv[:-1]) if value == "-netdev"]
    device_values = [
        index + 1
        for index, value in enumerate(argv[:-1])
        if value == "-device" and "netdev=net0" in argv[index + 1].split(",")
    ]
    if len(netdev_values) != 1 or len(device_values) != 1:
        raise ConfigError(
            "SmolVM QEMU network argv is incompatible with the external-network facade"
        )

    append_values = [index + 1 for index, value in enumerate(argv[:-1]) if value == "-append"]
    if len(append_values) != 1:
        raise ConfigError("SmolVM QEMU boot argv is incompatible with gateway networking")

    result = list(argv)
    boot_args = re.sub(r"(?<!\S)(?:ip|smolvm\.network)=\S+\s*", "", result[append_values[0]])
    result[append_values[0]] = f"{boot_args.strip()} smolvm.network=guest"
    result[netdev_values[0]] = (
        f"stream,id=net0,server=off,reconnect-ms=100,addr.type=unix,addr.path={backend.socket_path}"
    )
    device = result[device_values[0]].split(",")
    mac_values = [index for index, value in enumerate(device) if value.startswith("mac=")]
    if len(mac_values) != 1:
        raise ConfigError("SmolVM QEMU NIC argv is incompatible with the external-network facade")
    device[mac_values[0]] = f"mac={backend.mac.lower()}"
    result[device_values[0]] = ",".join(device)
    return result


def build_qemu_argv(
    vm_info: VMInfo,
    *,
    qemu_bin: Path,
    boot_args: str,
    platform_spec: GuestPlatformSpec,
    external_network: ExternalNetworkBackend | None = None,
    control_socket_path: Path | None = None,
    firmware_vars_path: Path | None = None,
    swtpm_socket: Path | None = None,
    start_paused: bool = False,
    root_node_name: str = QEMU_ROOT_NODE_NAME,
    host_system: str | None = None,
) -> list[str]:
    """Build stock SmolVM argv, replacing only its single NIC when requested."""
    sandbox = False
    if external_network is not None:
        _validate_backend(external_network)
        require_external_network_support(qemu_bin)
        linux = (host_system or platform.system()) == "Linux"
        sandbox = linux and _qemu_sandbox_supported(qemu_bin)
        if linux and not sandbox:
            print(
                "sbx: warning: QEMU sandbox support is unavailable; confinement is reduced",
                file=sys.stderr,
            )

    argv = _build_smolvm_qemu_argv(
        vm_info,
        qemu_bin=qemu_bin,
        boot_args=boot_args,
        platform_spec=platform_spec,
        control_socket_path=control_socket_path,
        firmware_vars_path=firmware_vars_path,
        swtpm_socket=swtpm_socket,
        start_paused=start_paused,
        root_node_name=root_node_name,
        host_system=host_system,
    )
    if external_network is None:
        return argv
    if (host_system or platform.system()) == "Linux" and not os.access(
        "/dev/kvm", os.R_OK | os.W_OK
    ):
        argv = _use_tcg_without_kvm(argv)
    argv = _replace_network(argv, external_network)
    if sandbox:
        argv[1:1] = ["-sandbox", _QEMU_SANDBOX]
    return argv


# ponytail: private SmolVM lifecycle seams; remove when its typed backend API ships.
def start_vm(
    vm: Any,
    backend: ExternalNetworkBackend,
    *,
    boot_timeout: float,
) -> int:
    """Start one SmolVM facade through its typed stream backend, never SLIRP."""
    sdk = vm._sdk
    vm_info = sdk.state.get_vm(vm.vm_id)
    if vm_info.status not in (VMState.CREATED, VMState.STOPPED):
        raise SmolVMError(f"Cannot start VM in state '{vm_info.status.value}'")
    if vm_info.network is None:
        raise SmolVMError("VM has no network configuration")
    sdk._check_workspace_mounts(vm_info)
    context = sdk._runtime_context()

    def start_qemu(
        info: VMInfo,
        log_path: Path,
        *,
        control_socket_path: Path | None = None,
        start_paused: bool = False,
        root_node_name: str = QEMU_ROOT_NODE_NAME,
        firmware_vars_path: Path | None = None,
        swtpm_socket: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        override = os.environ.get("SBX_QEMU_BIN")
        qemu_bin = Path(override).expanduser() if override else sdk._find_qemu_binary()
        if qemu_bin is None:
            raise SmolVMError("qemu-system binary is missing")
        platform_spec = get_guest_platform(
            info.config.guest_os,
            host_system=platform.system(),
            arch=platform.machine(),
        )
        argv = build_qemu_argv(
            info,
            qemu_bin=qemu_bin,
            boot_args=sdk._resolve_boot_args(info),
            platform_spec=platform_spec,
            external_network=backend,
            control_socket_path=control_socket_path,
            firmware_vars_path=firmware_vars_path,
            swtpm_socket=swtpm_socket,
            start_paused=start_paused,
            root_node_name=root_node_name,
        )
        fd = os.open(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(fd, 0o600)
        log_file = os.fdopen(fd, "w")  # owned until the SmolVM runtime closes it.
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env={"HOME": str(sdk.data_dir), "LANG": "C.UTF-8", "PATH": os.defpath, "TZ": "UTC"},
            )
        except BaseException:
            log_file.close()
            raise
        sdk._log_files[f"qemu:{info.vm_id}"] = log_file
        sdk._process_handles[process.pid] = process
        return process

    context.start_qemu = start_qemu
    adapter = QemuRuntimeAdapter(context)
    log_path = sdk.data_dir / f"{vm.vm_id}.log"
    try:
        launch = adapter.start(vm_info, log_path=log_path, boot_timeout=boot_timeout)
        vm._info = sdk.state.update_vm(
            vm.vm_id,
            status=launch.status,
            pid=launch.pid,
            control_socket_path=launch.control_socket_path,
        )
        vm._reset_runtime_state(close_ssh=False)
        return launch.pid
    except BaseException:
        sdk.state.update_vm(
            vm.vm_id,
            status=VMState.ERROR,
            clear_pid=True,
            clear_socket_path=True,
        )
        with suppress(Exception):
            sdk._close_runtime_log(vm.vm_id, "qemu", vm_info.control_socket_path)
        raise
