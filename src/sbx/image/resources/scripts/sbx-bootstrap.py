#!/usr/bin/env python3
"""Trusted, root-run sbx provisioning phases. Configuration is JSON data, never code."""

import base64
import ctypes
import errno
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

ROOT_UID = 0
STAGE = Path("/var/lib/sbx/bootstrap")
ETC = Path("/etc")
ROOT_HOME = Path("/root")
LOWER_ROOT = Path("/mnt")
UPPER_ROOT = Path("/tmp")
LOCAL_BIN = Path("/usr/local/bin")
FILESYSTEMS = Path("/proc/filesystems")
COPY_MARKER = ".sbx-incomplete-copy"
COPY_MAGIC = "sbx-bootstrap-copy-v1\n"
PHASES = {
    "check": set(),
    "account": set(),
    "mounts": set(),
    "credentials": set(),
    "ssh": {"public_key"},
    "hostname": set(),
    "clock": {"timestamp", "timezone"},
    "git": {"text"},
    "tools": {"tools", "install_timeout"},
    "kvm": set(),
    "docker": set(),
}
USER = re.compile(r"[a-z_][a-z0-9_-]*[$]?", re.I)
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def path_value(value):
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("expected an absolute guest path")
    if (
        value.startswith("//")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or ".." in value.split("/")
    ):
        raise ValueError("unsafe guest path")
    return Path(value)


def under(path, parent):
    return path == parent or parent in path.parents


def no_symlinks(path):
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError(f"symlinked guest path requires explicit repair: {part}")


