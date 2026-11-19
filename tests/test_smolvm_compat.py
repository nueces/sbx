import subprocess
import sys
from pathlib import Path

import pytest
import smolvm.vm
from pydantic import ValidationError
from smolvm.runtime.guest_platforms import _LINUX_SPEC
from smolvm.types import VMConfig, VMInfo

from sbx import runtime, smolvm_compat
from sbx.smolvm_compat import microvm_argv_to_pci

CTX = {"validate_paths": False}


def _config(memory: int) -> VMConfig:
    return VMConfig.model_validate(
        {"memory": memory, "kernel_path": "/k", "rootfs_path": "/r"}, context=CTX
    )


def test_vm_config_accepts_32_gib_and_rejects_more() -> None:
    assert _config(32768).memory == 32768
    with pytest.raises(ValidationError, match="memory"):
        _config(32769)


def test_vm_info_accepts_32_gib_config() -> None:
    config = _config(32768)
    info = VMInfo.model_validate(
        {"vm_id": config.vm_id, "status": "stopped", "config": config.model_dump()}, context=CTX
    )
    assert info.config.memory == 32768


def test_smolvm_cli_subprocess_accepts_32_gib_config() -> None:
    code = runtime.SMOLVM_CLI_BOOTSTRAP.split("; from smolvm.cli.main")[0] + (
        "; from smolvm.types import VMConfig;"
        " VMConfig.model_validate({'memory': 32768, 'kernel_path': '/k', 'rootfs_path': '/r'},"
        " context={'validate_paths': False})"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


MICROVM = "microvm,accel=kvm,acpi=on,pcie=off,pic=off,pit=off,rtc=on"
MICROVM_PCI = "microvm,accel=kvm,acpi=on,pcie=on,pic=off,pit=off,rtc=on"
ROOT = "drive=rootdisk0-drive"


def _devices(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == "-device"]


def _microvm_argv(*devices: str) -> list[str]:
    argv = ["qemu", "-smp", "4", "-qmp", "unix:/q", "-machine", MICROVM, "-cpu", "host"]
    for device in devices:
        argv.extend(["-device", device])
    return argv


def test_microvm_devices_move_to_pci_with_root_disk_first() -> None:
    argv = microvm_argv_to_pci(
        _microvm_argv(
            "virtio-9p-device,fsdev=fs0,mount_tag=workspace0",
            "virtio-9p-device,fsdev=fs1,mount_tag=workspace1",
            "virtio-net-device,netdev=net0,mac=aa:fc:00:00:00:01",
            f"virtio-blk-device,{ROOT}",
        )
    )
    assert argv[argv.index("-machine") + 1] == MICROVM_PCI
    assert _devices(argv) == [
        f"virtio-blk-pci,{ROOT}",
        "virtio-9p-pci,fsdev=fs0,mount_tag=workspace0",
        "virtio-9p-pci,fsdev=fs1,mount_tag=workspace1",
        "virtio-net-pci,netdev=net0,mac=aa:fc:00:00:00:01",
    ]
    # Everything that is not a device keeps its order.
    assert [a for a in argv if a not in ("-device", *_devices(argv))] == [
        "qemu", "-smp", "4", "-qmp", "unix:/q", "-machine", MICROVM_PCI, "-cpu", "host"
    ]  # fmt: skip


def test_microvm_extra_drives_keep_guest_order_and_vsock_maps() -> None:
    argv = microvm_argv_to_pci(
        _microvm_argv(
            "virtio-blk-device,drive=extra0-drive",
            "virtio-blk-device,drive=extra1-drive",
            "virtio-net-device,netdev=net0",
            f"virtio-blk-device,{ROOT}",
            "vhost-vsock-device,guest-cid=7",
        )
    )
    assert _devices(argv) == [
        f"virtio-blk-pci,{ROOT}",
        "virtio-blk-pci,drive=extra1-drive",
        "virtio-blk-pci,drive=extra0-drive",
        "virtio-net-pci,netdev=net0",
        "vhost-vsock-pci,guest-cid=7",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["qemu", "-machine", "q35,accel=kvm", "-device", "virtio-blk-pci,drive=x"],
        ["qemu", "-machine", "virt,accel=kvm", "-device", "virtio-blk-device,drive=x"],
        ["qemu", "-machine", MICROVM_PCI, "-device", "virtio-blk-pci,drive=x"],
        ["qemu", "-device", "virtio-blk-device,drive=x"],
    ],
)
def test_other_machines_are_unchanged(argv: list[str]) -> None:
    assert microvm_argv_to_pci(argv) == argv


def test_unknown_microvm_device_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="'virtio-serial-device'.*SBX_QEMU_VIRTIO_BUS=mmio"):
        microvm_argv_to_pci(_microvm_argv("virtio-serial-device", f"virtio-blk-device,{ROOT}"))


def _smolvm_argv() -> list[str]:
    config = VMConfig.model_validate(
        {
            "vm_id": "pci-test",
            "kernel_path": "/k",
            "rootfs_path": "/r.qcow2",
            "workspace_mounts": [{"host_path": "/w"}],
        },
        context=CTX,
    )
    info = VMInfo.model_validate(
        {
            "vm_id": config.vm_id,
            "status": "stopped",
            "config": config.model_dump(),
            "network": {
                "tap_device": "tap0",
                "guest_ip": "10.0.2.15",
                "gateway_ip": "10.0.2.2",
                "guest_mac": "AA:FC:00:00:00:01",
                "ssh_host_port": 2200,
            },
        },
        context=CTX,
    )
    return smolvm.vm.build_qemu_argv(
        info,
        qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
        boot_args="console=ttyS0",
        platform_spec=_LINUX_SPEC,
        host_system="Linux",
    )


def test_smolvm_builder_emits_pci_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(smolvm_compat.VIRTIO_BUS_ENV, raising=False)
    monkeypatch.delenv("SMOLVM_QEMU_MACHINE", raising=False)
    argv = _smolvm_argv()
    assert argv[argv.index("-machine") + 1] == MICROVM_PCI
    assert [device.split(",")[0] for device in _devices(argv)] == [
        "virtio-blk-pci",
        "virtio-9p-pci",
        "virtio-net-pci",
    ]


def test_virtio_bus_opt_out_and_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMOLVM_QEMU_MACHINE", raising=False)
    monkeypatch.setenv(smolvm_compat.VIRTIO_BUS_ENV, "mmio")
    argv = _smolvm_argv()
    assert argv[argv.index("-machine") + 1] == MICROVM
    assert all(device.split(",")[0].endswith("-device") for device in _devices(argv))

    monkeypatch.setenv(smolvm_compat.VIRTIO_BUS_ENV, "isa")
    with pytest.raises(runtime.ConfigError, match="must be 'pci' or 'mmio', got 'isa'"):
        _smolvm_argv()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "pci"), ("", "pci"), (" PCI ", "pci"), ("MMIO", "mmio")],
)
def test_virtio_bus_values(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    if value is None:
        monkeypatch.delenv(smolvm_compat.VIRTIO_BUS_ENV, raising=False)
    else:
        monkeypatch.setenv(smolvm_compat.VIRTIO_BUS_ENV, value)
    assert smolvm_compat.virtio_bus() == expected


def test_smolvm_cli_subprocess_installs_pci_rewrite() -> None:
    code = runtime.SMOLVM_CLI_BOOTSTRAP.split("; from smolvm.cli.main")[0] + (
        "; import smolvm.vm; assert smolvm.vm.build_qemu_argv.__module__ == 'sbx.smolvm_compat'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
