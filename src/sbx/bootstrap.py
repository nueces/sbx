"""Transfer and invoke the packaged guest bootstrap over smolvm's public transport."""

import hashlib
import json
import os
import shlex
import sys
import tempfile
import uuid
from contextlib import suppress
from importlib.resources import files
from pathlib import PurePosixPath

from sbx.runtime import ConfigError

STAGE = "/var/lib/sbx/bootstrap"
PHASES = {
    "check",
    "account",
    "mounts",
    "credentials",
    "ssh",
    "hostname",
    "clock",
    "git",
    "tools",
    "kvm",
    "docker",
}
# The first upload cannot rely on the resource that is about to be uploaded.
PREPARE = """import json, os, pathlib, stat, subprocess
p = pathlib.Path("/var/lib/sbx/bootstrap")
for part in (*reversed(p.parents), p):
    if part.is_symlink():
        raise ValueError("symlinked bootstrap directory")
    part.mkdir(mode=0o700, exist_ok=True)
    s = part.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
        raise ValueError("bootstrap directory is not root-controlled")
mounts = subprocess.run(
    ["findmnt", "--json", "--list", "--output", "TARGET,FSTYPE"],
    check=True, capture_output=True, text=True, timeout=10,
)
for item in json.loads(mounts.stdout)["filesystems"]:
    shared = item["fstype"] in ("9p", "virtiofs") or (
        item["fstype"] == "overlay" and item["target"] != "/"
    )
    if shared and p.is_relative_to(pathlib.Path(item["target"])):
        raise ValueError("bootstrap storage is covered by an active shared folder")
p.chmod(0o700)
"""
PUBLISH = """import os, stat, sys
source, destination = sys.argv[1:]
s = os.lstat(source)
if not stat.S_ISREG(s.st_mode) or s.st_uid != 0:
    raise ValueError("untrusted bootstrap upload")
os.chmod(source, 0o600)
os.replace(source, destination)
"""


LEASE_PROBE = """import fcntl, os, stat, sys
try:
    fd = os.open("/var/lib/sbx/bootstrap/execution.lock", os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("untrusted execution lock")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("another provisioning phase is active; retry after it finishes", file=sys.stderr)
    sys.exit(75)
except (OSError, ValueError):
    print("cannot check guest provisioning state", file=sys.stderr)
    sys.exit(1)
"""
LEASE_CHECK = f"if [ -e {STAGE}/execution.lock ]; then python3 -c {shlex.quote(LEASE_PROBE)}; fi"


