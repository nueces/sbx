import argparse
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sbx.image.ls
import sbx.network as network
from sbx import (
    __version__,
    doctor,
    guest_customization,
    lifecycle_warnings,
    session_state,
    vm_metadata,
    vm_state,
)
from sbx.completion import SUPPORTED_SHELLS, completion_script
from sbx.constants import (
    DEFAULT_BACKEND,
    DEFAULT_BOOT_TIMEOUT,
    SMOLVM_DB_PATH,
)
from sbx.image import build_debian
from sbx.runtime import ConfigError as RuntimeConfigError
from sbx.runtime import smolvm_env

AGENTS = ("pi", "claude", "codex")
MIB = 1024 * 1024
LAUNCH_COMMANDS = {"pi": "pi", "claude": "claude", "codex": "codex"}
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
VM_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORWARDABLE_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
SAFE_GIT_CONFIG_KEYS = (
    "user.name",
    "user.email",
    "init.defaultBranch",
    "pull.rebase",
    "push.default",
    "core.autocrlf",
    "core.eol",
)
DEFAULT_CONFIG_PATHS = (Path.home() / ".config" / "sbx" / "config.toml",)
LOCAL_CONFIG_PATHS = (Path.cwd() / ".sbx.toml",)
DEBUG = False


ConfigError = RuntimeConfigError


def _debug(message: str) -> None:
    if DEBUG:
        print(f"sbx debug: {message}", file=sys.stderr)


def _debug_command(argv: Sequence[str], env: Mapping[str, str] | None) -> None:
    _debug(f"run: {shlex.join(list(argv))}")
    active_env = env if env is not None else os.environ
    env_source = "custom" if env is not None else "current"
    interesting = {
        key: active_env.get(key)
        for key in ("HOME", "SMOLVM_DATA_DIR", "XDG_STATE_HOME")
        if active_env.get(key) is not None
    }
    _debug(f"env source: {env_source}; {interesting}")


def _run(argv: Sequence[str], *, check: bool = False, env: Mapping[str, str] | None = None) -> int:
    _debug_command(argv, env)
    try:
        proc = subprocess.run(list(argv), check=check, env=dict(env) if env is not None else None)
    except FileNotFoundError:
        print(f"sbx: command not found on PATH: {argv[0]}", file=sys.stderr)
        return 127
    except subprocess.CalledProcessError as exc:
        _debug(f"return code: {exc.returncode}")
        return exc.returncode
    _debug(f"return code: {proc.returncode}")
    return proc.returncode


def _run_capture(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    _debug_command(argv, env)
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            text=True,
            capture_output=True,
            env=dict(env) if env is not None else None,
        )
        _debug(f"return code: {result.returncode}")
        if result.stdout:
            _debug(f"stdout: {result.stdout.strip()[:2000]}")
        if result.stderr:
            _debug(f"stderr: {result.stderr.strip()[:2000]}")
        return result
    except FileNotFoundError:
        print(f"sbx: command not found on PATH: {argv[0]}", file=sys.stderr)
        return None


def _smolvm_argv(args: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from smolvm.cli.main import main; raise SystemExit(main())",
        *args,
    ]


def _run_smolvm(args: Sequence[str], **kwargs: Any) -> int:
    kwargs["env"] = smolvm_env(kwargs.get("env"))
    return _run(_smolvm_argv(args), **kwargs)


def _run_smolvm_capture(
    args: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str] | None:
    kwargs["env"] = smolvm_env(kwargs.get("env"))
    return _run_capture(_smolvm_argv(args), **kwargs)


def _require(command: str, install_hint: str | None = None) -> bool:
    if shutil.which(command):
        return True
    print(f"sbx: required command not found: {command}", file=sys.stderr)
    if install_hint:
        print(install_hint, file=sys.stderr)
    return False


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a TOML table")
    return data


def load_config(explicit_path: str | None = None) -> dict[str, Any]:
    """Load sbx config.

    Precedence, lowest to highest:
      1. default config path: ~/.config/sbx/config.toml
      2. current directory config: ./.sbx.toml
      3. --config PATH, when provided
      4. CLI flags, applied separately by each command
    """
    paths = [*DEFAULT_CONFIG_PATHS, *LOCAL_CONFIG_PATHS]
    if explicit_path:
        explicit = Path(explicit_path).expanduser()
        if not explicit.exists():
            raise ConfigError(f"{explicit}: config file does not exist")
        paths.append(explicit)

    config: dict[str, Any] = {}
    for path in paths:
        if path.exists():
            _debug(f"loading config: {path}")
            config = _deep_merge(config, _read_toml(path))
    _debug(f"merged config: {config}")
    return config


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _cfg(config: Mapping[str, Any], section: str, key: str, default: Any = None) -> Any:
    return _section(config, section).get(key, default)


def _sbx_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return _section(config, "sbx")


def _cfg_agent(config: Mapping[str, Any]) -> str:
    agent = _sbx_config(config).get("agent", "pi")
    if agent not in AGENTS:
        raise ConfigError(f"[sbx].agent must be one of: {', '.join(AGENTS)}")
    return str(agent)


