import subprocess
from pathlib import Path

import pytest

from sbx import lifecycle_warnings

MIB = 1024 * 1024


def test_format_size() -> None:
    assert lifecycle_warnings.format_size(None) == "-"
    assert lifecycle_warnings.format_size(0) == "0B"
    assert lifecycle_warnings.format_size(512 * MIB) == "512M"
    assert lifecycle_warnings.format_size(1536 * MIB) == "1.5G"
    assert lifecycle_warnings.format_size(20 * 1024 * MIB) == "20G"
    assert lifecycle_warnings.format_size(3 * 1024**5) == "3072T"


def test_disk_usage_is_sparse_aware(tmp_path: Path) -> None:
    sparse = tmp_path / "rootfs.ext4"
    with sparse.open("wb") as handle:
        handle.truncate(1024 * MIB)
    (tmp_path / "kernel").write_bytes(b"x" * 10_000)

    file_usage = lifecycle_warnings.disk_usage_bytes(sparse)
    dir_usage = lifecycle_warnings.disk_usage_bytes(tmp_path)

    assert file_usage is not None and file_usage < MIB
    assert dir_usage is not None and dir_usage >= file_usage + 10_000
    assert lifecycle_warnings.disk_usage_bytes(tmp_path / "missing") is None
    assert lifecycle_warnings.rootfs_size_bytes(sparse) == 1024 * MIB


def test_qcow2_virtual_size_uses_force_share(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '{"virtual-size": 4096}', "")

    monkeypatch.setattr(lifecycle_warnings.shutil, "which", lambda name: "/usr/bin/qemu-img")
    monkeypatch.setattr(lifecycle_warnings.subprocess, "run", run)

    assert lifecycle_warnings.rootfs_size_bytes(Path("disk.qcow2")) == 4096
    assert lifecycle_warnings.rootfs_size_mib(Path("disk.qcow2")) == 1
    assert "--force-share" in commands[0]
