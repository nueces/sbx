import atexit
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
from smolvm.vm import SmolVMManager

from sbx import bootstrap, runtime, session_state, vm_metadata
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


@contextmanager
def open_guest(vm_id: str):
    with ExitStack() as cleanup:
        vm = smolvm.facade.SmolVM.from_id(vm_id)
        cleanup.callback(vm.close)
        config = vm.info.config
        if config.comm_channel == "vsock":
            yield vm
            return
        private_key = runtime.guest_ssh_key_path(vm_id, config)
        vm = smolvm.facade.SmolVM.from_id(vm_id, ssh_key_path=str(private_key))
        cleanup.callback(vm.close)
        yield vm


def sync_forwarded_env(vm_id: str, names: list[str], *, guest=None) -> None:
    if not names:
        return
    validate_env_names(names)
    values = {name: os.environ[name] for name in names if name in os.environ}
    missing = [name for name in names if name not in os.environ]
    context = nullcontext(guest.vm) if guest is not None else open_guest(vm_id)
    with context as vm:
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
    with open_guest(vm_id) as vm:
        vm.wait_for_ready(timeout=timeout)
        guest = bootstrap.GuestBootstrap(vm, user, vm_id, env_names)
        guest.phase("check")
        yield guest


def wait_for_control(vm_id: str, *, timeout: float) -> None:
    with open_guest(vm_id) as vm:
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


def stop_vm(vm_id: str, *, only_if_idle: bool = False) -> int:
    with vm_metadata.Lifecycle() as operation:
        operation.lock(vm_id)
        if only_if_idle:
            with session_state.session_lock():
                if session_state.active_sessions(vm_id):
                    return 0
        try:
            with open_guest(vm_id) as vm:
                vm.wait_for_ready(timeout=10)
                result = vm.run("sync", timeout=10)
                if result.exit_code:
                    raise ConfigError("guest filesystem flush failed")
        except Exception:  # noqa: BLE001 - flushing must not prevent an explicit stop.
            print(
                f"sbx: warning: could not flush guest writes for {vm_id!r}; stopping anyway",
                file=sys.stderr,
            )
        with session_state.session_lock() if only_if_idle else nullcontext():
            if only_if_idle and session_state.active_sessions(vm_id):
                runtime.debug(f"not stopping {vm_id}: a session joined during flushing")
                return 0
            manager, key = _manager_for(vm_id)
            return _stop_manager(vm_id, manager, key)


def _phase(vm_id, name, arguments=None, *, user="root", guest=None, timeout=30):
    if guest is not None:
        return guest.phase(name, arguments, timeout=timeout)
    with bootstrap_session(vm_id, user) as client:
        return client.phase(name, arguments, timeout=timeout)


def mount_workspaces(vm_id: str, user: str, *, guest=None) -> None:
    count = len(guest.mounts) if guest is not None else 0
    _phase(vm_id, "mounts", user=user, guest=guest, timeout=30 + 30 * count)


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
    cwd: str | None = None,
    run_capture: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[..., list[str]] | None = None,
) -> str | None:
    run_capture = run_capture or runtime.run_capture
    ssh = ssh or ssh_command
    validate_run_user(user)
    cd_prefix = f"cd -- {shlex.quote(cwd)} && " if cwd is not None else ""
    completed = run_capture(
        [*ssh(vm_id, user=user), f"{cd_prefix}command -v {shlex.quote(command)}"]
    )
    if completed is None:
        raise ConfigError(f"failed to resolve {command!r}: ssh command not found")
    if completed.returncode == 255:
        raise ConfigError(
            f"user SSH failed for {vm_id!r}; check guest SSH policy or run "
            f"`sbx run {vm_id} --reprovision --no-attach` to authorize its existing key"
        )
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
    launch_command: str | None,
    user: str | None = None,
    cwd: str | None = None,
    *,
    run: Callable[[list[str]], int] | None = None,
    ssh: Callable[..., list[str]] | None = None,
) -> int:
    run = run or runtime.run
    ssh = ssh or ssh_command
    cmd = ssh(vm_id, user=validate_run_user(user or "root"))
    cd_prefix = f"cd -- {shlex.quote(cwd)} && " if cwd is not None else ""
    # sshd supplies SHELL from the guest account; login startup stays the shell's job.
    command = shlex.quote(launch_command) if launch_command is not None else '"$SHELL" -il'
    cmd.insert(-1, "-t")
    cmd.append(f"{cd_prefix}exec {command}")
    rc = run(cmd)
    if rc == 255:
        print(
            f"sbx: user SSH failed for {vm_id!r}; check the guest SSH policy and "
            f"authorize its existing key with `sbx run {vm_id} --reprovision --no-attach`",
            file=sys.stderr,
        )
    return rc