def _list_value(value: Any, *, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ConfigError(f"{key} must be a string or an array of strings")


def _resolve_project_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _project_identity(args: argparse.Namespace, config: Mapping[str, Any]) -> dict[str, str]:
    explicit = getattr(args, "config", None)
    if explicit:
        config_path = Path(str(explicit)).expanduser().resolve(strict=False)
        root = config_path.parent
    elif LOCAL_CONFIG_PATHS[0].exists():
        config_path = LOCAL_CONFIG_PATHS[0].resolve(strict=False)
        root = config_path.parent
    else:
        project_path = _cfg(config, "sbx", "project_path")
        if project_path is not None:
            root = _resolve_project_path(str(project_path))
            config_path = root / ".sbx.toml"
        else:
            root = Path.cwd().resolve(strict=False)
            config_path = root / ".sbx.toml"
    return {
        "project_root": str(root.expanduser().resolve(strict=False)),
        "config_path": str(config_path.expanduser().resolve(strict=False)),
    }


def _same_path_mount(path_value: str) -> str:
    resolved = _resolve_project_path(path_value)
    return f"{resolved}:{resolved}"


def _workspace_mounts_from_specs(mounts: Sequence[str], *, writable: bool) -> list[dict[str, Any]]:
    workspace_mounts: list[dict[str, Any]] = []
    guest_paths: set[str] = set()
    for spec in mounts:
        host_str, guest_path = spec.rsplit(":", 1) if ":" in spec else (spec, spec)
        host_path = _resolve_project_path(host_str)
        if not host_path.exists() or not host_path.is_dir():
            raise ConfigError(f"mount host path must be an existing directory: {host_path}")
        if not guest_path.startswith("/"):
            raise ConfigError(f"mount guest path must be absolute: {guest_path}")
        if guest_path in guest_paths:
            raise ConfigError(f"duplicate mount guest path: {guest_path}")
        guest_paths.add(guest_path)
        workspace_mounts.append(
            {
                "host_path": str(host_path),
                "guest_path": guest_path,
                "mount_tag": None,
                "writable": writable,
            }
        )
    return workspace_mounts


def _warn_running_mount_drift(
    vm_name: str, mounts: Sequence[str] | None, *, writable_mounts: bool
) -> None:
    if mounts is None:
        return
    current = vm_state.existing_vm_start_config(vm_name)
    if current is None or current[0] != "running":
        return
    desired_mounts = _workspace_mounts_from_specs(mounts, writable=writable_mounts)
    if current[1].get("workspace_mounts") != desired_mounts:
        print(
            f"sbx: VM '{vm_name}' is running with mounts that differ from current config.",
            file=sys.stderr,
        )
        print("sbx: Current session will use the VM's existing mounts.", file=sys.stderr)
        print(
            f"sbx: Run `sbx stop {vm_name}` then `sbx run {vm_name}` to apply config mounts.",
            file=sys.stderr,
        )


def _sync_existing_vm_start_config(
    vm_name: str,
    mounts: Sequence[str] | None,
    *,
    writable_mounts: bool,
    port_forwards: Sequence[str],
    project: Mapping[str, str] | None = None,
) -> None:
    if project is not None:
        vm_metadata.validate_vm_project(vm_name, project)
    current = vm_state.existing_vm_start_config(vm_name)
    if current is None or current[0] == "running":
        return

    desired_mounts = (
        _workspace_mounts_from_specs(mounts, writable=writable_mounts)
        if mounts is not None
        else None
    )
    desired_forwards = network.port_forwards_from_specs(port_forwards)
    updated: list[str] = []
    with sqlite3.connect(SMOLVM_DB_PATH.expanduser()) as conn:
        config = current[1]
        if desired_mounts is not None and config.get("workspace_mounts") != desired_mounts:
            config["workspace_mounts"] = desired_mounts
            updated.append("mounts")
        if config.get("port_forwards", []) != desired_forwards:
            config["port_forwards"] = desired_forwards
            updated.append("port forwards")
        if not updated:
            return
        conn.execute(
            "UPDATE vms SET config = ? WHERE id = ?",
            (json.dumps(config, separators=(",", ":")), vm_name),
        )
    print(f"sbx: updated {', '.join(updated)} for existing VM '{vm_name}'")


def _sync_existing_vm_mounts_from_config(
    vm_name: str, mounts: Sequence[str], *, writable_mounts: bool
) -> None:
    _sync_existing_vm_start_config(
        vm_name, mounts, writable_mounts=writable_mounts, port_forwards=[]
    )


def _project_guest_cwd(path_value: Any) -> str | None:
    if path_value is None:
        return None
    return str(_resolve_project_path(str(path_value)))


def _validate_run_user(user: str) -> str:
    if not USERNAME_RE.match(user):
        raise ConfigError("[sbx].run_user must be a valid Linux user name")
    return user


def _validate_vm_name(name: str) -> str:
    if not VM_NAME_RE.match(name):
        raise ConfigError(
            "[sbx].name must be a valid hostname: lowercase letters, digits, hyphens, "
            "1-63 chars, no leading/trailing hyphen"
        )
    return name


def _sync_forwarded_env_or_error(vm_id: str, names: list[str]) -> bool:
    try:
        guest_customization.sync_forwarded_env(vm_id, names, run_capture=_run_capture)
    except Exception as exc:  # noqa: BLE001 - keep CLI errors user-friendly.
        print(f"sbx: failed to sync environment for VM {vm_id!r}: {exc}", file=sys.stderr)
        return False
    return True


@contextmanager
def _patched_environ(env: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _validate_cpus(value: Any) -> int:
    cpus = int(value)
    if not 1 <= cpus <= 32:
        raise ConfigError("[sbx].cpus must be between 1 and 32")
    return cpus


def _validate_boot_timeout(value: Any) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise ConfigError("[sbx].boot_timeout must be greater than 0")
    return timeout


def _smolvm_info_vm(vm_id: str) -> Mapping[str, Any] | None:
    completed = _run_smolvm_capture(["sandbox", "info", vm_id, "--json"])
    if completed is None or completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
        vm = payload["data"]["vm"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return vm if isinstance(vm, Mapping) else None


def _get_existing_vm_status(vm_id: str) -> str | None:
    vm = _smolvm_info_vm(vm_id)
    if vm is None:
        return None
    status = vm.get("status")
    return status if isinstance(status, str) else None


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]
    header_line = "  ".join(
        header.ljust(width) for header, width in zip(headers, widths, strict=True)
    )
    print(header_line.rstrip())
    for row in rows:
        line = "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True))
        print(line.rstrip())


def cmd_list(args: argparse.Namespace) -> int:
    metadata = vm_metadata.load_vm_metadata()
    rows = []
    for vm in vm_state.smolvm_vms(all_vms=bool(getattr(args, "all", False))):
        name = str(getattr(vm, "vm_id", "-"))
        status = getattr(getattr(vm, "status", "-"), "value", getattr(vm, "status", "-"))
        rootfs = getattr(getattr(vm, "config", None), "rootfs_path", None)
        image_path = Path(rootfs) if rootfs is not None else None
        image = "-" if image_path is None else image_path.parent.name or image_path.name or "-"
        ssh_port = getattr(getattr(vm, "network", None), "ssh_host_port", None)
        rows.append(
            [
                name,
                str(status),
                metadata.get(name, {}).get("project_root", "-"),
                image,
                str(ssh_port) if ssh_port is not None else "-",
            ]
        )
    _print_table(("NAME", "STATUS", "PROJECT", "IMAGE", "SSH"), rows)
    return 0


def _print_boot_timeout_running_hint(vm_id: str, boot_timeout: float) -> None:
    print(
        f"sbx: VM '{vm_id}' started, but SSH was not ready within {boot_timeout:g}s.",
        file=sys.stderr,
    )
    print(
        "sbx: The VM is still running and may finish booting shortly. "
        f"Try `sbx run {vm_id}` again, or increase the timeout with "
        f"`sbx run {vm_id} --boot-timeout {int(max(boot_timeout * 2, boot_timeout + 1))}`.",
        file=sys.stderr,
    )
    print(
        "sbx: To make it persistent, set `[sbx].boot_timeout` in .sbx.toml.",
        file=sys.stderr,
    )
    print("sbx: If this keeps happening, run `sbx doctor`.", file=sys.stderr)


def _maybe_print_boot_timeout_running_hint(vm_id: str | None, boot_timeout: float) -> bool:
    if not vm_id:
        return False
    if _get_existing_vm_status(vm_id) != "running":
        return False
    _print_boot_timeout_running_hint(vm_id, boot_timeout)
    return True


def _start_existing_vm_if_needed(vm_id: str, status: str, boot_timeout: float) -> int:
    if status == "running":
        return 0
    if status == "error":
        print(f"sbx: VM '{vm_id}' is in error state.", file=sys.stderr)
        print(
            "sbx: Run `sbx doctor --fix` to repair local VM bookkeeping, "
            f"then retry `sbx run {vm_id}`.",
            file=sys.stderr,
        )
        print(f"sbx: If it still fails, run `sbx recreate {vm_id} --force`.", file=sys.stderr)
        return 1
    rc = _run_smolvm(["sandbox", "start", vm_id, "--boot-timeout", f"{boot_timeout:g}"])
    if rc != 0:
        _maybe_print_boot_timeout_running_hint(vm_id, boot_timeout)
    return rc


def _smolvm_error_message(stdout: str, fallback: str) -> str:
    try:
        payload = json.loads(stdout)
        message = payload["error"]["message"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return fallback
    return message if isinstance(message, str) and message else fallback


def _print_start_failure(stdout: str) -> None:
    message = _smolvm_error_message(stdout, "SmolVM failed to start the VM.")
    print(f"sbx: {message}", file=sys.stderr)
    if "QEMU exited early" in message:
        print(
            "sbx: The VM was created but the backend failed during boot. "
            "Try `sbx recreate <name> --force`; if it repeats, run `sbx doctor` "
            "and inspect the SmolVM backend runtime logs.",
            file=sys.stderr,
        )


def _extract_started_vm_name(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
        name = payload["data"]["vm"]["name"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ConfigError("could not read VM name from smolvm JSON output") from exc
    if not isinstance(name, str) or not name:
        raise ConfigError("smolvm JSON output did not include a VM name")
    return name


def _host_timezone() -> str:
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


def _sync_guest_clock(vm_id: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    timezone = _host_timezone()
    script = f"""
set -eu
zone={shlex.quote(timezone)}
if [ -f "/usr/share/zoneinfo/$zone" ]; then
  ln -sf "/usr/share/zoneinfo/$zone" /etc/localtime
  printf '%s\n' "$zone" > /etc/timezone
fi
date -u -s {shlex.quote(timestamp)}
"""
    cmd = guest_customization.ssh_command(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = _run_capture(cmd)
    if completed is None:
        raise ConfigError("failed to sync VM clock: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to sync VM clock: {stderr}")


def _stop_vm_if_last_session(vm_id: str, *, stop_on_exit: bool) -> None:
    if not stop_on_exit:
        _debug(f"not stopping {vm_id}: stop_on_exit disabled")
        return
    if session_state.active_sessions(vm_id):
        _debug(f"not stopping {vm_id}: other sbx sessions still active")
        return
    _debug(f"stopping {vm_id}: no other sbx sessions active")
    _run_smolvm(["sandbox", "stop", vm_id])


def _post_start_actions(
    *,
    vm_name: str,
    agent: str,
    attach: bool,
    run_user: str | None,
    auth_port: bool,
    auth_host_port: int,
    auth_guest_port: int,
    stop_on_exit: bool,
    launch_command: str | None = None,
    cwd: str | None = None,
    git_config_text: str | None = None,
) -> int:
    _sync_guest_clock(vm_name)
    if auth_port:
        port_rc = network.expose_auth_port(vm_name, auth_host_port, auth_guest_port)
        if port_rc != 0:
            return port_rc
    if not attach:
        return 0
    command = launch_command or LAUNCH_COMMANDS[agent]
    session_state.register_session(vm_name, "run")
    try:
        if run_user is not None:
            guest_customization.prepare_run_user(vm_name, run_user, run_capture=_run_capture)
            guest_customization.install_git_config(
                vm_name, run_user, git_config_text, run_capture=_run_capture
            )
            return guest_customization.attach_as_user(vm_name, run_user, command, cwd=cwd, run=_run)
        guest_customization.install_git_config(
            vm_name, None, git_config_text, run_capture=_run_capture
        )
        return guest_customization.attach_as_root(vm_name, command, cwd=cwd, run=_run)
    finally:
        remaining = session_state.unregister_session(vm_name)
        _debug(f"unregistered session for {vm_name}; remaining={remaining}")
        _stop_vm_if_last_session(vm_name, stop_on_exit=stop_on_exit)


def _arg_or_config(
    args: argparse.Namespace,
    attr: str,
    config: Mapping[str, Any],
    section: str,
    key: str | None = None,
    default: Any = None,
) -> Any:
    value = getattr(args, attr, None)
    if value is not None:
        return value
    return _cfg(config, section, key or attr, default)


def _manifest_sbx(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    sbx = manifest.get("sbx", {})
    if not isinstance(sbx, Mapping):
        raise ConfigError("image manifest field 'sbx' must be an object")
    return sbx


def _project_config_path() -> Path:
    return LOCAL_CONFIG_PATHS[0]


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _project_config_values(
    args: argparse.Namespace, config: Mapping[str, Any], *, vm_name: str, agent: str
) -> dict[str, Any]:
    values: dict[str, Any] = {"name": vm_name, "agent": agent}
    for key in ("image", "memory", "cpus", "disk_size", "project_path", "run_user"):
        value = getattr(args, key, None)
        if value is not None:
            values[key] = value
    if getattr(args, "writable_mounts", None) is not None:
        values["writable_mounts"] = bool(args.writable_mounts)
    if getattr(args, "env", None) is not None:
        values["env"] = list(args.env)
    return values


def _insert_missing_sbx_values(text: str, values: Mapping[str, Any]) -> tuple[str, list[str]]:
    try:
        parsed = tomllib.loads(text)
        existing_sbx = _sbx_config(parsed)
    except (ConfigError, tomllib.TOMLDecodeError):
        existing_sbx = {}
    missing = [key for key in values if key not in existing_sbx]
    if not missing:
        return text, []

    lines_to_add = [f"{key} = {_toml_value(values[key])}" for key in missing]
    lines = text.splitlines()
    sbx_header = next((i for i, line in enumerate(lines) if line.strip() == "[sbx]"), None)
    if sbx_header is None:
        prefix = "\n" if text and not text.endswith("\n") else ""
        return text + prefix + "[sbx]\n" + "\n".join(lines_to_add) + "\n", missing

    insert_at = len(lines)
    for index in range(sbx_header + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            insert_at = index
            break
    updated = lines[:insert_at] + lines_to_add + lines[insert_at:]
    return "\n".join(updated) + "\n", missing


def _write_project_config_values(path: Path, values: Mapping[str, Any]) -> list[str]:
    if path.exists():
        original = path.read_text(encoding="utf-8")
        updated, added = _insert_missing_sbx_values(original, values)
        if added:
            path.write_text(updated, encoding="utf-8")
        return added

    path.write_text(
        "# Project defaults for sbx. Edit as needed.\n"
        "[sbx]\n"
        + "\n".join(f"{key} = {_toml_value(value)}" for key, value in values.items())
        + "\n",
        encoding="utf-8",
    )
    return list(values)


def _maybe_write_project_config(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    *,
    vm_name: str,
    agent: str,
    created: bool,
) -> None:
    if getattr(args, "action", None) not in {"run", "create"}:
        return
    write_config = getattr(args, "write_config", None)
    path = _project_config_path()
    exists = path.exists()
    if exists and write_config is not True:
        return
    if not exists and write_config is False:
        return
    if not exists and not created and write_config is not True:
        return

    values = _project_config_values(args, config, vm_name=vm_name, agent=agent)
    added = _write_project_config_values(path, values)
    if not exists:
        print(f"sbx: wrote {path.name} for project defaults", file=sys.stderr)
    elif added:
        print(
            f"sbx: updated {path.name} with missing project defaults: {', '.join(added)}",
            file=sys.stderr,
        )
    else:
        print(
            f"sbx: {path.name} already contains project defaults; no changes made",
            file=sys.stderr,
        )


def _start_preset_with_sdk(
    *,
    args: argparse.Namespace,
    config: Mapping[str, Any],
    agent: str,
    cpus: int | None,
    mounts: Sequence[str],
    writable_mounts: bool,
    attach: bool,
    run_user: str | None,
    auth_port: bool,
    auth_host_port: int,
    auth_guest_port: int,
    stop_on_exit: bool,
    cwd: str | None,
    git_config_text: str | None,
    copy_host_credentials: bool,
    forward_env: list[str],
    json_output: bool,
    port_forwards: Sequence[str],
) -> int:
    from smolvm import SmolVM
    from smolvm.facade import _build_auto_config
    from smolvm.presets import apply_preset, get_preset
    from smolvm.types import PortForwardConfig

    preset_name = "claude-code" if agent == "claude" else agent
    preset = get_preset(preset_name)
    memory = _arg_or_config(args, "memory", config, "sbx", default=preset.default_mem_mib)
    disk_size = _arg_or_config(args, "disk_size", config, "sbx", default=preset.default_disk_mib)
    requested_name = _arg_or_config(args, "name", config, "sbx")
    requested_os = _arg_or_config(args, "os", config, "sbx", default="ubuntu")
    install_timeout = int(_arg_or_config(args, "install_timeout", config, "sbx", default=600))
    boot_timeout = _validate_boot_timeout(
        _arg_or_config(args, "boot_timeout", config, "sbx", default=DEFAULT_BOOT_TIMEOUT)
    )

    def start() -> str:
        vm = None
        config_obj, ssh_key_path = _build_auto_config(
            vm_name=str(requested_name) if requested_name else None,
            name_prefix=agent,
            os=str(requested_os),
            backend=DEFAULT_BACKEND,
            memory=int(memory),
            disk_size_mib=int(disk_size),
            ssh_key_path=None,
        )
        updates: dict[str, Any] = {
            "port_forwards": [
                PortForwardConfig(**item)
                for item in network.port_forwards_from_specs(port_forwards)
            ]
        }
        if cpus is not None:
            updates["vcpu_count"] = cpus
        config_obj = config_obj.model_copy(update=updates)
        try:
            vm = SmolVM(
                config_obj,
                ssh_key_path=ssh_key_path,
                mounts=list(mounts),
                writable_mounts=writable_mounts,
            )
            vm.start(boot_timeout=boot_timeout)
            vm.wait_for_ssh(timeout=boot_timeout)
            guest_customization.set_hostname(str(vm.vm_id), run_capture=_run_capture)
            ssh = vm._ensure_ssh_for_env()
            apply_preset(ssh, preset, install_timeout=install_timeout)
            return str(vm.vm_id)
        finally:
            if vm is not None:
                vm.close()

    env = guest_customization.sanitize_forwarded_env(dict(os.environ), forward_env)
    temp_home_ctx = None
    if not copy_host_credentials:
        temp_home_ctx = tempfile.TemporaryDirectory(prefix="sbx-no-credentials-")
        env = guest_customization.credential_free_env(
            Path(temp_home_ctx.name), forward_env=forward_env
        )
        _debug(f"credential-free HOME: {temp_home_ctx.name}")

    try:
        with _patched_environ(env):
            vm_name = start()
    finally:
        if temp_home_ctx is not None:
            temp_home_ctx.cleanup()

    _maybe_write_project_config(args, config, vm_name=vm_name, agent=agent, created=True)
    vm_metadata.record_vm_project(vm_name, _project_identity(args, config))

    if json_output:
        print(json.dumps({"vm": {"name": vm_name, "status": "running"}}))
    elif attach:
        user_msg = f" as user {run_user}" if run_user is not None else ""
        print(f"Started '{vm_name}'. Launching {agent}{user_msg}...")
    else:
        print(f"Started '{vm_name}'.")

    return _post_start_actions(
        vm_name=vm_name,
        agent=agent,
        attach=attach,
        run_user=run_user,
        auth_port=auth_port,
        auth_host_port=auth_host_port,
        auth_guest_port=auth_guest_port,
        stop_on_exit=stop_on_exit,
        cwd=cwd,
        git_config_text=git_config_text,
    )


def _start_local_image(
    *,
    args: argparse.Namespace,
    config: Mapping[str, Any],
    image_dir: Path,
    manifest: Mapping[str, Any],
    agent: str,
    mounts: Sequence[str],
    writable_mounts: bool,
    attach: bool,
    run_user: str | None,
    auth_port: bool,
    auth_host_port: int,
    auth_guest_port: int,
    stop_on_exit: bool,
    cwd: str | None,
    git_config_text: str | None,
    forward_env: list[str] | None = None,
    port_forwards: Sequence[str] = (),
) -> int:
    from smolvm import SmolVM, VMConfig
    from smolvm.utils import ensure_ssh_key

    sbx_manifest = _manifest_sbx(manifest)
    manifest_agent = sbx_manifest.get("agent")
    if manifest_agent is not None and manifest_agent != agent:
        raise ConfigError(
            f"image agent {manifest_agent!r} does not match configured agent {agent!r}"
        )
    launch_command = sbx_manifest.get("launch_command")
    if launch_command is not None and not isinstance(launch_command, str):
        raise ConfigError("image manifest field 'sbx.launch_command' must be a string")

    kernel_path = lifecycle_warnings.manifest_path(image_dir, manifest, "kernel")
    rootfs_path = lifecycle_warnings.manifest_path(image_dir, manifest, "rootfs")
    if not kernel_path.is_file():
        raise ConfigError(f"image kernel not found: {kernel_path}")
    if not rootfs_path.is_file():
        raise ConfigError(f"image rootfs not found: {rootfs_path}")

    initrd_value = manifest.get("initrd")
    initrd_path = None
    if initrd_value is not None:
        initrd_path = lifecycle_warnings.manifest_path(image_dir, manifest, "initrd")
        if not initrd_path.is_file():
            raise ConfigError(f"image initrd not found: {initrd_path}")

    boot_args = manifest.get("boot_args")
    if boot_args is not None and not isinstance(boot_args, str):
        raise ConfigError("image manifest field 'boot_args' must be a string")
    if boot_args is None:
        boot_args = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"

    private_key, public_key = ensure_ssh_key()
    requested_name = _arg_or_config(args, "name", config, "sbx")
    memory = int(_arg_or_config(args, "memory", config, "sbx", default=512))
    cpus_value = _arg_or_config(args, "cpus", config, "sbx")
    disk_size = _arg_or_config(args, "disk_size", config, "sbx")
    boot_timeout = _validate_boot_timeout(
        _arg_or_config(args, "boot_timeout", config, "sbx", default=DEFAULT_BOOT_TIMEOUT)
    )

    vm_config: dict[str, Any] = {
        "memory": memory,
        "kernel_path": kernel_path,
        "initrd_path": initrd_path,
        "rootfs_path": rootfs_path,
        "boot_args": boot_args,
        "backend": DEFAULT_BACKEND,
        "ssh_capable": True,
        "ssh_public_key": public_key.read_text(encoding="utf-8").strip(),
        "port_forwards": network.port_forwards_from_specs(port_forwards),
    }
    if cpus_value is not None:
        vm_config["vcpu_count"] = _validate_cpus(cpus_value)
    if disk_size is not None:
        disk_size_mib = int(disk_size)
        image_size_mib = lifecycle_warnings.rootfs_size_mib(rootfs_path)
        if image_size_mib is not None and disk_size_mib < image_size_mib:
            raise ConfigError(
                lifecycle_warnings.local_image_disk_size_error(disk_size_mib, image_size_mib)
            )
        vm_config["disk_size_mib"] = disk_size_mib
        if rootfs_path.suffix.lower() != ".qcow2":
            vm_config["grow_filesystem"] = True
    if requested_name:
        vm_config["vm_id"] = str(requested_name)

    vm = SmolVM(
        VMConfig(**vm_config),
        ssh_key_path=str(private_key),
        mounts=list(mounts),
        writable_mounts=writable_mounts,
    )
    try:
        vm.start(boot_timeout=boot_timeout)
        vm.wait_for_ssh(timeout=boot_timeout)
        vm_name = vm.vm_id
        guest_customization.set_hostname(str(vm_name), run_capture=_run_capture)
    except Exception:
        vm.close()
        raise
    vm.close()

    _maybe_write_project_config(args, config, vm_name=str(vm_name), agent=agent, created=True)
    vm_metadata.record_vm_project(str(vm_name), _project_identity(args, config))

    if attach:
        print(f"Started '{vm_name}'. Launching {agent}...")
        if not _sync_forwarded_env_or_error(str(vm_name), forward_env or []):
            return 1
    else:
        print(f"Started '{vm_name}'.")
    return _post_start_actions(
        vm_name=vm_name,
        agent=agent,
        attach=attach,
        run_user=run_user,
        auth_port=auth_port,
        auth_host_port=auth_host_port,
        auth_guest_port=auth_guest_port,
        stop_on_exit=stop_on_exit,
        launch_command=launch_command,
        cwd=cwd,
        git_config_text=git_config_text,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    rc = _run_smolvm(["doctor", "--backend", DEFAULT_BACKEND])
    lifecycle_warnings.doctor_config_state(
        getattr(args, "config_data", {}), smolvm_info=_smolvm_info_vm
    )
    return rc or doctor.run_doctor_checks(fix=bool(getattr(args, "fix", False)))


def cmd_completion(args: argparse.Namespace) -> int:
    print(completion_script(args.shell), end="")
    return 0


def cmd_image_build_debian(args: argparse.Namespace) -> int:
    return build_debian.main_from_args(args)


def cmd_image_ls(args: argparse.Namespace) -> int:
    return sbx.image.ls.main_from_args(args)


def cmd_start(args: argparse.Namespace) -> int:
    config = args.config_data
    sbx_cfg = _sbx_config(config)
    project_identity = _project_identity(args, config)
    project_root = Path(project_identity["project_root"])
    if args.name is None and args.name_arg is not None:
        args.name = args.name_arg

    agent = args.agent or _cfg_agent(config)
    if agent not in AGENTS:
        print(f"sbx: agent must be one of: {', '.join(AGENTS)}", file=sys.stderr)
        return 2

    argv: list[str] = [str(agent), "start"]
    configured_backend = _cfg(config, "sbx", "backend", DEFAULT_BACKEND)
    if configured_backend != DEFAULT_BACKEND:
        print("sbx: other backends are not supported yet", file=sys.stderr)
        return 2

    scalar_options = (
        ("name", "--name"),
        ("memory", "--memory"),
        ("disk_size", "--disk-size"),
        ("os", "--os"),
        ("install_timeout", "--install-timeout"),
    )
    for attr, flag in scalar_options:
        value = _arg_or_config(args, attr, config, "sbx")
        if value is not None:
            argv += [flag, str(value)]
    argv += ["--backend", DEFAULT_BACKEND]
    boot_timeout = _validate_boot_timeout(
        _arg_or_config(args, "boot_timeout", config, "sbx", default=DEFAULT_BOOT_TIMEOUT)
    )
    argv += ["--boot-timeout", f"{boot_timeout:g}"]

    mounts = (
        args.mount
        if args.mount is not None
        else _list_value(sbx_cfg.get("mount"), key="[sbx].mount")
    )
    effective_mounts: list[str] = []

    project_path = _arg_or_config(args, "project_path", config, "sbx")
    project_guest_cwd = _project_guest_cwd(project_path)
    if project_path is not None:
        project_mount = _same_path_mount(str(project_path))
        effective_mounts.append(project_mount)
        argv += ["--mount", project_mount]

    for mount in mounts or []:
        mount = mount if ":" in mount else _same_path_mount(mount)
        effective_mounts.append(mount)
        argv += ["--mount", mount]

    writable_mounts = bool(_arg_or_config(args, "writable_mounts", config, "sbx", default=False))
    if project_path is not None:
        writable_mounts = True
    if writable_mounts:
        argv.append("--writable-mounts")

    attach = True if args.attach is None else bool(args.attach)
    run_user = _arg_or_config(args, "run_user", config, "sbx")
    if run_user is not None:
        run_user = _validate_run_user(str(run_user))
    copy_host_credentials = bool(
        _arg_or_config(args, "copy_host_credentials", config, "sbx", default=False)
    )
    forward_env = guest_customization.validate_env_names(
        args.env if args.env is not None else _list_value(sbx_cfg.get("env"), key="[sbx].env")
    )
    _debug(
        "run options: "
        f"agent={agent!r}, name={getattr(args, 'name', None)!r}, attach={attach!r}, "
        f"run_user={run_user!r}, copy_host_credentials={copy_host_credentials!r}, "
        f"forward_env={forward_env!r}"
    )

    auth_port = bool(_arg_or_config(args, "auth_port", config, "sbx", default=True))
    auth_host_port = int(_arg_or_config(args, "auth_host_port", config, "sbx", default=1455))
    auth_guest_port = int(_arg_or_config(args, "auth_guest_port", config, "sbx", default=1455))
    stop_on_exit = bool(_arg_or_config(args, "stop_on_exit", config, "sbx", default=True))
    git_config = bool(_arg_or_config(args, "git_config", config, "sbx", default=True))
    git_config_text = guest_customization.host_git_config(project_root) if git_config else None
    cpus_value = _arg_or_config(args, "cpus", config, "sbx")
    cpus = _validate_cpus(cpus_value) if cpus_value is not None else None
    try:
        port_forwards = _list_value(sbx_cfg.get("port_forwards"), key="[sbx].port_forwards")
        network.port_forwards_from_specs(port_forwards)
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2

    requested_name = _arg_or_config(args, "name", config, "sbx")
    if requested_name:
        requested_name = _validate_vm_name(str(requested_name))
        existing_status = _get_existing_vm_status(str(requested_name))
        _debug(f"existing VM lookup: name={requested_name!r}, status={existing_status!r}")
        if existing_status is not None:
            if existing_status != "running":
                try:
                    _sync_existing_vm_start_config(
                        str(requested_name),
                        effective_mounts,
                        writable_mounts=writable_mounts,
                        port_forwards=port_forwards,
                        project=project_identity,
                    )
                except ConfigError as exc:
                    print(f"sbx: {exc}", file=sys.stderr)
                    return 2
            else:
                _warn_running_mount_drift(
                    str(requested_name), effective_mounts, writable_mounts=writable_mounts
                )
            start_rc = _start_existing_vm_if_needed(
                str(requested_name),
                existing_status,
                boot_timeout,
            )
            if start_rc != 0:
                return start_rc
            if attach and not _sync_forwarded_env_or_error(str(requested_name), forward_env):
                return 1
            _maybe_write_project_config(
                args, config, vm_name=str(requested_name), agent=str(agent), created=False
            )
            vm_metadata.record_vm_project(str(requested_name), project_identity)
            return _post_start_actions(
                vm_name=str(requested_name),
                agent=str(agent),
                attach=attach,
                run_user=str(run_user) if run_user is not None else None,
                auth_port=auth_port,
                auth_host_port=auth_host_port,
                auth_guest_port=auth_guest_port,
                stop_on_exit=stop_on_exit,
                cwd=project_guest_cwd,
                git_config_text=git_config_text,
            )

    image = lifecycle_warnings.path_from_config(_arg_or_config(args, "image", config, "sbx"))
    if image is not None:
        try:
            manifest = lifecycle_warnings.local_image_manifest(image)
            return _start_local_image(
                args=args,
                config=config,
                image_dir=image,
                manifest=manifest,
                agent=str(agent),
                mounts=effective_mounts,
                writable_mounts=writable_mounts,
                attach=attach,
                run_user=str(run_user) if run_user is not None else None,
                auth_port=auth_port,
                auth_host_port=auth_host_port,
                auth_guest_port=auth_guest_port,
                stop_on_exit=stop_on_exit,
                cwd=project_guest_cwd,
                git_config_text=git_config_text,
                forward_env=forward_env,
                port_forwards=port_forwards,
            )
        except ConfigError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - keep CLI errors user-friendly.
            if _maybe_print_boot_timeout_running_hint(
                str(requested_name) if requested_name else None, boot_timeout
            ):
                return 1
            print(f"sbx: failed to start image: {exc}", file=sys.stderr)
            return 1

    if cpus is not None or port_forwards:
        try:
            return _start_preset_with_sdk(
                args=args,
                config=config,
                agent=str(agent),
                cpus=cpus,
                mounts=effective_mounts,
                writable_mounts=writable_mounts,
                attach=attach,
                run_user=str(run_user) if run_user is not None else None,
                auth_port=auth_port,
                auth_host_port=auth_host_port,
                auth_guest_port=auth_guest_port,
                stop_on_exit=stop_on_exit,
                cwd=project_guest_cwd,
                git_config_text=git_config_text,
                copy_host_credentials=copy_host_credentials,
                forward_env=forward_env,
                json_output=bool(args.json),
                port_forwards=port_forwards,
            )
        except ConfigError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - keep CLI errors user-friendly.
            if _maybe_print_boot_timeout_running_hint(
                str(requested_name) if requested_name else None, boot_timeout
            ):
                return 1
            print(f"sbx: failed to start preset with cpus={cpus}: {exc}", file=sys.stderr)
            return 1

    managed_start = auth_port or (
        attach
        and (run_user is not None or project_guest_cwd is not None or git_config_text is not None)
    )
    argv.append("--no-attach" if managed_start or not attach else "--attach")

    json_output = bool(args.json)
    if json_output or managed_start:
        argv.append("--json")

    temp_home_ctx = None
    smolvm_env = guest_customization.sanitize_forwarded_env(dict(os.environ), forward_env)
    if not copy_host_credentials:
        temp_home_ctx = tempfile.TemporaryDirectory(prefix="sbx-no-credentials-")
        smolvm_env = guest_customization.credential_free_env(
            Path(temp_home_ctx.name), forward_env=forward_env
        )
        _debug(f"credential-free HOME: {temp_home_ctx.name}")

    if not managed_start:
        try:
            rc = _run_smolvm(argv, env=smolvm_env)
            if rc == 0 and requested_name:
                guest_customization.set_hostname(str(requested_name), run_capture=_run_capture)
                _maybe_write_project_config(
                    args, config, vm_name=str(requested_name), agent=str(agent), created=True
                )
                vm_metadata.record_vm_project(str(requested_name), project_identity)
            return rc
        finally:
            if temp_home_ctx is not None:
                temp_home_ctx.cleanup()

    completed = _run_smolvm_capture(argv, env=smolvm_env)
    if temp_home_ctx is not None:
        temp_home_ctx.cleanup()
    if completed is None:
        return 127
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        if json_output and completed.stdout:
            print(completed.stdout, end="")
        elif completed.stdout:
            _print_start_failure(completed.stdout)
        _maybe_print_boot_timeout_running_hint(
            str(requested_name) if requested_name else None, boot_timeout
        )
        return completed.returncode

    vm_name = _extract_started_vm_name(completed.stdout)
    guest_customization.set_hostname(vm_name, run_capture=_run_capture)
    _maybe_write_project_config(args, config, vm_name=vm_name, agent=str(agent), created=True)
    vm_metadata.record_vm_project(vm_name, project_identity)
    if auth_port:
        port_rc = network.expose_auth_port(vm_name, auth_host_port, auth_guest_port)
        if port_rc != 0:
            return port_rc

    if json_output:
        print(completed.stdout, end="")
    elif attach:
        user_msg = f" as user {run_user}" if run_user is not None else ""
        print(f"Started '{vm_name}'. Launching {agent}{user_msg}...")
    else:
        print(f"Started '{vm_name}'. Auth callback port: localhost:{auth_host_port}")

    if not attach:
        return 0
    session_state.register_session(vm_name, "run")
    try:
        if run_user is not None:
            guest_customization.prepare_run_user(vm_name, str(run_user), run_capture=_run_capture)
            guest_customization.install_git_config(
                vm_name, str(run_user), git_config_text, run_capture=_run_capture
            )
            return guest_customization.attach_as_user(
                vm_name, str(run_user), LAUNCH_COMMANDS[str(agent)], cwd=project_guest_cwd, run=_run
            )
        guest_customization.install_git_config(
            vm_name, None, git_config_text, run_capture=_run_capture
        )
        return guest_customization.attach_as_root(
            vm_name, LAUNCH_COMMANDS[str(agent)], cwd=project_guest_cwd, run=_run
        )
    finally:
        remaining = session_state.unregister_session(vm_name)
        _debug(f"unregistered session for {vm_name}; remaining={remaining}")
        _stop_vm_if_last_session(vm_name, stop_on_exit=stop_on_exit)


def _confirm_destructive_action(message: str, *, force: bool) -> bool:
    if force:
        return True
    if not sys.stdin.isatty():
        print(f"sbx: refusing destructive action without --force: {message}", file=sys.stderr)
        return False
    answer = input(f"{message} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _vm_name_from_arg_or_config(
    args: argparse.Namespace, config: Mapping[str, Any], command: str
) -> str | None:
    name = getattr(args, "name", None) or _cfg(config, "sbx", "name")
    if not name:
        print(f"sbx: {command} requires a VM name argument or [sbx].name", file=sys.stderr)
        return None
    return str(name)


def cmd_shell(args: argparse.Namespace) -> int:
    config = args.config_data
    project_identity = _project_identity(args, config)
    name = _vm_name_from_arg_or_config(args, config, "shell")
    if name is None:
        return 2
    try:
        forward_env = guest_customization.validate_env_names(
            _list_value(_sbx_config(config).get("env"), key="[sbx].env")
        )
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2
    run_user = None if args.root else args.run_user or _cfg(config, "sbx", "run_user")
    if run_user is not None:
        run_user = _validate_run_user(str(run_user))
    project_path = args.project_path or _cfg(config, "sbx", "project_path")
    project_guest_cwd = _project_guest_cwd(project_path)
    smolvm_command = ["sandbox", "ssh", name]
    keep_running = bool(getattr(args, "keep_running", False))
    stop_on_exit = bool(_cfg(config, "sbx", "stop_on_exit", True)) and not keep_running
    git_config = bool(
        args.git_config if args.git_config is not None else _cfg(config, "sbx", "git_config", True)
    )
    git_config_text = (
        guest_customization.host_git_config(Path(project_identity["project_root"]))
        if git_config
        else None
    )
    try:
        port_forwards = _list_value(
            _sbx_config(config).get("port_forwards"), key="[sbx].port_forwards"
        )
        network.port_forwards_from_specs(port_forwards)
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2
    existing_status = _get_existing_vm_status(name)
    if (
        run_user is not None or project_guest_cwd is not None or git_config_text is not None
    ) and existing_status is None:
        print(f"sbx: {guest_customization.missing_vm_message(name)}", file=sys.stderr)
        return 1
    if existing_status is not None:
        if existing_status != "running":
            try:
                _sync_existing_vm_start_config(
                    name,
                    None,
                    writable_mounts=False,
                    port_forwards=port_forwards,
                    project=project_identity,
                )
            except ConfigError as exc:
                print(f"sbx: {exc}", file=sys.stderr)
                return 2
        else:
            sbx_cfg = _sbx_config(config)
            mounts = _list_value(sbx_cfg.get("mount"), key="[sbx].mount")
            effective_mounts = []
            if project_path is not None:
                effective_mounts.append(_same_path_mount(str(project_path)))
            effective_mounts.extend(
                mount if ":" in mount else _same_path_mount(mount) for mount in mounts
            )
            if effective_mounts:
                _warn_running_mount_drift(
                    name, effective_mounts, writable_mounts=project_path is not None
                )
        start_rc = _start_existing_vm_if_needed(
            name,
            existing_status,
            DEFAULT_BOOT_TIMEOUT,
        )
        if start_rc != 0:
            return start_rc
    if not _sync_forwarded_env_or_error(name, forward_env):
        return 1
    session_state.register_session(name, "shell")
    try:
        if run_user is not None:
            guest_customization.prepare_run_user(name, run_user, run_capture=_run_capture)
            guest_customization.install_git_config(
                name, run_user, git_config_text, run_capture=_run_capture
            )
            return guest_customization.attach_as_user(
                name, run_user, "bash", cwd=project_guest_cwd, run=_run
            )
        if project_guest_cwd is not None or git_config_text is not None:
            guest_customization.install_git_config(
                name, None, git_config_text, run_capture=_run_capture
            )
            return guest_customization.attach_as_root(name, "bash", cwd=project_guest_cwd, run=_run)
        return _run_smolvm(smolvm_command)
    finally:
        remaining = session_state.unregister_session(name)
        _debug(f"unregistered session for {name}; remaining={remaining}")
        _stop_vm_if_last_session(name, stop_on_exit=stop_on_exit)


def cmd_stop(args: argparse.Namespace) -> int:
    config = args.config_data
    name = _vm_name_from_arg_or_config(args, config, "stop")
    if name is None:
        return 2
    return _run_smolvm(["sandbox", "stop", name])


def cmd_remove(args: argparse.Namespace) -> int:
    config = args.config_data
    name = _vm_name_from_arg_or_config(args, config, str(args.action))
    if name is None:
        return 2
    if not _confirm_destructive_action(f"Destroy VM '{name}'?", force=args.force):
        return 2
    return _delete_vm(name)


def _delete_vm(vm_id: str, extra_args: Sequence[str] | None = None) -> int:
    extra = list(extra_args or [])
    completed = _run_smolvm_capture(["sandbox", "delete", vm_id, *extra, "--json"])
    if completed is None:
        return 127
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)

    try:
        payload = json.loads(completed.stdout)
        failed = payload.get("data", {}).get("failed", [])
    except json.JSONDecodeError:
        failed = []

    if completed.returncode == 0:
        with suppress(ConfigError):
            metadata = vm_metadata.load_vm_metadata()
            if vm_id in metadata:
                metadata.pop(vm_id, None)
                vm_metadata.save_vm_metadata(metadata)
        print(f"Destroyed VM '{vm_id}'.")
        return 0

    if any(item.get("error") == f"VM '{vm_id}' not found" for item in failed):
        print(f"VM '{vm_id}' not found; nothing to destroy.")
        return 0

    if completed.stdout:
        print(completed.stdout, end="")
    return completed.returncode


def cmd_create(args: argparse.Namespace) -> int:
    args.attach = False
    if args.auth_port is None:
        args.auth_port = False
    return cmd_start(args)


def cmd_recreate(args: argparse.Namespace) -> int:
    config = args.config_data
    force = args.force
    name = args.name or args.name_arg or _cfg(config, "sbx", "name")
    if not name:
        print("sbx: recreate requires a VM name argument, --name, or [sbx].name", file=sys.stderr)
        return 2

    if not _confirm_destructive_action(f"Destroy and recreate VM '{name}'?", force=force):
        return 2

    destroy_rc = _delete_vm(str(name))
    if destroy_rc != 0:
        return destroy_rc
    args.name = str(name)
    args.attach = False
    if args.auth_port is None:
        args.auth_port = False
    return cmd_start(args)


def _add_start_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent", choices=AGENTS, help="Agent preset to run (default: [sbx].agent or pi)."
    )
    parser.add_argument("--name")
    parser.add_argument("--memory", type=int, metavar="MIB")
    parser.add_argument("--cpus", type=int, metavar="COUNT", help="Number of virtual CPUs.")
    parser.add_argument("--disk-size", type=int, metavar="MIB")
    parser.add_argument("--os")
    parser.add_argument("--image", help="Local ready-to-run image directory.")
    parser.add_argument("--mount", action="append", metavar="HOST_PATH[:GUEST_PATH]")
    parser.add_argument(
        "--project-path",
        help="Mount this host path at the same absolute guest path as read-write.",
    )
    parser.add_argument(
        "--run-user",
        help="When attaching, create/use this guest user and run the agent as that user.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=None,
        metavar="KEY",
        help="Forward this host environment variable into the guest. Can be repeated.",
    )
    auth_port = parser.add_mutually_exclusive_group()
    auth_port.add_argument(
        "--auth-port",
        dest="auth_port",
        action="store_true",
        default=None,
        help="Expose the agent OAuth callback port before attaching (default).",
    )
    auth_port.add_argument(
        "--no-auth-port",
        dest="auth_port",
        action="store_false",
        help="Do not expose the agent OAuth callback port automatically.",
    )
    parser.add_argument("--auth-host-port", type=int, help="Host OAuth callback port.")
    parser.add_argument("--auth-guest-port", type=int, help="Guest OAuth callback port.")
    credential_copy = parser.add_mutually_exclusive_group()
    credential_copy.add_argument(
        "--copy-host-credentials",
        dest="copy_host_credentials",
        action="store_true",
        default=None,
        help="Allow SmolVM presets to copy host CLI config files.",
    )
    credential_copy.add_argument(
        "--no-copy-host-credentials",
        dest="copy_host_credentials",
        action="store_false",
        help="Do not copy host CLI config files (default).",
    )
    git_config = parser.add_mutually_exclusive_group()
    git_config.add_argument(
        "--git-config",
        dest="git_config",
        action="store_true",
        default=None,
        help="Copy safe host Git identity/config into the guest (default).",
    )
    git_config.add_argument(
        "--no-git-config",
        dest="git_config",
        action="store_false",
        help="Do not copy host Git identity/config into the guest.",
    )
    parser.add_argument("--writable-mounts", action="store_true", default=None)
    attach = parser.add_mutually_exclusive_group()
    attach.add_argument("--attach", dest="attach", action="store_true", default=None)
    attach.add_argument(
        "--no-attach",
        dest="attach",
        action="store_false",
        help="Create VM but do not launch the agent.",
    )
    stop_on_exit = parser.add_mutually_exclusive_group()
    stop_on_exit.add_argument(
        "--stop-on-exit",
        dest="stop_on_exit",
        action="store_true",
        default=None,
        help="Stop the VM when this sbx session exits and no other sbx sessions remain.",
    )
    stop_on_exit.add_argument(
        "--keep-running",
        dest="stop_on_exit",
        action="store_false",
        help="Keep the VM running after this sbx session exits.",
    )
    parser.add_argument(
        "--boot-timeout",
        type=float,
        help="Seconds to wait for VM boot/SSH readiness (default: [sbx].boot_timeout or 60).",
    )
    parser.add_argument("--install-timeout", type=float)
    write_config = parser.add_mutually_exclusive_group()
    write_config.add_argument(
        "--write-config",
        dest="write_config",
        action="store_true",
        default=None,
        help="Create or update .sbx.toml with missing project defaults.",
    )
    write_config.add_argument(
        "--no-write-config",
        dest="write_config",
        action="store_false",
        help="Do not create .sbx.toml automatically for this invocation.",
    )
    parser.add_argument("--json", action="store_true", default=None)
    parser.add_argument("name_arg", nargs="?", metavar="NAME", help="Sandbox name.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sbx", description="Run coding agents inside a SmolVM sandbox."
    )
    parser.add_argument(
        "--config", help="Path to a TOML config file. Overrides default and local config files."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print sbx debug diagnostics to stderr."
    )
    parser.add_argument("--version", action="version", version=f"sbx {__version__}")
    sub = parser.add_subparsers(dest="action", required=True)

    run = sub.add_parser("run", help="Run an agent session in a sandbox.")
    _add_start_options(run)
    run.set_defaults(func=cmd_start)

    create = sub.add_parser(
        "create", help="Create an agent-preinstalled sandbox without attaching."
    )
    _add_start_options(create)
    create.set_defaults(func=cmd_create)

    recreate = sub.add_parser("recreate", help="Delete a sandbox, then create it again.")
    recreate.add_argument("--force", action="store_true", help="Do not prompt for confirmation.")
    _add_start_options(recreate)
    recreate.set_defaults(func=cmd_recreate)

    for remove_name in ("remove", "rm"):
        remove = sub.add_parser(remove_name, help="Remove a sandbox.")
        remove.add_argument("name", nargs="?", help="Sandbox name. Defaults to [sbx].name.")
        remove.add_argument("--force", action="store_true", help="Do not prompt for confirmation.")
        remove.set_defaults(func=cmd_remove)

    stop = sub.add_parser("stop", help="Stop a sandbox.")
    stop.add_argument("name", nargs="?", help="Sandbox name. Defaults to [sbx].name.")
    stop.set_defaults(func=cmd_stop)

    shell = sub.add_parser("shell", help="Open a shell in a sandbox.")
    shell.add_argument(
        "--keep-running",
        action="store_true",
        help="Keep the VM running after this shell exits.",
    )
    shell.add_argument(
        "--run-user",
        help="Create/use this guest user for the shell (default: [sbx].run_user).",
    )
    shell.add_argument(
        "--project-path",
        help="Start the shell in this mounted project path (default: [sbx].project_path).",
    )
    git_config_shell = shell.add_mutually_exclusive_group()
    git_config_shell.add_argument(
        "--git-config",
        dest="git_config",
        action="store_true",
        default=None,
        help="Copy safe host Git identity/config into the guest (default).",
    )
    git_config_shell.add_argument(
        "--no-git-config",
        dest="git_config",
        action="store_false",
        help="Do not copy host Git identity/config into the guest.",
    )
    shell.add_argument(
        "--root",
        action="store_true",
        help="Open the shell as root, ignoring [sbx].run_user.",
    )
    shell.add_argument("name", nargs="?", help="Sandbox name. Defaults to [sbx].name.")
    shell.set_defaults(func=cmd_shell)

    for list_name in ("list", "ls"):
        list_parser = sub.add_parser(list_name, help="List sandboxes.")
        list_parser.add_argument(
            "-a",
            "--all",
            action="store_true",
            help="List all sandboxes, including stopped ones.",
        )
        list_parser.set_defaults(func=cmd_list)

    network_parser = sub.add_parser("network", help="Expert networking helpers.")
    network_sub = network_parser.add_subparsers(dest="network_action", required=True)
    forward = network_sub.add_parser(
        "forward",
        help="Forward a host TCP port to a running sandbox until Ctrl-C.",
    )
    forward.add_argument(
        "forward_args",
        nargs="+",
        metavar="[NAME] SPEC...",
        help="Forward specs: GUEST_PORT, HOST_PORT:GUEST_PORT, or BIND_HOST:HOST_PORT:GUEST_PORT.",
    )
    forward.set_defaults(func=network.cmd_forward)

    auth_port = network_sub.add_parser(
        "auth-port",
        help="Expose the Pi OAuth callback port from a sandbox to host localhost.",
    )
    auth_port.add_argument("name", nargs="?", help="Sandbox name or ID. Defaults to [sbx].name.")
    auth_port.add_argument(
        "--guest-port",
        type=int,
        default=1455,
        help="Guest callback port opened by the agent (default: 1455).",
    )
    auth_port.add_argument(
        "--host-port",
        type=int,
        default=1455,
        help="Host localhost port used by the browser redirect (default: 1455).",
    )
    auth_port.add_argument(
        "--replace",
        action="store_true",
        help="Close an existing sbx-tracked auth tunnel on this host port before exposing it.",
    )
    auth_port.set_defaults(func=network.cmd_auth_port)

    close_auth_port = network_sub.add_parser(
        "close-auth-port",
        help="Close the tracked Pi OAuth callback port tunnel for a sandbox.",
    )
    close_auth_port.add_argument(
        "name", nargs="?", help="Sandbox name or ID. Defaults to [sbx].name."
    )
    close_auth_port.set_defaults(func=network.cmd_close_auth_port)

    network_status = network_sub.add_parser(
        "status",
        help="Show sandbox networking and auth callback tunnel status.",
    )
    network_status.add_argument(
        "name", nargs="?", help="Sandbox name or ID. Defaults to [sbx].name."
    )
    network_status.add_argument(
        "--host-port",
        type=int,
        default=1455,
        help="Host auth callback port to inspect (default: 1455).",
    )
    network_status.set_defaults(func=network.cmd_status)

    image = sub.add_parser("image", help="Advanced local image helpers.")
    image_sub = image.add_subparsers(dest="image_action", required=True)
    build_debian_parser = image_sub.add_parser(
        "build-debian", help="Build a local Debian Pi image for sbx."
    )
    build_debian.add_arguments(build_debian_parser)
    build_debian_parser.set_defaults(func=cmd_image_build_debian)

    list_images_parser = image_sub.add_parser("ls", help="List local sbx images.")
    sbx.image.ls.add_arguments(list_images_parser)
    list_images_parser.set_defaults(func=cmd_image_ls)

    doctor = sub.add_parser("doctor", help="Run non-sudo diagnostics for the configured backend.")
    doctor.add_argument(
        "--fix",
        action="store_true",
        help="Repair safe local sbx/SmolVM bookkeeping issues found by doctor.",
    )
    doctor.set_defaults(func=cmd_doctor)

    completion = sub.add_parser("completion", help="Generate shell completion script.")
    completion.add_argument("shell", choices=SUPPORTED_SHELLS)
    completion.set_defaults(func=cmd_completion)

    return parser


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    """Normalize `sbx run NAME --flag` to `sbx run --name NAME --flag`."""
    normalized = list(argv)
    for command in ("run", "create", "recreate"):
        if command not in normalized:
            continue
        command_index = normalized.index(command)
        name_index = command_index + 1
        if name_index >= len(normalized):
            return normalized
        candidate = normalized[name_index]
        if candidate != "--" and not candidate.startswith("-"):
            normalized.insert(name_index, "--name")
        return normalized
    return normalized


def main(argv: Sequence[str] | None = None) -> int:
    global DEBUG

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    normalized_argv = _normalize_argv(raw_argv)
    parser = build_parser()
    args = parser.parse_args(normalized_argv)
    DEBUG = bool(args.debug)
    _debug(f"argv: {raw_argv}")
    if normalized_argv != raw_argv:
        _debug(f"normalized argv: {normalized_argv}")
    if args.action in {"completion", "image"}:
        args.config_data = {}
    else:
        try:
            args.config_data = load_config(args.config)
        except ConfigError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Shell convention: signal exits are 128 + signal number.
        return 128 + signal.SIGINT
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
