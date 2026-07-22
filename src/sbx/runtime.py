import json
import os
import shlex
import subprocess
import sys
from argparse import Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEBUG = False
SMOLVM_DISABLE_VERSION_CHECK_ENV = "SMOLVM_DISABLE_VERSION_CHECK"
SBX_SMOLVM_VERSION_NOTICES_ENV = "SBX_SMOLVM_VERSION_NOTICES"


class ConfigError(ValueError):
    pass


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
        f"create it with `sbx run {vm_id}` or list VMs with `sbx ls -a`."
    )


def ssh_command(vm_id: str) -> list[str]:
    from smolvm.exceptions import VMNotFoundError
    from smolvm.facade import SmolVM

    try:
        vm = SmolVM.from_id(vm_id)
    except VMNotFoundError as exc:
        raise ConfigError(missing_vm_message(vm_id)) from exc
    try:
        return list(vm._ssh_direct_command())
    finally:
        vm.close()


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
