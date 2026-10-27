import atexit
import base64
import os
import pwd
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from datetime import UTC, datetime
from pathlib import Path

import smolvm.facade
import smolvm.utils
from smolvm.env import ENV_FILE, build_env_script
from smolvm.vm import SmolVMManager

from sbx import bootstrap, runtime, vm_metadata
from sbx.runtime import ConfigError, ssh_command

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
VM_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
FORWARDABLE_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
# Retain public managers so same-process stops can reap the QEMU children they launched.
_STARTED: dict[tuple[Path, str], SmolVMManager] = {}


def _close_started_managers() -> None:
    for manager in _STARTED.values():
        with suppress(Exception):
            manager.close()
    _STARTED.clear()


atexit.register(_close_started_managers)

SAFE_GIT_CONFIG_KEYS = (
    "user.name",
    "user.email",
    "init.defaultBranch",
    "pull.rebase",
    "push.default",
    "core.autocrlf",
    "core.eol",
)


def validate_vm_name(name: str) -> str:
    if not VM_NAME_RE.fullmatch(name):
        raise ConfigError(
            "[sbx].name must be a valid hostname: lowercase letters, digits, hyphens, "
            "1-63 chars, no leading/trailing hyphen"
        )
    return name


def validate_run_user(user: str) -> str:
    if not USERNAME_RE.fullmatch(user):
        raise ConfigError("[sbx].run_user must be a valid Linux user name")
    return user


def host_username() -> str:
    sudo_user = os.environ.get("SUDO_USER") if os.geteuid() == 0 else None
    if sudo_user and sudo_user != "root":
        with suppress(KeyError):
            return validate_run_user(pwd.getpwnam(sudo_user).pw_name)
    try:
        return validate_run_user(pwd.getpwuid(os.getuid()).pw_name)
    except KeyError as exc:
        raise ConfigError("could not resolve the current host user; set [sbx].run_user") from exc


def validate_env_names(names: list[str]) -> list[str]:
    invalid = [name for name in names if not ENV_NAME_RE.fullmatch(name)]
    if invalid:
        raise ConfigError(f"invalid env var name(s): {', '.join(invalid)}")
    return names


def sanitize_forwarded_env(env: dict[str, str], allowed: list[str]) -> dict[str, str]:
    allowed_set = set(allowed)
    for key in FORWARDABLE_ENV_VARS:
        if key not in allowed_set:
            env.pop(key, None)
    return env


def credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
    env = sanitize_forwarded_env(dict(os.environ), forward_env)
    real_home = Path.home()
    real_smolvm_cache = real_home / ".smolvm"
    real_smolvm_data = Path(env.get("SMOLVM_DATA_DIR", real_home / ".local" / "state" / "smolvm"))
    temp_home.mkdir(parents=True, exist_ok=True)
    real_smolvm_cache.mkdir(parents=True, exist_ok=True)
    real_smolvm_data.mkdir(parents=True, exist_ok=True)
    (temp_home / ".smolvm").symlink_to(real_smolvm_cache, target_is_directory=True)
    fake_bin = temp_home / ".sbx-bin"
    fake_bin.mkdir()
    security = fake_bin / "security"
    security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    security.chmod(0o755)
    env["HOME"] = str(temp_home)
    env["SMOLVM_DATA_DIR"] = str(real_smolvm_data)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def host_git_config(project_root: Path | None = None) -> str | None:
    values: dict[str, str] = {}
    for key in SAFE_GIT_CONFIG_KEYS:
        cmd = ["git", "config", "--global", "--get", key]
        if project_root is not None:
            cmd = ["git", "-C", str(project_root), "config", "--get", key]
        try:
            completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
        except FileNotFoundError:
            return None
        if completed.returncode != 0 and project_root is not None:
            completed = subprocess.run(
                ["git", "config", "--global", "--get", key],
                check=False,
                text=True,
                capture_output=True,
            )
        if completed.returncode == 0:
            value = completed.stdout.strip()
            if value and "\n" not in value:
                values[key] = value
    if not values:
        return None
    sections: dict[str, list[tuple[str, str]]] = {}
    for key, value in values.items():
        section, option = key.split(".", 1)
        sections.setdefault(section, []).append((option, value))
    lines: list[str] = []
    for section, entries in sections.items():
        lines.append(f"[{section}]")
        for option, value in entries:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'\t{option} = "{escaped}"')
        lines.append("")
    return "\n".join(lines)