class GuestBootstrap:
    def __init__(self, vm, user: str, vm_name: str, env_names=()):
        self.vm = vm
        self.user = user
        self.vm_name = vm_name
        self.env_names = list(dict.fromkeys([*env_names, *getattr(vm.info.config, "env_vars", {})]))
        self.sensitive_values = set()
        self.mounts = []
        protected = PurePosixPath(STAGE)
        for index, mount in enumerate(vm.info.config.workspace_mounts):
            target = PurePosixPath(mount.guest_path)
            if target == protected or target in protected.parents or protected in target.parents:
                raise ConfigError("workspace must not cover bootstrap storage")
            self.mounts.append(
                {
                    "target": str(target),
                    "tag": mount.resolved_tag(index),
                    "writable": mount.writable,
                }
            )
        self.script = None

    def sanitized(self, text: str) -> str:
        values = self.sensitive_values.copy()
        configured = getattr(self.vm.info.config, "env_vars", {})
        for name in self.env_names:
            values.update((os.environ.get(name), configured.get(name)))
        for value in sorted(filter(None, values), key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    def checked(self, command: str, *, timeout: int = 30):
        result = self.vm.run(command, timeout=timeout)
        if result.exit_code != 0:
            details = self.sanitized((result.stderr or result.stdout).strip())
            raise ConfigError(f"guest bootstrap failed: {details or 'guest command failed'}")
        return result

    def upload(self, content: bytes, destination: str) -> None:
        incoming = f"{STAGE}/.upload-{uuid.uuid4().hex}"
        try:
            with tempfile.NamedTemporaryFile() as source:
                source.write(content)
                source.flush()
                self.vm.upload_file(source.name, incoming, make_dirs=False)
            command = "python3 -c " + shlex.quote(PUBLISH)
            self.checked(f"{command} {shlex.quote(incoming)} {shlex.quote(destination)}")
        except BaseException:
            # Only this uninvoked upload is disposable; other phase inputs may still be in use.
            with suppress(Exception):
                self.vm.run(f"rm -f -- {shlex.quote(incoming)}", timeout=10)
            raise

    def stage(self) -> None:
        if self.script is not None:
            return
        self.checked("python3 -c " + shlex.quote(PREPARE))
        content = files("sbx.image").joinpath("resources/scripts/sbx-bootstrap.py").read_bytes()
        self.script = f"{STAGE}/bootstrap-{hashlib.sha256(content).hexdigest()}.py"
        self.upload(content, self.script)

    def phase(self, name: str, arguments=None, *, timeout: int = 30):
        if name not in PHASES:
            raise ConfigError("unknown bootstrap phase")
        self.stage()
        config = {
            "version": 1,
            "phase": name,
            "user": self.user,
            "vm_name": self.vm_name,
            "mounts": self.mounts,
            "arguments": arguments or {},
            "env_names": self.env_names,
        }
        destination = f"{STAGE}/.input-{uuid.uuid4().hex}.json"
        self.upload(json.dumps(config).encode(), destination)
        command = shlex.join(
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=10s",
                f"{timeout}s",
                "python3",
                self.script,
                destination,
            ]
        )
        # On a disconnected/timed-out channel, leave root-only inputs intact for surviving work.
        result = self.vm.run(command, timeout=timeout + 15)
        try:
            if result.exit_code:
                details = self.sanitized((result.stderr or result.stdout).strip())
                raise ConfigError(f"guest {name} phase failed: {details or 'guest command failed'}")
            if result.stderr:
                # Successful phases may report skipped initial copies, never secret values.
                print(self.sanitized(result.stderr).rstrip(), file=sys.stderr)
            return result
        finally:
            with suppress(Exception):
                self.vm.run(f"rm -f -- {shlex.quote(destination)}", timeout=10)

    def run(self, command, timeout=30, shell="login"):
        """CommChannel adapter for the existing public upstream preset installer."""
        self.stage()
        guarded = shlex.join(
            [
                "flock",
                "--nonblock",
                "--no-fork",
                "--conflict-exit-code",
                "75",
                f"{STAGE}/execution.lock",
                "timeout",
                "--signal=TERM",
                "--kill-after=10s",
                f"{timeout}s",
                "sh",
                "-c",
                command,
            ]
        )
        result = self.vm.run(guarded, timeout=timeout + 15, shell=shell)
        stderr = (
            "another provisioning phase is active; retry after it finishes"
            if result.exit_code == 75
            else self.sanitized(result.stderr)
        )
        return result.model_copy(update={"stdout": self.sanitized(result.stdout), "stderr": stderr})

    def put_file(self, source, destination):
        self.phase("check")
        # SFTP preserves an existing mode, but otherwise creates world-readable archives.
        result = self.run(shlex.join(["install", "-m", "600", "--", "/dev/null", destination]))
        if result.exit_code:
            raise ConfigError(f"could not prepare private preset transfer: {result.stderr.strip()}")
        return self.vm.upload_file(source, destination, make_dirs=False)

    def supports(self, *capabilities):
        return any(value in ("env_managed", "env.managed") for value in capabilities)

    def set_managed_env(self, values, *, merge=True):
        self.env_names = list(dict.fromkeys([*self.env_names, *values]))
        self.sensitive_values.update(values.values())
        self.phase("check")
        self.vm.set_env_vars(values, merge=merge)
        return self.vm.list_env_vars()
