"""Runtime patches for the pinned smolvm release.

shortcut: raises smolvm 0.0.28's VMConfig.memory cap from 16384 to 32768 MiB (upstream
Celesto #605); delete this module when migrating to celesto>=0.1.3.

shortcut: moves the microvm machine's virtio devices from virtio-mmio to PCI. QEMU's
userspace IOAPIC can leave a level-triggered virtio-mmio interrupt line stuck, which hangs
the device (seen on 9p shared folders). PCI devices use MSI-X and avoid that path. Remove
when sbx moves to a Celesto release that attaches microvm devices to PCI itself.
"""

import os
from collections.abc import Sequence
from typing import Any

import smolvm.vm as _smolvm_vm
from annotated_types import Le
from smolvm.types import SnapshotInfo, VMConfig, VMInfo

MAX_MEMORY_MIB = 32768

_memory = VMConfig.model_fields["memory"]
_memory.metadata = [Le(MAX_MEMORY_MIB) if isinstance(m, Le) else m for m in _memory.metadata]
# VMInfo and SnapshotInfo embed VMConfig's schema, so they must be rebuilt too.
for _model in (VMConfig, VMInfo, SnapshotInfo):
    _model.model_rebuild(force=True)

VIRTIO_BUS_ENV = "SBX_QEMU_VIRTIO_BUS"
_ROOT_DRIVE = "drive=rootdisk0-drive"
_MMIO_TO_PCI = {
    "virtio-blk-device": "virtio-blk-pci",
    "virtio-9p-device": "virtio-9p-pci",
    "virtio-net-device": "virtio-net-pci",
    "vhost-vsock-device": "vhost-vsock-pci",
}


def microvm_argv_to_pci(argv: Sequence[str]) -> list[str]:
    """Return ``argv`` with a microvm machine's virtio-mmio devices moved to PCI.

    Command lines for other machines are returned unchanged.
    """
    args = list(argv)
    if "-machine" not in args[:-1]:
        return args
    machine_at = args.index("-machine") + 1
    machine_options = args[machine_at].split(",")
    if machine_options[0] != "microvm" or "pcie=off" not in machine_options:
        return args

    rest: list[str] = []
    devices: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "-device" and index + 1 < len(args):
            model, separator, properties = args[index + 1].partition(",")
            if model not in _MMIO_TO_PCI:
                raise RuntimeError(
                    f"sbx: cannot move microvm device {model!r} to PCI; "
                    f"set {VIRTIO_BUS_ENV}=mmio to keep virtio-mmio devices"
                )
            devices.append(_MMIO_TO_PCI[model] + separator + properties)
            index += 2
        else:
            rest.append(args[index])
            index += 1
    rest[rest.index("-machine") + 1] = ",".join(
        "pcie=on" if option == "pcie=off" else option for option in machine_options
    )

    # virtio-mmio enumerates in reverse command-line order, PCI in slot order: the root
    # disk goes first to stay /dev/vda, and extra drives are reversed to keep their names.
    disks = [device for device in devices if device.startswith("virtio-blk-pci")]
    root = [disk for disk in disks if _ROOT_DRIVE in disk.split(",")]
    extra = [disk for disk in disks if disk not in root]
    others = [device for device in devices if device not in disks]
    for device in [*root, *reversed(extra), *others]:
        rest.extend(["-device", device])
    return rest


def _virtio_bus() -> str:
    value = os.environ.get(VIRTIO_BUS_ENV, "").strip().lower() or "pci"
    if value not in ("pci", "mmio"):
        raise RuntimeError(f"sbx: {VIRTIO_BUS_ENV} must be 'pci' or 'mmio', got {value!r}")
    return value


_smolvm_build_qemu_argv = _smolvm_vm.build_qemu_argv


def _build_qemu_argv(*args: Any, **kwargs: Any) -> list[str]:
    argv = _smolvm_build_qemu_argv(*args, **kwargs)
    return argv if _virtio_bus() == "mmio" else microvm_argv_to_pci(argv)


# smolvm.vm imports build_qemu_argv by name, so the patch must replace that binding.
_smolvm_vm.build_qemu_argv = _build_qemu_argv
