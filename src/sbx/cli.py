from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sbx.image.ls
from sbx import __version__
from sbx.completion import SUPPORTED_SHELLS, completion_script
from sbx.image import build_debian

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


AGENTS = ("pi", "claude", "codex")
DEFAULT_BACKEND = "qemu"
DEFAULT_BOOT_TIMEOUT = 30.0
MIB = 1024 * 1024
LAUNCH_COMMANDS = {"pi": "pi", "claude": "claude", "codex": "codex"}
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
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
SBX_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "sbx"
TUNNELS_FILE = SBX_STATE_DIR / "tunnels.json"
SESSIONS_FILE = SBX_STATE_DIR / "sessions.json"
SMOLVM_DB_PATH = Path.home() / ".local" / "state" / "smolvm" / "smolvm.db"
DEBUG = False


class ConfigError(ValueError):
    pass


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
    return _run(_smolvm_argv(args), **kwargs)


def _run_smolvm_capture(
    args: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str] | None:
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


def _sync_existing_vm_mounts_from_config(
    vm_name: str, mounts: Sequence[str], *, writable_mounts: bool
) -> None:
    db_path = SMOLVM_DB_PATH.expanduser()
    if not db_path.exists():
        return

    desired = _workspace_mounts_from_specs(mounts, writable=writable_mounts)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, config FROM vms WHERE id = ?", (vm_name,)).fetchone()
        if row is None or row["status"] == "running":
            return
        config = json.loads(row["config"])
        if config.get("workspace_mounts") == desired:
            return
        config["workspace_mounts"] = desired
        conn.execute(
            "UPDATE vms SET config = ? WHERE id = ?",
            (json.dumps(config, separators=(",", ":")), vm_name),
        )
    print(f"sbx: updated mounts for existing VM '{vm_name}'")


def _project_guest_cwd(path_value: Any) -> str | None:
    if path_value is None:
        return None
    return str(_resolve_project_path(str(path_value)))


def _validate_run_user(user: str) -> str:
    if not USERNAME_RE.match(user):
        raise ConfigError("[sbx].run_user must be a valid Linux user name")
    return user


def _validate_env_names(names: list[str]) -> list[str]:
    invalid = [name for name in names if not ENV_NAME_RE.match(name)]
    if invalid:
        raise ConfigError(f"invalid env var name(s): {', '.join(invalid)}")
    return names


def _sanitize_forwarded_env(env: dict[str, str], allowed: list[str]) -> dict[str, str]:
    allowed_set = set(allowed)
    for key in FORWARDABLE_ENV_VARS:
        if key not in allowed_set:
            env.pop(key, None)
    return env


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


def _credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
    """Return an environment that prevents SmolVM presets from seeing host credentials."""
    env = _sanitize_forwarded_env(dict(os.environ), forward_env)
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
    _debug(
        "credential isolation: "
        f"real_cache={real_smolvm_cache}, real_data={real_smolvm_data}, temp_home={temp_home}"
    )
    return env


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


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ceil_mib(size_bytes: int) -> int:
    return (size_bytes + MIB - 1) // MIB


