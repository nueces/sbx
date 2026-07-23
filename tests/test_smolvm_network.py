import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from smolvm.runtime.guest_platforms import _LINUX_SPEC
from smolvm.runtime.qemu_args import build_qemu_argv as build_stock_qemu_argv
from smolvm.types import NetworkConfig, VMConfig, VMInfo, VMState

from sbx import smolvm_network
from sbx.runtime import ConfigError


@pytest.fixture
def unix_socket(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "qemu-net.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    try:
        yield path
    finally:
        listener.close()


@pytest.fixture
def vm_info(tmp_path: Path) -> VMInfo:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    return VMInfo(
        vm_id="vm-gateway",
        status=VMState.CREATED,
        config=VMConfig(
            vm_id="vm-gateway",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            qemu_machine="q35",
            boot_args="console=ttyS0 reboot=k panic=1 init=/init",
        ),
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:00:00:01",
            ssh_host_port=2200,
        ),
    )


def completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def support_capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if argv[-1] == "--version":
        return completed("QEMU emulator version 10.0.11 (Debian)\n")
    return completed("Available netdev backend types:\nstream\nuser\ntap\n")


def backend(path: Path) -> smolvm_network.ExternalNetworkBackend:
    return smolvm_network.ExternalNetworkBackend(
        kind="unix-stream",
        socket_path=path,
        role="qemu-connects",
        mac="52:54:00:12:34:56",
    )


def test_external_network_builds_exact_single_stream_argv(
    monkeypatch: pytest.MonkeyPatch,
    vm_info: VMInfo,
    unix_socket: Path,
) -> None:
    monkeypatch.setattr(smolvm_network, "_capture", support_capture)

    argv = smolvm_network.build_qemu_argv(
        vm_info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args=vm_info.config.boot_args,
        platform_spec=_LINUX_SPEC,
        external_network=backend(unix_socket),
        host_system="Linux",
    )

    assert argv == [
        "/usr/bin/qemu-system-x86_64",
        "-smp",
        "2",
        "-m",
        "512",
        "-kernel",
        str(vm_info.config.kernel_path),
        "-append",
        "console=ttyS0 reboot=k panic=1 init=/init",
        "-drive",
        (
            f"file={vm_info.config.rootfs_path},if=none,format=raw,"
            "id=rootdisk0-drive,node-name=rootdisk0"
        ),
        "-netdev",
        f"stream,id=net0,server=off,addr.type=unix,addr.path={unix_socket}",
        "-nographic",
        "-no-reboot",
        "-rtc",
        "base=utc,clock=host",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-device",
        "virtio-blk-pci,drive=rootdisk0-drive",
        "-device",
        "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56",
    ]
    netdevs = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-netdev"]
    nics = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "-device" and "netdev=" in argv[index + 1]
    ]
    assert netdevs == [f"stream,id=net0,server=off,addr.type=unix,addr.path={unix_socket}"]
    assert len(nics) == 1
    assert not any(mode in " ".join(argv) for mode in ("user,id=", "tap,id=", "hostfwd="))


def test_non_gateway_argv_is_stock_smolvm_argv(
    monkeypatch: pytest.MonkeyPatch, vm_info: VMInfo
) -> None:
    monkeypatch.setattr(
        smolvm_network,
        "require_external_network_support",
        lambda _path: (_ for _ in ()).throw(AssertionError("gateway probe must not run")),
    )
    options = {
        "qemu_bin": Path("/usr/bin/qemu-system-x86_64"),
        "boot_args": vm_info.config.boot_args,
        "platform_spec": _LINUX_SPEC,
        "host_system": "Linux",
    }

    assert smolvm_network.build_qemu_argv(vm_info, **options) == build_stock_qemu_argv(
        vm_info, **options
    )


@pytest.mark.parametrize("version", ["6.2.0", "7.1.99"])
def test_qemu_before_7_2_fails_clearly(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(
        smolvm_network,
        "_capture",
        lambda _argv: completed(f"QEMU emulator version {version}\n"),
    )

    with pytest.raises(ConfigError, match=r"requires QEMU 7\.2 or newer"):
        smolvm_network.require_external_network_support(Path("/usr/bin/qemu"))


def test_qemu_without_stream_backend_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    def capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--version":
            return completed("QEMU emulator version 7.2.0\n")
        return completed("Available netdev backend types:\nuser\ntap\n")

    monkeypatch.setattr(smolvm_network, "_capture", capture)

    with pytest.raises(ConfigError, match="required 'stream' netdev backend"):
        smolvm_network.require_external_network_support(Path("/usr/bin/qemu"))


def test_unsupported_smolvm_version_fails_before_qemu_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smolvm_network, "package_version", lambda _name: "0.0.29")
    monkeypatch.setattr(
        smolvm_network,
        "_capture",
        lambda _argv: (_ for _ in ()).throw(AssertionError("QEMU probe must not run")),
    )

    with pytest.raises(ConfigError, match="supports SmolVM 0.0.28; found 0.0.29"):
        smolvm_network.require_external_network_support(Path("/usr/bin/qemu"))


@pytest.mark.parametrize(
    ("kind", "role", "message"),
    [
        ("tap", "qemu-connects", "unsupported external network backend"),
        ("unix-stream", "qemu-listens", "unsupported external network role"),
    ],
)
def test_unsupported_external_backend_fails_clearly(
    kind: str,
    role: str,
    message: str,
    vm_info: VMInfo,
    unix_socket: Path,
) -> None:
    external = smolvm_network.ExternalNetworkBackend(
        kind=kind,  # type: ignore[arg-type]
        socket_path=unix_socket,
        role=role,  # type: ignore[arg-type]
        mac="52:54:00:12:34:56",
    )

    with pytest.raises(ConfigError, match=message):
        smolvm_network.build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            external_network=external,
            host_system="Linux",
        )


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (Path("relative.sock"), "must be absolute"),
        (Path("/tmp/bad,socket"), "cannot contain"),
        (Path("/" + "x" * 104), "exceeds 103 filesystem bytes"),
    ],
)
def test_malformed_socket_paths_fail_clearly(
    path: Path,
    message: str,
    vm_info: VMInfo,
) -> None:
    with pytest.raises(ConfigError, match=message):
        smolvm_network.build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            external_network=backend(path),
            host_system="Linux",
        )


