import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

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