def parse_managed_env_script(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export ") and "=" in line:
            key, raw_value = line[len("export ") :].split("=", 1)
            with suppress(ValueError):
                env[key] = (shlex.split(raw_value) or [""])[0]
    return env


def sync_forwarded_env_direct_ssh(
    vm_id: str,
    values: dict[str, str],
    missing: list[str],
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    ssh_cmd = ssh(vm_id)
    read_command = (
        f"{bootstrap.LEASE_CHECK} && {{ cat {shlex.quote(ENV_FILE)} 2>/dev/null || true; }}"
    )
    completed = run_capture([*ssh_cmd, read_command])
    if completed is None:
        raise ConfigError("failed to sync environment: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to read environment: {stderr}")
    env = parse_managed_env_script(completed.stdout)
    env.update(values)
    for name in missing:
        env.pop(name, None)
    encoded = base64.b64encode(build_env_script(env).encode("utf-8")).decode("ascii")
    write_script = f"""
set -eu
_t=$(mktemp /tmp/.smolvm_env.XXXXXXXXXX)
trap 'rm -f "$_t"' EXIT
printf %s {shlex.quote(encoded)} | base64 -d > "$_t"
chmod 0644 "$_t"
mv "$_t" {shlex.quote(ENV_FILE)}
"""
    completed = run_capture([*ssh_cmd, "bash -lc " + shlex.quote(write_script)])
    if completed is None:
        raise ConfigError("failed to sync environment: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to write environment: {stderr}")


@contextmanager
def open_guest(vm_id: str, *, legacy_ssh: bool = False):
    with ExitStack() as cleanup:
        vm = smolvm.facade.SmolVM.from_id(vm_id)
        cleanup.callback(vm.close)
        config = vm.info.config
        channel = config.comm_channel
        options = {"comm_channel": "ssh"} if legacy_ssh and channel is None else {}
        injected_key = getattr(config, "ssh_public_key", None)
        if (channel == "ssh" or options) and injected_key:
            key_home = Path.home()
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user:
                with suppress(KeyError):
                    key_home = Path(pwd.getpwnam(sudo_user).pw_dir)
            # shortcut: pinned key layout; use upstream read-only resolution when available.
            key_dir = key_home / ".smolvm" / "keys"
            public_key = key_dir / "id_ed25519.pub"
            if (
                public_key.is_file()
                and public_key.read_text(encoding="utf-8").split()[:2] == injected_key.split()[:2]
            ):
                if not (key_dir / "id_ed25519").is_file():
                    raise ConfigError("missing smolvm SSH key; restore the existing key pair")
                private_key, _ = smolvm.utils.ensure_ssh_key()
                options["ssh_key_path"] = str(private_key)
        if options:
            vm = smolvm.facade.SmolVM.from_id(vm_id, **options)
            cleanup.callback(vm.close)
        yield vm


def sync_forwarded_env(
    vm_id: str, names: list[str], *, run_capture=None, ssh=None, guest=None
) -> None:
    if not names:
        return
    validate_env_names(names)
    values = {name: os.environ[name] for name in names if name in os.environ}
    missing = [name for name in names if name not in os.environ]
    context = nullcontext(guest.vm) if guest is not None else open_guest(vm_id)
    with context as vm:
        if vm.info.config.comm_channel is None:
            sync_forwarded_env_direct_ssh(vm_id, values, missing, run_capture=run_capture, ssh=ssh)
            return
        if guest is None:
            result = vm.run(bootstrap.LEASE_CHECK, timeout=10)
            if result.exit_code:
                raise ConfigError(
                    "guest provisioning is active or unavailable; retry after it finishes"
                )
        if values:
            vm.set_env_vars(values)
        if missing:
            vm.unset_env_vars(missing)


@contextmanager
def bootstrap_session(vm_id: str, user: str, *, env_names=(), timeout: float = 30):
    validate_run_user(user)
    validate_vm_name(vm_id)
    with open_guest(vm_id, legacy_ssh=True) as vm:
        vm.wait_for_ready(timeout=timeout)
        guest = bootstrap.GuestBootstrap(vm, user, vm_id, env_names)
        guest.phase("check")
        yield guest


def wait_for_control(vm_id: str, *, timeout: float) -> None:
    with open_guest(vm_id, legacy_ssh=True) as vm:
        vm.wait_for_ready(timeout=timeout)


def prepare_boot(vm_id: str, user: str, names: list[str], *, guest, mounts: bool = True) -> None:
    prepare_run_user(vm_id, user, guest=guest)
    set_hostname(vm_id, guest=guest)
    sync_guest_clock(vm_id, guest=guest)
    configured = getattr(guest.vm.info.config, "env_vars", {})
    if configured:
        guest.vm.set_env_vars(configured)
    sync_forwarded_env(vm_id, names, guest=guest)
    if mounts:
        mount_workspaces(vm_id, user, guest=guest)


def _manager_for(vm_id: str):
    manager = SmolVMManager()
    key = (manager.data_dir, vm_id)
    owned = _STARTED.get(key)
    if owned is not None:
        manager.close()
        manager = owned
    return manager, key


def start_vm(vm_id: str, *, boot_timeout: float, status: str = "stopped") -> None:
    manager, key = _manager_for(vm_id)
    try:
        if status == "paused":
            manager.resume(vm_id)
        else:
            manager.start(vm_id, boot_timeout=boot_timeout)
    except BaseException:
        if key not in _STARTED:
            manager.close()
        raise
    _STARTED[key] = manager


def _stop_manager(vm_id, manager, key) -> int:
    try:
        manager.stop(vm_id)
    except Exception as exc:  # noqa: BLE001 - preserve stop failure independently of flush failure.
        if key not in _STARTED:
            manager.close()
        print(f"sbx: failed to stop VM {vm_id!r}: {exc}", file=sys.stderr)
        return 1
    _STARTED.pop(key, None)
    manager.close()
    return 0


def stop_if_owned(vm_id: str) -> int:
    # Destructive removal may run in the same process as creation; reap our own child first.
    if not _STARTED:
        return 0
    manager, key = _manager_for(vm_id)
    if key not in _STARTED:
        manager.close()
        return 0
    return _stop_manager(vm_id, manager, key)


def stop_vm(vm_id: str) -> int:
    with vm_metadata.Lifecycle() as operation:
        operation.lock(vm_id)
        try:
            with open_guest(vm_id, legacy_ssh=True) as vm:
                vm.wait_for_ready(timeout=10)
                result = vm.run("sync", timeout=10)
                if result.exit_code:
                    raise ConfigError("guest filesystem flush failed")
        except Exception:  # noqa: BLE001 - flushing must not prevent an explicit stop.
            print(
                f"sbx: warning: could not flush guest writes for {vm_id!r}; stopping anyway",
                file=sys.stderr,
            )
        manager, key = _manager_for(vm_id)
        return _stop_manager(vm_id, manager, key)


def _phase(vm_id, name, arguments=None, *, user="root", guest=None, timeout=30):
    if guest is not None:
        return guest.phase(name, arguments, timeout=timeout)
    with bootstrap_session(vm_id, user) as client:
        return client.phase(name, arguments, timeout=timeout)


def mount_workspaces(vm_id: str, user: str, *, guest=None) -> None:
    count = len(guest.mounts) if guest is not None else 0
    _phase(vm_id, "mounts", user=user, guest=guest, timeout=270 + 15 * count)


def host_timezone() -> str:
    zoneinfo = Path("/usr/share/zoneinfo")
    try:
        target = Path("/etc/localtime").resolve()
        return target.relative_to(zoneinfo).as_posix()
    except (OSError, ValueError):
        pass
    try:
        timezone = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        return "UTC"
    return timezone or "UTC"


def sync_guest_clock(vm_id: str, *, guest=None) -> None:
    _phase(
        vm_id,
        "clock",
        {"timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"), "timezone": host_timezone()},
        guest=guest,
    )


def set_hostname(vm_id: str, *, guest=None) -> None:
    validate_vm_name(vm_id)
    _phase(vm_id, "hostname", guest=guest)


def install_git_config(
    vm_id: str, user: str | None, git_config_text: str | None, *, guest=None
) -> None:
    if git_config_text:
        _phase(vm_id, "git", {"text": git_config_text}, user=user or "root", guest=guest)


def prepare_run_user(
    vm_id: str, user: str, *, copy_initial_state: bool = False, guest=None
) -> None:
    validate_run_user(user)
    _phase(vm_id, "account", user=user, guest=guest)
    if copy_initial_state:
        _phase(vm_id, "credentials", user=user, guest=guest)


def ensure_kvm_access(vm_id: str, user: str, *, guest=None) -> None:
    if user != "root":
        _phase(vm_id, "kvm", user=user, guest=guest)


def ensure_rootless_docker(vm_id: str, user: str, *, guest=None) -> None:
    if user != "root":
        _phase(vm_id, "docker", user=user, guest=guest, timeout=75)


def command_path(
    vm_id: str,
    user: str,
    command: str,
    *,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> str | None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    quoted_user = shlex.quote(user)
    check = (
        'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; '
        + f"command -v {shlex.quote(command)}"
    )
    script = f"""
set -eu
home="$(getent passwd {quoted_user} | cut -d: -f6)"
[ -n "$home" ] || {{ echo "user {quoted_user} has no home" >&2; exit 1; }}
sudo -iu {quoted_user} env HOME="$home" sh -lc {shlex.quote(check)}
"""
    completed = run_capture([*ssh(vm_id), "bash -lc " + shlex.quote(script)])
    if completed is None:
        raise ConfigError(f"failed to resolve {command!r}: ssh command not found")
    if completed.returncode == 0:
        lines = completed.stdout.strip().splitlines()
        return lines[-1] if lines else None
    details = completed.stderr.strip() or completed.stdout.strip()
    if not details:
        return None
    raise ConfigError(f"failed to resolve {command!r} for {user!r}: {details}")


def install_runtime_tools(
    vm_id: str, user: str, tools: list[str], install_timeout: int, *, guest=None
) -> None:
    if tools:
        _phase(
            vm_id,
            "tools",
            {"tools": tools, "install_timeout": install_timeout},
            user=user,
            guest=guest,
            timeout=len(tools) * (install_timeout + 30) + 15,
        )


def attach(
    vm_id: str,
    launch_command: str,
    user: str | None = None,
    cwd: str | None = None,
    *,
    run: Callable[[list[str]], int] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
) -> int:
    run = run or runtime.run
    ssh = ssh or ssh_command
    cmd = ssh(vm_id)
    cd_prefix = f"cd {shlex.quote(cwd)} || exit; " if cwd is not None else ""
    remote = (
        f"{cd_prefix}[ -r {ENV_FILE} ] && . {ENV_FILE}; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"exec {shlex.quote(launch_command)}"
    )
    if user is not None:
        remote = f"sudo -iu {shlex.quote(user)} bash -lc " + shlex.quote(remote)
    cmd.insert(-1, "-t")
    cmd.append(remote)
    return run(cmd)