def secure_directory(path):
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("bootstrap directory must not contain symlinks")
        if not part.exists():
            part.mkdir(mode=0o700)
        info = part.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise ValueError("bootstrap directory must have trusted ownership and permissions")
    mounts = subprocess.run(
        ["findmnt", "--json", "--list", "--output", "TARGET,FSTYPE"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if mounts.returncode:
        raise ValueError("cannot verify bootstrap storage")
    for item in json.loads(mounts.stdout)["filesystems"]:
        shared = item["fstype"] in ("9p", "virtiofs") or (
            item["fstype"] == "overlay" and item["target"] != "/"
        )
        if shared and under(path, Path(item["target"])):
            raise ValueError("bootstrap storage is covered by an active shared folder")
    path.chmod(0o700)


def publish_noreplace(source, destination):
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise ValueError("guest libc needs renameat2 for safe initial credential copying")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
        return
    error = ctypes.get_errno()
    if error != errno.EEXIST:
        raise OSError(error, os.strerror(error))


def secure_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o077:
            raise ValueError("bootstrap input must be a private root-owned file")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)


class Runtime:
    def __init__(self, config):
        required = {"version", "phase", "user", "vm_name", "mounts", "arguments", "env_names"}
        if (
            not isinstance(config, dict)
            or set(config) != required
            or type(config["version"]) is not int
            or config["version"] != 1
        ):
            raise ValueError("unsupported bootstrap configuration")
        self.phase = config["phase"]
        self.user = config["user"]
        self.name = config["vm_name"]
        if not isinstance(self.phase, str) or self.phase not in PHASES:
            raise ValueError("unknown bootstrap phase")
        if not isinstance(self.user, str) or not USER.fullmatch(self.user):
            raise ValueError("invalid runtime user")
        if not isinstance(self.name, str) or not NAME.fullmatch(self.name):
            raise ValueError("invalid VM name")
        self.args = config["arguments"]
        if not isinstance(self.args, dict) or set(self.args) != PHASES[self.phase]:
            raise ValueError("invalid bootstrap phase arguments")
        self.env_names = config["env_names"]
        if not isinstance(self.env_names, list) or any(
            not isinstance(name, str) or not ENV.fullmatch(name) for name in self.env_names
        ):
            raise ValueError("invalid environment names")
        if not isinstance(config["mounts"], list):
            raise ValueError("invalid workspace configuration")
        self.mounts = []
        targets, tags = set(), set()
        for mount in config["mounts"]:
            if not isinstance(mount, dict) or set(mount) != {"target", "tag", "writable"}:
                raise ValueError("invalid workspace configuration")
            target = path_value(mount["target"])
            tag = mount["tag"]
            if target == Path("/") or under(STAGE, target) or under(target, STAGE):
                raise ValueError("workspace must not cover bootstrap storage")
            if (
                not isinstance(tag, str)
                or not TAG.fullmatch(tag)
                or type(mount["writable"]) is not bool
            ):
                raise ValueError("invalid workspace tag/options")
            if str(target) in targets or tag in tags:
                raise ValueError("duplicate workspace target/tag")
            targets.add(str(target))
            tags.add(tag)
            self.mounts.append(dict(target=target, tag=tag, writable=mount["writable"]))
        backing_paths = [
            root / suffix
            for mount in self.mounts
            if not mount["writable"]
            for root, suffix in (
                (LOWER_ROOT, f".smolvm-ws-{mount['tag']}"),
                (UPPER_ROOT, f".smolvm-ws-{mount['tag']}-upper"),
                (UPPER_ROOT, f".smolvm-ws-{mount['tag']}-work"),
            )
        ]
        if any(
            under(backing, mount["target"]) or under(mount["target"], backing)
            for mount in self.mounts
            for backing in backing_paths
        ):
            raise ValueError("workspace must not cover overlay backing storage")
        if self.phase == "clock":
            if not all(isinstance(self.args[key], str) for key in self.args):
                raise ValueError("invalid clock input")
            zone = self.args["timezone"]
            if zone.startswith("/") or ".." in zone.split("/") or "\x00" in zone:
                raise ValueError("invalid timezone")
        if self.phase == "git" and not isinstance(self.args["text"], str):
            raise ValueError("invalid Git configuration")
        if self.phase == "tools":
            tools, timeout = self.args["tools"], self.args["install_timeout"]
            if not isinstance(tools, list) or any(tool not in ("pi", "specify") for tool in tools):
                raise ValueError("unsupported runtime installer")
            if type(timeout) is not int or timeout <= 0:
                raise ValueError("invalid installer deadline")
        if self.phase == "ssh":
            key = self.args["public_key"]
            if not isinstance(key, str) or len(key.splitlines()) != 1:
                raise ValueError("invalid SSH public key")
            fields = key.split()
            if len(fields) < 2 or fields[0] != "ssh-ed25519":
                raise ValueError("expected the existing Ed25519 public key")
            base64.b64decode(fields[1], validate=True)
        self.lock_fd = None

    def redact(self, text):
        values = {os.environ.get(name) for name in self.env_names}
        for value in sorted(filter(None, values), key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    def run(self, argv, *, timeout=30, user=False, check=True):
        options = {}
        if user:
            account = self.existing_account()
            env = {
                key: os.environ[key]
                for key in ("LANG", "LC_ALL", "TERM", "TZ")
                if key in os.environ
            }
            env.update(
                HOME=account.pw_dir,
                USER=self.user,
                LOGNAME=self.user,
                PATH=f"{account.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            )
            options.update(
                user=account.pw_uid,
                group=account.pw_gid,
                extra_groups=os.getgrouplist(self.user, account.pw_gid),
                cwd=account.pw_dir,
                env=env,
            )
        # Children retain the execution lease if the SSH process disappears.
        descriptors = (self.lock_fd,) if self.lock_fd is not None else ()
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, pass_fds=descriptors, **options
        )
        if check and result.returncode:
            details = self.redact(
                "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            )
            raise ValueError(f"{self.phase} failed: {details or 'guest command failed'}")
        return result

    def existing_account(self):
        try:
            account = pwd.getpwnam(self.user)
        except KeyError as error:
            raise ValueError(
                f"runtime account {self.user!r} is missing; explicit repair required"
            ) from error
        home = path_value(account.pw_dir)
        no_symlinks(home)
        if not home.is_dir():
            raise ValueError(f"home {home} for {self.user!r} is missing; explicit repair required")
        return account

    def account(self):
        if self.user == "root":
            self.existing_account()
            return
        try:
            pwd.getpwnam(self.user)
        except KeyError:
            defaults = self.run(["useradd", "-D"]).stdout.splitlines()
            base = next((line[5:] for line in defaults if line.startswith("HOME=")), "/home")
            home = path_value(base) / self.user
            no_symlinks(home)
            if home.exists():
                raise ValueError(
                    f"home {home} exists without {self.user!r}; explicit repair required"
                ) from None
            self.run(["useradd", "-m", "-U", "-s", "/bin/bash", self.user])
        self.existing_account()
        directory = ETC / "sudoers.d"
        no_symlinks(directory)
        directory.mkdir(mode=0o755, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".sbx-", dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{self.user} ALL=(ALL) NOPASSWD:ALL\n")
            self.run(["visudo", "-cf", str(temporary)])
            temporary.chmod(0o440)
            temporary.replace(directory / self.user)
        finally:
            temporary.unlink(missing_ok=True)

    def current_mounts(self):
        result = self.run(
            ["findmnt", "--json", "--list", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"]
        )
        return {item["target"]: item for item in json.loads(result.stdout)["filesystems"]}

    def covered(self, path):
        no_symlinks(path)
        covered = [mount["target"] for mount in self.mounts]
        covered += [
            Path(item["target"])
            for item in self.current_mounts().values()
            if item["target"] != "/" and item["fstype"] in ("9p", "virtiofs", "overlay")
        ]
        return any(under(path, target) or under(target, path) for target in covered)

    def warn_skip(self, path):
        print(
            f"sbx: warning: skipped initial configuration at {path}: workspace covers destination",
            file=sys.stderr,
        )

    def mount_one(self, source, target, kind, options):
        no_symlinks(target)
        current = self.current_mounts().get(str(target))
        expected = set(options.split(","))
        if current:
            actual = set(current["options"].split(","))
            # Linux omits the default 9p2000.L protocol version from mountinfo.
            if kind == "9p" and not any(option.startswith("version=") for option in actual):
                expected.discard("version=9p2000.L")
            if current["source"] != source or current["fstype"] != kind or not expected <= actual:
                raise ValueError(f"conflicting workspace at {target}; explicit repair required")
            return
        target.mkdir(parents=True, exist_ok=True)
        self.run(["mount", "-t", kind, "-o", options, "--", source, str(target)], timeout=15)

    def mounts_phase(self):
        self.existing_account()
        if not self.mounts:
            return
        needed = ["9p"] + (
            ["overlay"] if any(not mount["writable"] for mount in self.mounts) else []
        )
        supported = FILESYSTEMS.read_text().split()
        for module in ("9p", "9pnet_virtio", "overlay"):
            if module == "overlay" and "overlay" not in needed:
                continue
            if (module == "9pnet_virtio" and "9p" in supported) or module in supported:
                continue
            self.run(["modprobe", module], check=False)
        if any(kind not in FILESYSTEMS.read_text().split() for kind in needed):
            raise ValueError(
                "guest kernel lacks required workspace support; rebuild a supported image"
            )
        for mount in self.mounts:
            target, tag = mount["target"], mount["tag"]
            if mount["writable"]:
                self.mount_one(tag, target, "9p", "trans=virtio,version=9p2000.L,rw")
            else:
                lower = LOWER_ROOT / f".smolvm-ws-{tag}"
                upper = UPPER_ROOT / f".smolvm-ws-{tag}-upper"
                work = UPPER_ROOT / f".smolvm-ws-{tag}-work"
                for directory in (upper, work):
                    if self.covered(directory):
                        raise ValueError("overlay backing storage must not be in a shared folder")
                    no_symlinks(directory)
                    directory.mkdir(mode=0o700, exist_ok=True)
                    info = directory.stat()
                    if info.st_uid != ROOT_UID or info.st_mode & 0o022:
                        raise ValueError(
                            "overlay backing directory needs explicit ownership repair"
                        )
                self.mount_one(tag, lower, "9p", "trans=virtio,version=9p2000.L,ro")
                self.mount_one(
                    "overlay",
                    target,
                    "overlay",
                    f"lowerdir={lower},upperdir={upper},workdir={work}",
                )

    def ssh(self):
        account = self.existing_account()
        home = Path(account.pw_dir)
        directory = home / ".ssh"
        if self.covered(directory) or self.covered(directory / "authorized_keys"):
            raise ValueError(
                "SSH authorization must not be in a shared folder; explicit repair required"
            )
        # Pin directory descriptors: user-owned home entries must not redirect root writes.
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with suppress(FileExistsError):
                os.mkdir(".ssh", mode=0o700, dir_fd=home_fd)
            ssh_fd = os.open(".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        finally:
            os.close(home_fd)
        try:
            os.fchown(ssh_fd, account.pw_uid, account.pw_gid)
            os.fchmod(ssh_fd, 0o700)
            fd = os.open(
                "authorized_keys",
                os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=ssh_fd,
            )
            with os.fdopen(fd, "a+", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("unsafe authorized_keys; explicit repair required")
                os.fchown(handle.fileno(), account.pw_uid, account.pw_gid)
                os.fchmod(handle.fileno(), 0o600)
                handle.seek(0)
                text = handle.read()
                identity = self.args["public_key"].split()[:2]
                for line in text.splitlines():
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    fields = shlex.shlex(line, posix=True)
                    fields.whitespace_split = True
                    fields.commenters = ""
                    try:
                        key_type = next(fields)
                        if key_type != identity[0]:
                            key_type = next(fields)  # Skip the optional key-options field.
                        # Stop at the key blob: the remaining comment is not shell syntax.
                        if key_type == identity[0] and next(fields) == identity[1]:
                            return
                    except (ValueError, StopIteration) as error:
                        raise ValueError(
                            "cannot parse authorized_keys prefix; explicit repair required"
                        ) from error
                # Follow ssh-copy-id: preserve existing bytes and append on a fresh line.
                newline = "\n" if text and not text.endswith("\n") else ""
                handle.write(newline + " ".join(identity) + "\n")
        finally:
            os.close(ssh_fd)

    def credentials(self):
        if self.user == "root":
            return
        account = self.existing_account()
        home = Path(account.pw_dir)
        for incomplete in home.glob(".sbx-copy-*"):
            info = incomplete.lstat()
            marker = incomplete / COPY_MARKER
            if not (
                stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o077
            ):
                continue
            if not marker.is_file() or marker.is_symlink():
                continue
            marker_info = marker.stat()
            if (
                marker_info.st_uid != ROOT_UID
                or marker_info.st_mode & 0o077
                or marker.read_text() != COPY_MAGIC
            ):
                continue
            if self.covered(incomplete) or any(
                under(Path(target), incomplete) for target in self.current_mounts()
            ):
                raise ValueError(
                    "incomplete credential copy overlaps a workspace; explicit repair required"
                )
            shutil.rmtree(incomplete)
        for name in (".ssh", ".pi", ".codex", ".claude", ".claude.json"):
            source, destination = ROOT_HOME / name, home / name
            if destination.exists() or destination.is_symlink() or not source.exists():
                continue
            if self.covered(destination):
                self.warn_skip(destination)
                continue
            if any(under(Path(item["target"]), source) for item in self.current_mounts().values()):
                raise ValueError(
                    "credential source contains a mounted directory; explicit repair required"
                )
            with tempfile.TemporaryDirectory(prefix=".sbx-copy-", dir=home) as temporary:
                marker = Path(temporary) / COPY_MARKER
                with marker.open("x") as handle:
                    handle.write(COPY_MAGIC)
                marker.chmod(0o600)
                copy = Path(temporary) / name
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(source, copy, symlinks=True)
                else:
                    shutil.copy2(source, copy, follow_symlinks=False)
                if not copy.is_symlink():
                    for parent, directories, filenames in os.walk(copy, followlinks=False):
                        for child in directories + filenames:
                            os.lchown(Path(parent) / child, account.pw_uid, account.pw_gid)
                    copy.chmod(stat.S_IMODE(copy.stat().st_mode) & ~0o077)
                os.lchown(copy, account.pw_uid, account.pw_gid)
                publish_noreplace(copy, destination)

    def git(self):
        account = self.existing_account()
        destination = Path(account.pw_dir) / ".gitconfig"
        if destination.exists() or destination.is_symlink() or not self.args["text"]:
            return
        if self.covered(destination):
            self.warn_skip(destination)
            return
        fd, name = tempfile.mkstemp(prefix=".sbx-git-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(self.args["text"])
            os.chown(temporary, account.pw_uid, account.pw_gid)
            with suppress(FileExistsError):
                os.link(temporary, destination, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)

    def tools(self):
        self.existing_account()
        timeout = self.args["install_timeout"]
        for tool in self.args["tools"]:
            found = self.run(
                [
                    "bash",
                    "-lc",
                    'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; '
                    f"command -v {tool}",
                ],
                user=True,
                check=False,
            )
            if found.returncode == 0:
                continue
            if found.stderr.strip():
                raise ValueError(self.redact(found.stderr.strip()))
            if tool == "pi":
                command = (
                    "export NPM_CONFIG_USERCONFIG=/dev/stdin NPM_CONFIG_GLOBALCONFIG=/dev/null\n"
                    "if [ -r /etc/profile.d/smolvm_env.sh ]; then "
                    ". /etc/profile.d/smolvm_env.sh; fi\n"
                    'exec npm install --global --prefix "$HOME/.local" --ignore-scripts '
                    "@earendil-works/pi-coding-agent </dev/null"
                )
            else:
                command = "exec uv tool install specify-cli --from git+https://github.com/github/spec-kit.git"
            self.run(
                [
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=10s",
                    f"{timeout}s",
                    "bash",
                    "-lc",
                    command,
                ],
                user=True,
                timeout=timeout + 15,
            )

    def execute(self):
        if self.phase == "mounts":
            self.mounts_phase()
        elif self.phase in ("account", "credentials", "ssh", "git", "tools"):
            getattr(self, self.phase)()
        elif self.phase == "hostname":
            self.run(["hostname", self.name])
            (ETC / "hostname").write_text(self.name + "\n")
            hosts = ETC / "hosts"
            text = hosts.read_text()
            line = f"127.0.1.1 {self.name}"
            text = (
                re.sub(r"^127\.0\.1\.1[ \t].*$", line, text, flags=re.M)
                if re.search(r"^127\.0\.1\.1[ \t]", text, re.M)
                else text.rstrip() + "\n" + line + "\n"
            )
            hosts.write_text(text)
        elif self.phase == "clock":
            zone = Path("/usr/share/zoneinfo") / self.args["timezone"]
            if zone.is_file():
                (ETC / "localtime").unlink(missing_ok=True)
                (ETC / "localtime").symlink_to(zone)
                (ETC / "timezone").write_text(self.args["timezone"] + "\n")
            self.run(["date", "-u", "-s", self.args["timestamp"]])
        elif self.phase in ("kvm", "docker") and self.user != "root":
            self.existing_account()
            helper = LOCAL_BIN / (
                "sbx-prepare-kvm" if self.phase == "kvm" else "sbx-start-rootless-docker"
            )
            if os.access(helper, os.X_OK):
                argv = (
                    [str(helper), self.user]
                    if self.phase == "kvm"
                    else ["env", f"SBX_DOCKER_USER={self.user}", str(helper)]
                )
                # sudo/start-stop-daemon close the lock FD at the daemon boundary.
                self.run(argv, timeout=60 if self.phase == "docker" else 30)


def main(argv=None):
    try:
        if os.geteuid() != ROOT_UID:
            raise ValueError("bootstrap requires root guest control")
        secure_directory(STAGE)
        arguments = sys.argv[1:] if argv is None else argv
        if len(arguments) != 1:
            raise ValueError("expected one bootstrap configuration file")
        config_path = Path(arguments[0])
        if config_path.parent != STAGE:
            raise ValueError("configuration must be staged outside shared folders")
        guest = Runtime(secure_file(config_path))
        fd = os.open(STAGE / "execution.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != ROOT_UID or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise ValueError("untrusted execution lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(
                    "another provisioning phase is active; retry after it finishes"
                ) from error
            guest.lock_fd = fd
            guest.execute()
        finally:
            os.close(fd)
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        message = guest.redact(str(error)) if "guest" in locals() else str(error)
        print(f"sbx bootstrap: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