def test_start_vm_uses_external_backend_for_the_runtime_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vm_info: VMInfo,
    unix_socket: Path,
) -> None:
    events: list[object] = []

    class State:
        def get_vm(self, vm_id: str) -> VMInfo:
            assert vm_id == vm_info.vm_id
            return vm_info

        def update_vm(self, vm_id: str, **values: object) -> VMInfo:
            events.append(("update", vm_id, values))
            return vm_info

    class Process:
        pid = 7654

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            events.append(("popen", argv, kwargs))

    context = SimpleNamespace(start_qemu=None)
    qemu = tmp_path / "qemu-system-x86_64"
    qemu.touch()
    sdk = SimpleNamespace(
        state=State(),
        data_dir=tmp_path,
        _log_files={},
        _process_handles={},
        _check_workspace_mounts=lambda info: events.append(("mount-check", info.vm_id)),
        _runtime_context=lambda: context,
        _find_qemu_binary=lambda: qemu,
        _resolve_boot_args=lambda info: info.config.boot_args,
        _close_runtime_log=lambda *args: events.append(("close-log", args)),
    )
    vm = SimpleNamespace(
        vm_id=vm_info.vm_id,
        _sdk=sdk,
        _info=vm_info,
        _reset_runtime_state=lambda **kwargs: events.append(("reset", kwargs)),
    )
    external = backend(unix_socket)

    class Adapter:
        def __init__(self, adapter_context: object) -> None:
            assert adapter_context is context

        def start(self, info: VMInfo, *, log_path: Path, boot_timeout: float) -> object:
            assert boot_timeout == 12
            process = context.start_qemu(info, log_path)
            return SimpleNamespace(
                status=VMState.RUNNING,
                pid=process.pid,
                control_socket_path=tmp_path / "control.sock",
            )

    def build(info: VMInfo, **kwargs: Any) -> list[str]:
        assert info is vm_info
        assert kwargs["external_network"] == external
        return [str(qemu), "-netdev", "stream"]

    monkeypatch.setattr(smolvm_network, "QemuRuntimeAdapter", Adapter)
    monkeypatch.setattr(smolvm_network, "get_guest_platform", lambda *args, **kwargs: _LINUX_SPEC)
    monkeypatch.setattr(smolvm_network, "build_qemu_argv", build)
    monkeypatch.setattr(smolvm_network.subprocess, "Popen", Process)

    assert smolvm_network.start_vm(vm, external, boot_timeout=12) == 7654
    assert sdk._process_handles[7654].pid == 7654
    assert any(event[0] == "popen" for event in events if isinstance(event, tuple))
    assert ("reset", {"close_ssh": False}) in events
    for stream in sdk._log_files.values():
        stream.close()


def test_start_vm_failure_marks_state_error_and_closes_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vm_info: VMInfo,
    unix_socket: Path,
) -> None:
    events: list[object] = []

    class State:
        def get_vm(self, _vm_id: str) -> VMInfo:
            return vm_info

        def update_vm(self, _vm_id: str, **values: object) -> VMInfo:
            events.append(values)
            return vm_info

    class Adapter:
        def __init__(self, _context: object) -> None:
            pass

        def start(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("QEMU failed")

    sdk = SimpleNamespace(
        state=State(),
        data_dir=tmp_path,
        _check_workspace_mounts=lambda _info: None,
        _runtime_context=SimpleNamespace,
        _close_runtime_log=lambda *args: events.append(("close", args)),
    )
    vm = SimpleNamespace(vm_id=vm_info.vm_id, _sdk=sdk)
    monkeypatch.setattr(smolvm_network, "QemuRuntimeAdapter", Adapter)

    with pytest.raises(RuntimeError, match="QEMU failed"):
        smolvm_network.start_vm(vm, backend(unix_socket), boot_timeout=1)

    assert any(value.get("status") is VMState.ERROR for value in events if isinstance(value, dict))
    assert any(value[0] == "close" for value in events if isinstance(value, tuple))


def test_start_vm_rejects_invalid_state_and_missing_network(
    vm_info: VMInfo, unix_socket: Path
) -> None:
    state = SimpleNamespace(
        get_vm=lambda _vm_id: vm_info.model_copy(update={"status": VMState.RUNNING})
    )
    vm = SimpleNamespace(vm_id=vm_info.vm_id, _sdk=SimpleNamespace(state=state))
    with pytest.raises(Exception, match="Cannot start VM"):
        smolvm_network.start_vm(vm, backend(unix_socket), boot_timeout=1)

    state.get_vm = lambda _vm_id: vm_info.model_copy(update={"network": None})
    with pytest.raises(Exception, match="no network configuration"):
        smolvm_network.start_vm(vm, backend(unix_socket), boot_timeout=1)


def test_socket_path_must_name_an_existing_unix_socket(tmp_path: Path, vm_info: VMInfo) -> None:
    regular_file = tmp_path / "not-a-socket"
    regular_file.touch()

    with pytest.raises(ConfigError, match="is not a Unix socket"):
        smolvm_network.build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            external_network=backend(regular_file),
            host_system="Linux",
        )
