import fcntl
import json
import os
import pwd
import shlex
import stat
import subprocess
import sys
from argparse import Namespace
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any

import smolvm.facade
from smolvm.exceptions import VMNotFoundError

DEBUG = False
SMOLVM_DISABLE_VERSION_CHECK_ENV = "SMOLVM_DISABLE_VERSION_CHECK"
SBX_SMOLVM_VERSION_NOTICES_ENV = "SBX_SMOLVM_VERSION_NOTICES"
SUDO_ENV_KEYS = ("SUDO_COMMAND", "SUDO_GID", "SUDO_HOME", "SUDO_UID", "SUDO_USER")


@contextmanager
def without_stale_sudo_env() -> Iterator[None]:
    if os.geteuid() == 0:
        yield
        return

    saved = {key: os.environ.pop(key) for key in SUDO_ENV_KEYS if key in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class ConfigError(ValueError):
    pass


@contextmanager
def exclusive_lock(path: Path, *, busy_message: str, blocking: bool = False) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ConfigError(f"cannot open lock file {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ConfigError(f"untrusted lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ConfigError(busy_message) from exc
        yield
    finally:
        os.close(fd)


def vm_name_from_arg_or_config(
    args: Namespace, config: Mapping[str, Any] | None, command: str
) -> str | None:
    name = getattr(args, "name", None)
    sbx = (config or {}).get("sbx", {})
    if name is None and isinstance(sbx, Mapping):
        name = sbx.get("name")
    if not name:
        print(f"sbx: {command} requires a VM name argument or [sbx].name", file=sys.stderr)
        return None
    return str(name)


def debug(message: str) -> None:
    if DEBUG:
        print(f"sbx debug: {message}", file=sys.stderr)


def debug_command(argv: Sequence[str], env: Mapping[str, str] | None) -> None:
    debug(f"run: {shlex.join(list(argv))}")
    active_env = env if env is not None else os.environ
    env_source = "custom" if env is not None else "current"
    interesting = {
        key: active_env.get(key)
        for key in ("HOME", "SMOLVM_DATA_DIR", "XDG_STATE_HOME")
        if active_env.get(key) is not None
    }
    debug(f"env source: {env_source}; {interesting}")


def run(argv: Sequence[str], *, check: bool = False, env: Mapping[str, str] | None = None) -> int:
    debug_command(argv, env)
    try:
        proc = subprocess.run(list(argv), check=check, env=dict(env) if env is not None else None)
    except FileNotFoundError:
        print(f"sbx: command not found on PATH: {argv[0]}", file=sys.stderr)
        return 127
    except subprocess.CalledProcessError as exc:
        debug(f"return code: {exc.returncode}")
        return exc.returncode
    debug(f"return code: {proc.returncode}")
    return proc.returncode


def run_capture(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    debug_command(argv, env)
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            text=True,
            capture_output=True,
            env=dict(env) if env is not None else None,
        )
        debug(f"return code: {result.returncode}")
        if result.stdout:
            debug(f"stdout: {result.stdout.strip()[:2000]}")
        if result.stderr:
            debug(f"stderr: {result.stderr.strip()[:2000]}")
        return result
    except FileNotFoundError:
        print(f"sbx: command not found on PATH: {argv[0]}", file=sys.stderr)
        return None


def smolvm_argv(args: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from smolvm.cli.main import main; raise SystemExit(main())",
        *args,
    ]


def env_boolean(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def smolvm_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    if not env_boolean(result.get(SBX_SMOLVM_VERSION_NOTICES_ENV)):
        result[SMOLVM_DISABLE_VERSION_CHECK_ENV] = "1"
    return result


def run_smolvm(args: Sequence[str], **kwargs: Any) -> int:
    kwargs["env"] = smolvm_env(kwargs.get("env"))
    return run(smolvm_argv(args), **kwargs)


def run_smolvm_capture(
    args: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str] | None:
    kwargs["env"] = smolvm_env(kwargs.get("env"))
    return run_capture(smolvm_argv(args), **kwargs)


def missing_vm_message(vm_id: str) -> str:
    return (
        f"VM {vm_id!r} not found. `sbx shell` attaches to an existing sandbox; "
        f"create it with `sbx run {vm_id}` or list VMs with `sbx ls`."
    )


def guest_ssh_key_path(vm_id: str, config: Any) -> Path:
    if config.comm_channel not in {"ssh", "vsock"} or not getattr(config, "ssh_capable", False):
        raise ConfigError(
            f"VM {vm_id!r} requires explicit SSH metadata; recreate it with a supported image"
        )
    injected_key = getattr(config, "ssh_public_key", None)
    if not isinstance(injected_key, str) or len(injected_key.split()) < 2:
        raise ConfigError(f"VM {vm_id!r} has no trusted SSH identity; recreate it")
    key_home = Path.home()
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        with suppress(KeyError):
            key_home = Path(pwd.getpwnam(sudo_user).pw_dir)
    # shortcut: pinned key layout; use upstream read-only resolution when available.
    key_dir = key_home / ".smolvm" / "keys"
    public_key, private_key = key_dir / "id_ed25519.pub", key_dir / "id_ed25519"
    try:
        if not public_key.is_file() or not private_key.is_file():
            raise ConfigError("missing smolvm SSH key; restore the existing key pair")
        authorized = public_key.read_text(encoding="utf-8").split()[:2]
    except (OSError, UnicodeError) as exc:
        raise ConfigError("cannot read smolvm SSH key; restore the existing key pair") from exc
    if authorized != injected_key.split()[:2]:
        raise ConfigError(
            f"VM {vm_id!r} SSH identity does not match; restore its key pair or recreate it"
        )
    return private_key


def ssh_command(vm_id: str) -> list[str]:
    with ExitStack() as cleanup:
        try:
            vm = smolvm.facade.SmolVM.from_id(vm_id)
        except VMNotFoundError as exc:
            raise ConfigError(missing_vm_message(vm_id)) from exc
        cleanup.callback(vm.close)
        key = guest_ssh_key_path(vm_id, vm.info.config)
        vm = smolvm.facade.SmolVM.from_id(vm_id, ssh_key_path=str(key))
        cleanup.callback(vm.close)
        return list(vm._ssh_direct_command())


def read_json_object(path: Path, *, error: str | None = None) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if error is not None:
            raise ConfigError(f"{error}: {path}: {exc}") from exc
        return {}
    return data if isinstance(data, dict) else {}


def write_json_object(path: Path, data: Mapping[str, Any], *, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
