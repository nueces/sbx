"""Typed SmolVM compatibility facade for one external QEMU network backend."""

from __future__ import annotations

import os
import platform
import re
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal

from smolvm.exceptions import SmolVMError
from smolvm.runtime.guest_platforms import GuestPlatformSpec, get_guest_platform
from smolvm.runtime.qemu import QEMU_ROOT_NODE_NAME, QemuRuntimeAdapter
from smolvm.runtime.qemu_args import build_qemu_argv as _build_smolvm_qemu_argv
from smolvm.types import VMInfo, VMState

from sbx.runtime import ConfigError

_MINIMUM_QEMU_VERSION = (7, 2, 0)
_SUPPORTED_SMOLVM_VERSIONS = {"0.0.28"}
_UNIX_SOCKET_PATH_MAX_BYTES = 103
_MAC_PATTERN = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")
_QEMU_VERSION_PATTERN = re.compile(r"QEMU emulator version (\d+)\.(\d+)(?:\.(\d+))?")


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
    if len(os.fsencode(path)) > _UNIX_SOCKET_PATH_MAX_BYTES:
        raise ConfigError(
            f"gateway QEMU socket path exceeds {_UNIX_SOCKET_PATH_MAX_BYTES} filesystem bytes"
        )
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ConfigError(f"gateway QEMU socket is not ready: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISSOCK(mode):
        raise ConfigError(f"gateway QEMU socket path is not a Unix socket: {path}")


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
    """Fail before launch unless the pinned SmolVM and QEMU support stream netdevs."""
    smolvm_version = package_version("smolvm")
    if smolvm_version not in _SUPPORTED_SMOLVM_VERSIONS:
        expected = ", ".join(sorted(_SUPPORTED_SMOLVM_VERSIONS))
        raise ConfigError(
            f"gateway external networking supports SmolVM {expected}; found {smolvm_version}"
        )

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

    result = list(argv)
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
    if external_network is not None:
        _validate_backend(external_network)
        require_external_network_support(qemu_bin)

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
    return argv if external_network is None else _replace_network(argv, external_network)


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
        qemu_bin = sdk._find_qemu_binary()
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
        log_file = open(log_path, "w")  # noqa: SIM115 - owned until runtime closes.
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
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
