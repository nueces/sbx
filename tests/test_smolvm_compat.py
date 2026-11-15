import sys

import pytest
from pydantic import ValidationError
from smolvm.types import VMConfig, VMInfo

import sbx.smolvm_compat  # noqa: F401

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
    import subprocess

    from sbx import runtime

    code = runtime.SMOLVM_CLI_BOOTSTRAP.split("; from smolvm.cli.main")[0] + (
        "; from smolvm.types import VMConfig;"
        " VMConfig.model_validate({'memory': 32768, 'kernel_path': '/k', 'rootfs_path': '/r'},"
        " context={'validate_paths': False})"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
