"""Opt-in booted-image gate: the built kernel must list Landlock as an active LSM.

Run against a built image directory (a skip does not satisfy the gate):

    SBX_LANDLOCK_TEST_IMAGE=~/.smolvm/images/sbx pytest tests/test_landlock_image.py --no-cov
"""

import json
import os
from pathlib import Path

import pytest
import smolvm

from sbx.constants import DEFAULT_BACKEND

IMAGE = os.environ.get("SBX_LANDLOCK_TEST_IMAGE")
CHECK = (
    "mountpoint -q /sys/kernel/security || mount -t securityfs securityfs /sys/kernel/security; "
    "uname -r; cat /sys/kernel/security/lsm; echo; grep -qw landlock /sys/kernel/security/lsm"
)


@pytest.mark.skipif(
    not IMAGE, reason="required manual gate: set SBX_LANDLOCK_TEST_IMAGE to a built image dir"
)
def test_booted_image_lists_landlock_lsm() -> None:
    image_dir = Path(str(IMAGE)).expanduser()
    manifest = json.loads((image_dir / "smolvm-image.json").read_text(encoding="utf-8"))
    private_key, public_key = smolvm.utils.ensure_ssh_key()
    config = smolvm.VMConfig(
        memory=512,
        kernel_path=image_dir / manifest["kernel"],
        rootfs_path=image_dir / manifest["rootfs"],
        boot_args=manifest["boot_args"],
        backend=DEFAULT_BACKEND,
        comm_channel="ssh",
        ssh_capable=True,
        ssh_public_key=public_key.read_text(encoding="utf-8").strip(),
    )
    with smolvm.SmolVM(config, ssh_key_path=str(private_key)) as vm:
        result = vm.run(CHECK, timeout=60)
    print(result.stdout, result.stderr)
    assert result.exit_code == 0, f"landlock missing from active LSMs:\n{result.stdout}"
