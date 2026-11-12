from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hardening import Status, sanitized_environment


class HardeningTests(unittest.TestCase):
    def test_linux_profile_denies_privileged_syscall_files_and_core_dumps(self) -> None:
        if sys.platform != "linux":
            return
        script = """
import ctypes, os, resource, sys
from pathlib import Path
from hardening import apply
confdir = Path(sys.argv[1]); confdir.mkdir(mode=0o700)
status = apply(confdir, 128, Path(sys.executable))
assert status.no_new_privs and status.seccomp and status.limits, status
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
assert 'Seccomp:\\t2' in Path('/proc/self/status').read_text()
if status.landlock:
    try:
        Path('/etc/passwd').read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('Landlock allowed an unrelated file')
libc = ctypes.CDLL(None, use_errno=True)
number = 272 if os.uname().machine == 'x86_64' else 97
assert libc.syscall(number, 0x20000) == -1 and ctypes.get_errno() == 1
print(status.encode())
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, str(Path(directory) / "conf")],
                cwd=Path(__file__).parent,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no_new_privs,seccomp,", result.stdout)
        self.assertIn(",rlimits", result.stdout)

    def test_environment_and_inherited_descriptors_are_minimal(self) -> None:
        environment = sanitized_environment(
            Path("/private/state"),
            Status(True, True, False, True),
            {"SBX_GATEWAY_KEY_FD": "7"},
        )
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertEqual(environment["HOME"], "/private/state")
        self.assertEqual(environment["SBX_GATEWAY_KEY_FD"], "7")


if __name__ == "__main__":
    unittest.main()