def _qcow2_virtual_size_mib(path: Path) -> int | None:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        return None
    result = subprocess.run(
        [qemu_img, "info", "--output=json", str(path)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
        virtual_size = int(info["virtual-size"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return _ceil_mib(virtual_size)


def _rootfs_size_mib(rootfs_path: Path) -> int | None:
    if rootfs_path.suffix.lower() == ".qcow2":
        return _qcow2_virtual_size_mib(rootfs_path)
    try:
        return _ceil_mib(rootfs_path.stat().st_size)
    except OSError:
        return None


def _local_image_rootfs_size_mib(config: Mapping[str, Any]) -> int | None:
    image = _path_from_config(_cfg(config, "sbx", "image"))
    if image is None:
        return None
    try:
        manifest = _local_image_manifest(image)
        rootfs_path = _manifest_path(image, manifest, "rootfs")
        if not rootfs_path.is_file():
            return None
    except ConfigError:
        return None
    return _rootfs_size_mib(rootfs_path)


def _local_image_disk_size_error(configured_disk_size: int, image_size: int) -> str:
    return (
        "configured disk_size is smaller than the local image rootfs:\n"
        f"  disk_size: {configured_disk_size} MiB\n"
        f"  local image rootfs: {image_size} MiB\n"
        f"Set [sbx].disk_size to at least {image_size}, remove [sbx].disk_size, "
        "or rebuild the configured local image with a rootfs no larger than "
        f"{configured_disk_size} MiB."
    )


def _local_image_config_warnings(config: Mapping[str, Any]) -> list[str]:
    configured_disk_size = _int_or_none(_cfg(config, "sbx", "disk_size"))
    if configured_disk_size is None:
        return []
    image_size = _local_image_rootfs_size_mib(config)
    if image_size is None or configured_disk_size >= image_size:
        return []
    return [_local_image_disk_size_error(configured_disk_size, image_size)]


def _existing_vm_config_mismatches(name: str, config: Mapping[str, Any]) -> list[str]:
    vm = _smolvm_info_vm(name)
    if vm is None:
        return []

    checks = (
        ("disk_size", "disk_size", " MiB"),
        ("memory", "memory", " MiB"),
        ("cpus", "vcpus", ""),
    )
    mismatches: list[str] = []
    for config_key, vm_key, unit in checks:
        configured = _int_or_none(_cfg(config, "sbx", config_key))
        existing = _int_or_none(vm.get(vm_key))
        if configured is None or existing is None or configured == existing:
            continue
        mismatches.append(
            f"{config_key}: config requests {configured}{unit}, "
            f"existing VM has {existing}{unit}"
        )
    return mismatches


def _doctor_config_state(config: Mapping[str, Any]) -> None:
    name = _cfg(config, "sbx", "name")
    if not name:
        return

    vm_name = str(name)
    mismatches = _existing_vm_config_mismatches(vm_name, config)
    image_warnings = _local_image_config_warnings(config)
    if not mismatches and not image_warnings:
        return

    print("sbx config/state:")
    if mismatches:
        print(f"  warning: VM '{vm_name}' already exists and differs from .sbx.toml:")
        for mismatch in mismatches:
            print(f"    {mismatch}")
    if image_warnings:
        print("  warning: configured disk_size is smaller than the local image rootfs:")
        for warning in image_warnings:
            for line in warning.splitlines():
                print(f"    {line}")
    if mismatches:
        print(
            "  Existing VMs are reused as-is. "
            f"Run `sbx recreate {vm_name} --force` to apply config changes."
        )


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
        print(
            f"sbx: VM '{vm_id}' is in error state; run `sbx recreate {vm_id} --force` "
            "to delete it and create a fresh VM.",
            file=sys.stderr,
        )
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


def _host_git_config() -> str | None:
    values: dict[str, str] = {}
    for key in SAFE_GIT_CONFIG_KEYS:
        try:
            completed = subprocess.run(
                ["git", "config", "--global", "--get", key],
                check=False,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError:
            _debug("git not found; skipping git config forwarding")
            return None
        if completed.returncode == 0:
            value = completed.stdout.strip()
            if value and "\n" not in value:
                values[key] = value
    if not values:
        _debug("no safe global git config values found to forward")
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


def _install_git_config(vm_id: str, user: str | None, git_config_text: str | None) -> None:
    if not git_config_text:
        return
    if user is None:
        home = "/root"
        owner = "root:root"
    else:
        quoted_user = shlex.quote(user)
        home = f"/home/{quoted_user}"
        owner = f"{quoted_user}:{quoted_user}"

    encoded = base64.b64encode(git_config_text.encode("utf-8")).decode("ascii")
    script = f"""
set -eu
install -d {shlex.quote(home)}
printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(home)}/.gitconfig
chown {owner} {shlex.quote(home)}/.gitconfig
chmod 600 {shlex.quote(home)}/.gitconfig
"""
    cmd = _ssh_command(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = _run_capture(cmd)
    if completed is None:
        raise ConfigError("failed to install git config: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to install git config: {stderr}")


def _prepare_run_user(vm_id: str, user: str) -> None:
    quoted_user = shlex.quote(user)
    home = f"/home/{quoted_user}"
    script = f"""
set -eu
host="$(hostname)"
if [ -n "$host" ] && ! getent hosts "$host" >/dev/null 2>&1; then
  printf '127.0.1.1 %s\n' "$host" >> /etc/hosts
fi
if ! id -u {quoted_user} >/dev/null 2>&1; then
  useradd -m -s /bin/bash {quoted_user}
fi
install -d -o {quoted_user} -g {quoted_user} {home}
for p in .ssh .pi .codex .claude .claude.json; do
  if [ -e /root/$p ]; then
    rm -rf {home}/$p
    cp -a /root/$p {home}/$p
  fi
done
chown -R {quoted_user}:{quoted_user} {home}
"""
    cmd = _ssh_command(vm_id)
    cmd.append("bash -lc " + shlex.quote(script))
    completed = _run_capture(cmd)
    if completed is None:
        raise ConfigError(f"failed to prepare run user {user!r}: ssh command not found")
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigError(f"failed to prepare run user {user!r}: {stderr}")


def _missing_vm_message(vm_id: str) -> str:
    return (
        f"VM {vm_id!r} not found. `sbx shell` attaches to an existing sandbox; "
        f"create it with `sbx run {vm_id}` or list VMs with `sbx ls -a`."
    )


def _ssh_command(vm_id: str) -> list[str]:
    from smolvm.exceptions import VMNotFoundError
    from smolvm.facade import SmolVM

    try:
        vm = SmolVM.from_id(vm_id)
    except VMNotFoundError as exc:
        raise ConfigError(_missing_vm_message(vm_id)) from exc
    try:
        return list(vm._ssh_direct_command())
    finally:
        vm.close()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_object(path: Path, data: Mapping[str, Any]) -> None:
    SBX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_tunnels() -> dict[str, Any]:
    return _read_json_object(TUNNELS_FILE)


def _save_tunnels(data: Mapping[str, Any]) -> None:
    _write_json_object(TUNNELS_FILE, data)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tracked_auth_tunnel(vm_id: str) -> dict[str, Any] | None:
    tunnel = _load_tunnels().get(vm_id, {}).get("auth_port")
    if not isinstance(tunnel, dict):
        return None
    pid = tunnel.get("pid")
    if not isinstance(pid, int) or not _pid_is_alive(pid):
        return None
    return tunnel


def _tracked_auth_tunnel_for_host_port(host_port: int) -> tuple[str, dict[str, Any]] | None:
    for vm_id, vm_data in _load_tunnels().items():
        if not isinstance(vm_data, dict):
            continue
        tunnel = vm_data.get("auth_port")
        if not isinstance(tunnel, dict) or tunnel.get("host_port") != host_port:
            continue
        pid = tunnel.get("pid")
        if isinstance(pid, int) and _pid_is_alive(pid):
            return str(vm_id), tunnel
    return None


def _record_auth_tunnel(vm_id: str, *, pid: int, host_port: int, guest_port: int) -> None:
    data = _load_tunnels()
    vm_data = data.setdefault(vm_id, {})
    vm_data["auth_port"] = {"pid": pid, "host_port": host_port, "guest_port": guest_port}
    _save_tunnels(data)


def _remove_auth_tunnel_record(vm_id: str) -> None:
    data = _load_tunnels()
    vm_data = data.get(vm_id)
    if isinstance(vm_data, dict):
        vm_data.pop("auth_port", None)
        if not vm_data:
            data.pop(vm_id, None)
    _save_tunnels(data)


def _load_sessions() -> dict[str, Any]:
    return _read_json_object(SESSIONS_FILE)


def _save_sessions(data: Mapping[str, Any]) -> None:
    _write_json_object(SESSIONS_FILE, data)


def _active_sessions(vm_id: str) -> list[dict[str, Any]]:
    data = _load_sessions()
    raw_sessions = data.get(vm_id, {}).get("sessions", [])
    sessions = [item for item in raw_sessions if isinstance(item, dict)]
    active = [
        item
        for item in sessions
        if isinstance(item.get("pid"), int) and _pid_is_alive(int(item["pid"]))
    ]
    if active != sessions:
        if active:
            data.setdefault(vm_id, {})["sessions"] = active
        else:
            data.pop(vm_id, None)
        _save_sessions(data)
    return active


def _register_session(vm_id: str, kind: str) -> None:
    data = _load_sessions()
    sessions = _active_sessions(vm_id)
    sessions.append({"pid": os.getpid(), "kind": kind})
    data = _load_sessions()
    data.setdefault(vm_id, {})["sessions"] = sessions
    _save_sessions(data)
    _debug(f"registered {kind} session for {vm_id} with pid {os.getpid()}")


def _unregister_session(vm_id: str) -> None:
    data = _load_sessions()
    sessions = data.get(vm_id, {}).get("sessions", [])
    remaining = [
        item
        for item in sessions
        if isinstance(item, dict)
        and item.get("pid") != os.getpid()
        and isinstance(item.get("pid"), int)
        and _pid_is_alive(int(item["pid"]))
    ]
    if remaining:
        data.setdefault(vm_id, {})["sessions"] = remaining
    else:
        data.pop(vm_id, None)
    _save_sessions(data)
    _debug(f"unregistered session for {vm_id}; remaining={len(remaining)}")


def _stop_vm_if_last_session(vm_id: str, *, stop_on_exit: bool) -> None:
    if not stop_on_exit:
        _debug(f"not stopping {vm_id}: stop_on_exit disabled")
        return
    if _active_sessions(vm_id):
        _debug(f"not stopping {vm_id}: other sbx sessions still active")
        return
    _debug(f"stopping {vm_id}: no other sbx sessions active")
    _run_smolvm(["sandbox", "stop", vm_id])


def _localhost_port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _close_tracked_auth_tunnel(vm_id: str) -> bool:
    tracked = _tracked_auth_tunnel(vm_id)
    if tracked is None:
        _remove_auth_tunnel_record(vm_id)
        return False

    pid = int(tracked["pid"])
    with suppress_process_errors():
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _pid_is_alive(pid):
        time.sleep(0.1)
    if _pid_is_alive(pid):
        with suppress_process_errors():
            os.killpg(pid, signal.SIGKILL)
    _remove_auth_tunnel_record(vm_id)
    return True


def _expose_auth_port(vm_id: str, host_port: int, guest_port: int, *, replace: bool = False) -> int:
    _debug(f"expose auth port: vm={vm_id}, host_port={host_port}, guest_port={guest_port}")
    tracked = _tracked_auth_tunnel(vm_id)
    if (
        tracked
        and tracked.get("host_port") == host_port
        and tracked.get("guest_port") == guest_port
    ):
        _debug(f"auth host port {host_port} already tracked with pid {tracked['pid']}")
        return 0
    if _localhost_port_is_listening(host_port):
        owner = _tracked_auth_tunnel_for_host_port(host_port)
        if owner is not None:
            owner_vm, _owner_tunnel = owner
            if replace:
                print(
                    f"sbx: replacing auth tunnel on localhost:{host_port}: "
                    f"VM '{owner_vm}' -> VM '{vm_id}'.",
                    file=sys.stderr,
                )
                _close_tracked_auth_tunnel(owner_vm)
            else:
                print(
                    f"sbx: warning: localhost:{host_port} is already used by the auth "
                    f"tunnel for VM '{owner_vm}'. `/login` in VM '{vm_id}' may not work; "
                    f"run `sbx network auth-port {vm_id} --replace` to switch it.",
                    file=sys.stderr,
                )
                return 0
        else:
            print(
                f"sbx: warning: localhost:{host_port} is already in use and is not tracked "
                "by sbx. `/login` may not work until that port is free.",
                file=sys.stderr,
            )
            return 0

    cmd = _ssh_command(vm_id)
    forward_args = [
        "-N",
        "-L",
        f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
    ]
    cmd[-1:-1] = forward_args
    _debug_command(cmd, None)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("sbx: command not found: ssh", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"sbx: failed to start auth port tunnel: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
            print(f"sbx: auth port tunnel exited before becoming ready: {stderr}", file=sys.stderr)
            return proc.returncode or 1
        if _localhost_port_is_listening(host_port):
            _record_auth_tunnel(vm_id, pid=proc.pid, host_port=host_port, guest_port=guest_port)
            _debug(f"auth port tunnel ready with pid {proc.pid}")
            return 0
        time.sleep(0.1)

    with suppress_process_errors():
        os.killpg(proc.pid, signal.SIGTERM)
    print(f"sbx: auth port tunnel did not become ready on localhost:{host_port}", file=sys.stderr)
    return 1


class suppress_process_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, (ProcessLookupError, PermissionError, OSError))


def _attach_as_root(vm_id: str, launch_command: str, cwd: str | None = None) -> int:
    from smolvm.env import ENV_FILE

    cmd = _ssh_command(vm_id)

    cd_prefix = f"cd {shlex.quote(cwd)} || exit; " if cwd is not None else ""
    remote = (
        f"{cd_prefix}[ -r {ENV_FILE} ] && . {ENV_FILE}; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"exec {launch_command}"
    )
    cmd.insert(-1, "-t")
    cmd.append(remote)
    return _run(cmd)


def _attach_as_user(vm_id: str, user: str, launch_command: str, cwd: str | None = None) -> int:
    from smolvm.env import ENV_FILE

    cmd = _ssh_command(vm_id)

    quoted_user = shlex.quote(user)
    cd_prefix = f"cd {shlex.quote(cwd)} || exit; " if cwd is not None else ""
    remote = f"sudo -iu {quoted_user} bash -lc " + shlex.quote(
        f"{cd_prefix}[ -r {ENV_FILE} ] && . {ENV_FILE}; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"exec {launch_command}"
    )
    cmd.insert(-1, "-t")
    cmd.append(remote)
    return _run(cmd)


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
    if auth_port:
        port_rc = _expose_auth_port(vm_name, auth_host_port, auth_guest_port)
        if port_rc != 0:
            return port_rc
    if not attach:
        return 0
    command = launch_command or LAUNCH_COMMANDS[agent]
    _register_session(vm_name, "run")
    try:
        if run_user is not None:
            _prepare_run_user(vm_name, run_user)
            _install_git_config(vm_name, run_user, git_config_text)
            return _attach_as_user(vm_name, run_user, command, cwd=cwd)
        _install_git_config(vm_name, None, git_config_text)
        return _attach_as_root(vm_name, command, cwd=cwd)
    finally:
        _unregister_session(vm_name)
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


def _path_from_config(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(str(value)).expanduser()


def _local_image_manifest(image: Path) -> dict[str, Any]:
    if not image.is_dir():
        raise ConfigError("[sbx].image must point to a local image directory")
    manifest_path = image / "smolvm-image.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"image manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid image manifest JSON: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ConfigError("image manifest must be a JSON object")
    return manifest


def _manifest_path(image_dir: Path, manifest: Mapping[str, Any], key: str) -> Path:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"image manifest requires string field {key!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = image_dir / path
    return path


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
    cpus: int,
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
) -> int:
    from smolvm import SmolVM
    from smolvm.facade import _build_auto_config
    from smolvm.presets import apply_preset, get_preset

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
        config_obj = config_obj.model_copy(update={"vcpu_count": cpus})
        try:
            vm = SmolVM(
                config_obj,
                ssh_key_path=ssh_key_path,
                mounts=list(mounts),
                writable_mounts=writable_mounts,
            )
            vm.start(boot_timeout=boot_timeout)
            vm.wait_for_ssh(timeout=boot_timeout)
            ssh = vm._ensure_ssh_for_env()
            apply_preset(ssh, preset, install_timeout=install_timeout)
            return str(vm.vm_id)
        finally:
            if vm is not None:
                vm.close()

    env = _sanitize_forwarded_env(dict(os.environ), forward_env)
    temp_home_ctx = None
    if not copy_host_credentials:
        temp_home_ctx = tempfile.TemporaryDirectory(prefix="sbx-no-credentials-")
        env = _credential_free_env(Path(temp_home_ctx.name), forward_env=forward_env)
        _debug(f"credential-free HOME: {temp_home_ctx.name}")

    try:
        with _patched_environ(env):
            vm_name = start()
    finally:
        if temp_home_ctx is not None:
            temp_home_ctx.cleanup()

    _maybe_write_project_config(args, config, vm_name=vm_name, agent=agent, created=True)

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

    kernel_path = _manifest_path(image_dir, manifest, "kernel")
    rootfs_path = _manifest_path(image_dir, manifest, "rootfs")
    if not kernel_path.is_file():
        raise ConfigError(f"image kernel not found: {kernel_path}")
    if not rootfs_path.is_file():
        raise ConfigError(f"image rootfs not found: {rootfs_path}")

    initrd_value = manifest.get("initrd")
    initrd_path = None
    if initrd_value is not None:
        initrd_path = _manifest_path(image_dir, manifest, "initrd")
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
    }
    if cpus_value is not None:
        vm_config["vcpu_count"] = _validate_cpus(cpus_value)
    if disk_size is not None:
        disk_size_mib = int(disk_size)
        image_size_mib = _rootfs_size_mib(rootfs_path)
        if image_size_mib is not None and disk_size_mib < image_size_mib:
            raise ConfigError(_local_image_disk_size_error(disk_size_mib, image_size_mib))
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
    except Exception:
        vm.close()
        raise
    vm.close()

    _maybe_write_project_config(args, config, vm_name=str(vm_name), agent=agent, created=True)

    if attach:
        print(f"Started '{vm_name}'. Launching {agent}...")
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
    _doctor_config_state(getattr(args, "config_data", {}))
    return rc


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
    forward_env = _validate_env_names(
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
    git_config_text = _host_git_config() if git_config else None
    cpus_value = _arg_or_config(args, "cpus", config, "sbx")
    cpus = _validate_cpus(cpus_value) if cpus_value is not None else None

    requested_name = _arg_or_config(args, "name", config, "sbx")
    if requested_name:
        existing_status = _get_existing_vm_status(str(requested_name))
        _debug(f"existing VM lookup: name={requested_name!r}, status={existing_status!r}")
        if existing_status is not None:
            if existing_status != "running":
                try:
                    _sync_existing_vm_mounts_from_config(
                        str(requested_name), effective_mounts, writable_mounts=writable_mounts
                    )
                except ConfigError as exc:
                    print(f"sbx: {exc}", file=sys.stderr)
                    return 2
            start_rc = _start_existing_vm_if_needed(
                str(requested_name), existing_status, boot_timeout
            )
            if start_rc != 0:
                return start_rc
            _maybe_write_project_config(
                args, config, vm_name=str(requested_name), agent=str(agent), created=False
            )
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

    image = _path_from_config(_arg_or_config(args, "image", config, "sbx"))
    if image is not None:
        try:
            manifest = _local_image_manifest(image)
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

    if cpus is not None:
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
    smolvm_env = _sanitize_forwarded_env(dict(os.environ), forward_env)
    if not copy_host_credentials:
        temp_home_ctx = tempfile.TemporaryDirectory(prefix="sbx-no-credentials-")
        smolvm_env = _credential_free_env(Path(temp_home_ctx.name), forward_env=forward_env)
        _debug(f"credential-free HOME: {temp_home_ctx.name}")

    if not managed_start:
        try:
            rc = _run_smolvm(argv, env=smolvm_env)
            if rc == 0 and requested_name:
                _maybe_write_project_config(
                    args, config, vm_name=str(requested_name), agent=str(agent), created=True
                )
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
    _maybe_write_project_config(args, config, vm_name=vm_name, agent=str(agent), created=True)
    if auth_port:
        port_rc = _expose_auth_port(vm_name, auth_host_port, auth_guest_port)
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
    _register_session(vm_name, "run")
    try:
        if run_user is not None:
            _prepare_run_user(vm_name, str(run_user))
            _install_git_config(vm_name, str(run_user), git_config_text)
            return _attach_as_user(
                vm_name, str(run_user), LAUNCH_COMMANDS[str(agent)], cwd=project_guest_cwd
            )
        _install_git_config(vm_name, None, git_config_text)
        return _attach_as_root(vm_name, LAUNCH_COMMANDS[str(agent)], cwd=project_guest_cwd)
    finally:
        _unregister_session(vm_name)
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


def cmd_passthrough(args: argparse.Namespace) -> int:
    config = args.config_data
    if args.action == "ls":
        smolvm_command = ["sandbox", "list"]
        if getattr(args, "all", False):
            smolvm_command.append("--all")
    elif args.action == "shell":
        name = _vm_name_from_arg_or_config(args, config, "shell")
        if name is None:
            return 2
        run_user = None if args.root else args.run_user or _cfg(config, "sbx", "run_user")
        if run_user is not None:
            run_user = _validate_run_user(str(run_user))
        project_guest_cwd = _project_guest_cwd(
            args.project_path or _cfg(config, "sbx", "project_path")
        )
        smolvm_command = ["sandbox", "ssh", name]
        keep_running = bool(getattr(args, "keep_running", False))
        stop_on_exit = bool(_cfg(config, "sbx", "stop_on_exit", True)) and not keep_running
        git_config = bool(
            args.git_config
            if args.git_config is not None
            else _cfg(config, "sbx", "git_config", True)
        )
        git_config_text = _host_git_config() if git_config else None
        if (
            run_user is not None or project_guest_cwd is not None or git_config_text is not None
        ) and _get_existing_vm_status(name) is None:
            print(f"sbx: {_missing_vm_message(name)}", file=sys.stderr)
            return 1
        _register_session(name, "shell")
        try:
            if run_user is not None:
                _prepare_run_user(name, run_user)
                _install_git_config(name, run_user, git_config_text)
                return _attach_as_user(name, run_user, "bash", cwd=project_guest_cwd)
            if project_guest_cwd is not None or git_config_text is not None:
                _install_git_config(name, None, git_config_text)
                return _attach_as_root(name, "bash", cwd=project_guest_cwd)
            return _run_smolvm(smolvm_command)
        finally:
            _unregister_session(name)
            _stop_vm_if_last_session(name, stop_on_exit=stop_on_exit)
    elif args.action == "stop":
        name = _vm_name_from_arg_or_config(args, config, "stop")
        if name is None:
            return 2
        smolvm_command = ["sandbox", "stop", name]
    elif args.action == "rm":
        name = _vm_name_from_arg_or_config(args, config, "rm")
        if name is None:
            return 2
        force = args.force
        if not _confirm_destructive_action(f"Destroy VM '{name}'?", force=force):
            return 2
        return _delete_vm(name)
    else:  # Defensive guard; argparse should prevent this.
        print(f"sbx: unsupported passthrough command: {args.action}", file=sys.stderr)
        return 2

    return _run_smolvm(smolvm_command)


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
        print(f"Destroyed VM '{vm_id}'.")
        return 0

    if any(item.get("error") == f"VM '{vm_id}' not found" for item in failed):
        print(f"VM '{vm_id}' not found; nothing to destroy.")
        return 0

    if completed.stdout:
        print(completed.stdout, end="")
    return completed.returncode


def cmd_auth_port(args: argparse.Namespace) -> int:
    name = _vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network auth-port"
    )
    if name is None:
        return 2
    return _expose_auth_port(
        name, args.host_port, args.guest_port, replace=bool(getattr(args, "replace", False))
    )


def cmd_close_auth_port(args: argparse.Namespace) -> int:
    name = _vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network close-auth-port"
    )
    if name is None:
        return 2
    if not _close_tracked_auth_tunnel(name):
        print(f"No tracked auth port tunnel for '{name}'.")
        return 0
    print(f"Closed auth port tunnel for '{name}'.")
    return 0


def cmd_network_status(args: argparse.Namespace) -> int:
    name = _vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network status"
    )
    if name is None:
        return 2
    completed = _run_smolvm_capture(["sandbox", "info", name, "--json"])
    if completed is None:
        return 127
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        return completed.returncode

    payload = json.loads(completed.stdout)
    vm = payload["data"]["vm"]
    tracked = _tracked_auth_tunnel(name)
    auth_status = "inactive"
    auth_detail = "-"
    if tracked is not None:
        auth_status = "active"
        auth_detail = (
            f"pid {tracked['pid']}, localhost:{tracked['host_port']} -> "
            f"guest:{tracked['guest_port']}"
        )
    elif _localhost_port_is_listening(args.host_port):
        auth_status = "busy/untracked"
        auth_detail = f"localhost:{args.host_port} is listening but is not tracked by sbx"

    print(f"Sandbox: {vm['name']}")
    print(f"Status: {vm['status']}")
    print(f"Backend: {vm['backend']}")
    print(f"Guest IP: {vm['ip_address']}")
    print(f"SSH Port: {vm['ssh_port']}")
    print(f"Auth callback: {auth_status}")
    print(f"Auth detail: {auth_detail}")
    return 0


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
        help="Seconds to wait for VM boot/SSH readiness (default: [sbx].boot_timeout or 30).",
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

    rm = sub.add_parser("rm", help="Remove a sandbox.")
    rm.add_argument("name", nargs="?", help="Sandbox name. Defaults to [sbx].name.")
    rm.add_argument("--force", action="store_true", help="Do not prompt for confirmation.")
    rm.set_defaults(func=cmd_passthrough)

    stop = sub.add_parser("stop", help="Stop a sandbox.")
    stop.add_argument("name", nargs="?", help="Sandbox name. Defaults to [sbx].name.")
    stop.set_defaults(func=cmd_passthrough)

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
    shell.set_defaults(func=cmd_passthrough)

    ls_p = sub.add_parser("ls", help="List sandboxes.")
    ls_p.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="List all sandboxes, including stopped ones.",
    )
    ls_p.set_defaults(func=cmd_passthrough)

    network = sub.add_parser("network", help="Expert networking helpers.")
    network_sub = network.add_subparsers(dest="network_action", required=True)
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
    auth_port.set_defaults(func=cmd_auth_port)

    close_auth_port = network_sub.add_parser(
        "close-auth-port",
        help="Close the tracked Pi OAuth callback port tunnel for a sandbox.",
    )
    close_auth_port.add_argument(
        "name", nargs="?", help="Sandbox name or ID. Defaults to [sbx].name."
    )
    close_auth_port.set_defaults(func=cmd_close_auth_port)

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
    network_status.set_defaults(func=cmd_network_status)

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
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
