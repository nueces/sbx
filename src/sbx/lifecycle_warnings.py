import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from sbx.runtime import ConfigError

MIB = 1024 * 1024


def cfg(config: Mapping[str, Any], section: str, key: str, default: Any = None) -> Any:
    value = config.get(section, {})
    if not isinstance(value, Mapping):
        return default
    return value.get(key, default)


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ceil_mib(size_bytes: int) -> int:
    return (size_bytes + MIB - 1) // MIB


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "-"
    value = float(size_bytes)
    units = ["B", "K", "M", "G", "T"]
    while value >= 1024 and len(units) > 1:
        value /= 1024
        units.pop(0)
    return f"{value:.1f}".removesuffix(".0") + units[0]


def disk_usage_bytes(path: Path | None) -> int | None:
    """Host-allocated bytes (sparse-aware) of a file or of all files under a directory."""
    if path is None:
        return None
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return info.st_blocks * 512 if stat.S_ISREG(info.st_mode) else 0
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                file_info = os.lstat(os.path.join(root, name))
                if stat.S_ISREG(file_info.st_mode):
                    total += file_info.st_blocks * 512
        return total
    except OSError:
        return None


def qcow2_virtual_size_bytes(path: Path) -> int | None:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        return None
    # --force-share: a running QEMU holds a write lock on its disk.
    result = subprocess.run(
        [qemu_img, "info", "--force-share", "--output=json", str(path)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
        virtual_size = int(info["virtual-size"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return virtual_size


def rootfs_size_bytes(rootfs_path: Path | None) -> int | None:
    if rootfs_path is None:
        return None
    if rootfs_path.suffix.lower() == ".qcow2":
        return qcow2_virtual_size_bytes(rootfs_path)
    try:
        return rootfs_path.stat().st_size
    except OSError:
        return None


def rootfs_size_mib(rootfs_path: Path) -> int | None:
    size = rootfs_size_bytes(rootfs_path)
    return ceil_mib(size) if size is not None else None


def path_from_config(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(str(value)).expanduser()


def local_image_manifest(image: Path) -> dict[str, Any]:
    if not image.is_dir():
        raise ConfigError("[sbx].image must point to a local image directory")
    manifest_path = image / "smolvm-image.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"image manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid image manifest JSON: {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("image manifest must be a JSON object")
    return raw


def manifest_path(image_dir: Path, manifest: Mapping[str, Any], key: str) -> Path:
    value = manifest.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"image manifest requires string field {key!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = image_dir / path
    return path


def local_image_rootfs_size_mib(config: Mapping[str, Any]) -> int | None:
    image = path_from_config(cfg(config, "sbx", "image"))
    if image is None:
        return None
    with suppress(ConfigError):
        manifest = local_image_manifest(image)
        rootfs_path = manifest_path(image, manifest, "rootfs")
        if rootfs_path.is_file():
            return rootfs_size_mib(rootfs_path)
    return None


def local_image_disk_size_error(configured_disk_size: int, image_size: int) -> str:
    return (
        "configured disk_size is smaller than the local image rootfs:\n"
        f"  disk_size: {configured_disk_size} MiB\n"
        f"  local image rootfs: {image_size} MiB\n"
        f"Set [sbx].disk_size to at least {image_size}, remove [sbx].disk_size, "
        "or rebuild the configured local image with a rootfs no larger than "
        f"{configured_disk_size} MiB."
    )


def local_image_config_warnings(config: Mapping[str, Any]) -> list[str]:
    configured_disk_size = int_or_none(cfg(config, "sbx", "disk_size"))
    if configured_disk_size is None:
        return []
    image_size = local_image_rootfs_size_mib(config)
    if image_size is None or configured_disk_size >= image_size:
        return []
    return [local_image_disk_size_error(configured_disk_size, image_size)]


def existing_vm_config_mismatches(
    name: str,
    config: Mapping[str, Any],
    *,
    smolvm_info: Callable[[str], Mapping[str, Any] | None],
) -> list[str]:
    vm = smolvm_info(name)
    if vm is None:
        return []

    checks = (
        ("disk_size", "disk_size", " MiB"),
        ("memory", "memory", " MiB"),
        ("cpus", "vcpus", ""),
    )
    mismatches: list[str] = []
    for config_key, vm_key, unit in checks:
        configured = int_or_none(cfg(config, "sbx", config_key))
        existing = int_or_none(vm.get(vm_key))
        if configured is None or existing is None or configured == existing:
            continue
        mismatches.append(
            f"{config_key}: config requests {configured}{unit}, existing VM has {existing}{unit}"
        )
    return mismatches


def doctor_config_state(
    config: Mapping[str, Any], *, smolvm_info: Callable[[str], Mapping[str, Any] | None]
) -> None:
    name = cfg(config, "sbx", "name")
    if not name:
        return

    vm_name = str(name)
    mismatches = existing_vm_config_mismatches(vm_name, config, smolvm_info=smolvm_info)
    image_warnings = local_image_config_warnings(config)
    if not mismatches and not image_warnings:
        return

    print("sbx config/state:")
    if mismatches:
        print(f"  warning: VM '{vm_name}' already exists and differs from .sbx.toml:")
        for mismatch in mismatches:
            print(f"    {mismatch}")
    if image_warnings:
        print("  warning: configured disk_size is smaller than the local image rootfs:")
        for warning in image_warnings:
            for line in warning.splitlines():
                print(f"    {line}")
    if any(not m.startswith("disk_size:") for m in mismatches):
        print(f"  Run `sbx stop {vm_name}` then `sbx run {vm_name}` to apply memory/cpus changes.")
    if any(m.startswith("disk_size:") for m in mismatches):
        print(
            "  Existing VM disks are reused as-is. "
            f"Run `sbx recreate {vm_name} --force` to apply disk_size changes."
        )
