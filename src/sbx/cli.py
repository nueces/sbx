import argparse
import json
import os
import signal
import sqlite3
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import sbx.image.ls
import sbx.network as network
from sbx import (
    __version__,
    doctor,
    guest_setup,
    lifecycle_warnings,
    runtime,
    session_state,
    smolvm_preset,
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
from sbx.runtime import ConfigError

AGENTS = ("pi", "claude", "codex")
RUNTIME_INSTALLERS = ("pi", "specify")
MIB = 1024 * 1024
LAUNCH_COMMANDS = {"pi": "pi", "claude": "claude", "codex": "codex"}
DEFAULT_CONFIG_PATHS = (Path.home() / ".config" / "sbx" / "config.toml",)
LOCAL_CONFIG_PATHS = (Path.cwd() / ".sbx.toml",)


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
            runtime.debug(f"loading config: {path}")
            config = _deep_merge(config, _read_toml(path))
    runtime.debug(f"merged config: {config}")
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


def _runtime_install(args: argparse.Namespace, config: Mapping[str, Any]) -> list[str]:
    cli_value = getattr(args, "runtime_install", None)
    value = cli_value if cli_value is not None else _sbx_config(config).get("runtime_install", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("[sbx].runtime_install must be an array of strings")
    if len(value) != len(set(value)):
        raise ConfigError("runtime_install must not contain duplicates")
    unknown = [item for item in value if item not in RUNTIME_INSTALLERS]
    if unknown:
        raise ConfigError(
            f"unsupported runtime installer(s): {', '.join(unknown)}; "
            f"supported: {', '.join(RUNTIME_INSTALLERS)}"
        )
    return list(value)


def _install_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("[sbx].install_timeout must be a positive integer") from exc
    if timeout <= 0:
        raise ConfigError("[sbx].install_timeout must be a positive integer")
    return timeout


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


def _project_guest_cwd(path_value: Any) -> str | None:
    if path_value is None:
        return None
    return str(_resolve_project_path(str(path_value)))


def _sync_forwarded_env_or_error(vm_id: str, names: list[str]) -> bool:
    try:
        guest_setup.sync_forwarded_env(vm_id, names, run_capture=runtime.run_capture)
    except Exception as exc:  # noqa: BLE001 - keep CLI errors user-friendly.
        print(f"sbx: failed to sync environment for VM {vm_id!r}: {exc}", file=sys.stderr)
        return False
    return True


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
    completed = runtime.run_smolvm_capture(["sandbox", "info", vm_id, "--json"])
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
    for vm in vm_state.smolvm_vms(all_vms=not args.running):
        name = str(getattr(vm, "vm_id", "-"))
        status = getattr(getattr(vm, "status", "-"), "value", getattr(vm, "status", "-"))
        rootfs = getattr(getattr(vm, "config", None), "rootfs_path", None)
        image_path = Path(rootfs) if rootfs is not None else None
        rows.append(
            {
                "name": name,
                "status": str(status),
                "project": metadata.get(name, {}).get("project_root"),
                "image": (
                    image_path.parent.name or image_path.name if image_path is not None else None
                ),
                "ssh_port": getattr(getattr(vm, "network", None), "ssh_host_port", None),
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print_table(
            ("NAME", "STATUS", "PROJECT", "IMAGE", "SSH"),
            [
                [
                    row["name"],
                    row["status"],
                    row["project"] or "-",
                    row["image"] or "-",
                    str(row["ssh_port"]) if row["ssh_port"] is not None else "-",
                ]
                for row in rows
            ],
        )
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


def _start_existing_vm_if_needed(
    vm_id: str, status: str, boot_timeout: float, *, json_output: bool = False
) -> int:
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
    current = vm_state.existing_vm_start_config(vm_id)
    if current is not None:
        config = current[1]
        for key in ("kernel_path", "rootfs_path"):
            value = config.get(key)
            if value is not None and not Path(str(value)).expanduser().exists():
                print(
                    f"sbx: VM '{vm_id}' references missing boot artifact: {value}",
                    file=sys.stderr,
                )
                print(
                    "sbx: This VM was likely created from a temporary or removed image.",
                    file=sys.stderr,
                )
                print(f"sbx: Run `sbx recreate {vm_id} --force`.", file=sys.stderr)
                return 1
    command = ["sandbox", "start", vm_id, "--boot-timeout", f"{boot_timeout:g}"]
    completed = runtime.run_smolvm_capture(command)
    if completed is None:
        return 127
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.stdout and (not json_output or completed.returncode != 0):
        print(completed.stdout, end="", file=sys.stderr if json_output else sys.stdout)
    rc = completed.returncode
    if rc != 0:
        _maybe_print_boot_timeout_running_hint(vm_id, boot_timeout)
    return rc


def _stop_vm_if_last_session(vm_id: str, *, stop_on_exit: bool) -> None:
    if not stop_on_exit:
        runtime.debug(f"not stopping {vm_id}: stop_on_exit disabled")
        return
    if session_state.active_sessions(vm_id):
        runtime.debug(f"not stopping {vm_id}: other sbx sessions still active")
        return
    runtime.debug(f"stopping {vm_id}: no other sbx sessions active")
    runtime.run_smolvm(["sandbox", "stop", vm_id])


def _warn_initial_state_copy(vm_name: str) -> None:
    print(
        f"sbx: warning: [sbx].copy_host_credentials=true; host agent directories "
        f"for VM {vm_name!r}, including tokens, settings, and sessions, may be copied.",
        file=sys.stderr,
    )


def _print_provisioning_failure(vm_name: str, error: Exception, *, was_provisioned: bool) -> None:
    print(f"sbx: provisioning VM {vm_name!r} failed: {error}", file=sys.stderr)
    print(f"sbx: VM {vm_name!r} is still running.", file=sys.stderr)
    if was_provisioned:
        print("sbx: Its previous successful provisioning state remains recorded.", file=sys.stderr)
    print(f"sbx: Inspect: sbx shell {vm_name} --root", file=sys.stderr)
    print(f"sbx: Retry:   sbx run {vm_name} --reprovision --no-attach", file=sys.stderr)
    print(f"sbx: Stop:    sbx stop {vm_name}", file=sys.stderr)


def _post_start_actions(
    *,
    vm_name: str,
    started_now: bool,
    command: str,
    attach: bool,
    run_user: str | None,
    auth_port: bool,
    auth_host_port: int = 0,
    auth_guest_port: int = 0,
    stop_on_exit: bool,
    cwd: str | None = None,
    git_config_text: str | None = None,
    session_kind: str = "run",
    forward_env: list[str] | None = None,
    provision: bool = False,
    was_provisioned: bool = False,
    runtime_install: list[str] | None = None,
    install_timeout: int = 600,
    copy_initial_state: bool = False,
    validate_agent: bool = True,
    agent_name: str | None = None,
    diagnostic_root: bool = False,
) -> int:
    metadata = vm_metadata.load_vm(vm_name)
    runtime_user = metadata.get("run_user") if metadata else None
    if not isinstance(runtime_user, str):
        raise ConfigError(f"VM {vm_name!r} has no trusted sbx runtime identity")
    runtime_user = guest_setup.validate_run_user(runtime_user)
    if run_user is not None and run_user != runtime_user:
        raise ConfigError(f"VM {vm_name!r} is provisioned for {runtime_user!r}, not {run_user!r}")
    needs_boot_setup = started_now or provision

    try:
        if needs_boot_setup:
            for action in (guest_setup.set_hostname, guest_setup.sync_guest_clock):
                try:
                    action(vm_name, run_capture=runtime.run_capture)
                except ConfigError as exc:
                    if not diagnostic_root:
                        raise
                    print(f"sbx: warning: {exc}", file=sys.stderr)
        if not _sync_forwarded_env_or_error(vm_name, forward_env or []):
            return 1
        if auth_port:
            port_rc = network.expose_auth_port(vm_name, auth_host_port, auth_guest_port)
            if port_rc != 0:
                return port_rc
        if validate_agent and not provision and not metadata.get("provisioned"):
            print(
                f"sbx: VM {vm_name!r} has not completed provisioning; "
                f"run `sbx run {vm_name} --reprovision --no-attach`",
                file=sys.stderr,
            )
            return 1

        if provision:
            guest_setup.prepare_run_user(
                vm_name,
                runtime_user,
                copy_initial_state=copy_initial_state and not was_provisioned,
                run_capture=runtime.run_capture,
            )
            if runtime_user != "root":
                guest_setup.ensure_kvm_access(
                    vm_name, runtime_user, run_capture=runtime.run_capture
                )
            guest_setup.install_runtime_tools(
                vm_name,
                runtime_user,
                runtime_install or [],
                install_timeout,
                run_capture=runtime.run_capture,
            )
            if not was_provisioned:
                guest_setup.install_git_config(
                    vm_name, runtime_user, git_config_text, run_capture=runtime.run_capture
                )
            if runtime_user != "root":
                guest_setup.ensure_rootless_docker(
                    vm_name, runtime_user, run_capture=runtime.run_capture
                )
            vm_metadata.mark_provisioned(vm_name)
        elif needs_boot_setup and runtime_user != "root":
            for action in (guest_setup.ensure_kvm_access, guest_setup.ensure_rootless_docker):
                try:
                    action(vm_name, runtime_user, run_capture=runtime.run_capture)
                except ConfigError as exc:
                    if not diagnostic_root:
                        raise
                    print(f"sbx: warning: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - preserve provisioning diagnostics.
        if provision:
            _print_provisioning_failure(vm_name, exc, was_provisioned=was_provisioned)
        elif isinstance(exc, ConfigError):
            print(f"sbx: runtime startup check failed for VM {vm_name!r}: {exc}", file=sys.stderr)
            print(f"sbx: VM {vm_name!r} is still running.", file=sys.stderr)
            print(f"sbx: Inspect: sbx shell {vm_name} --root", file=sys.stderr)
            print(f"sbx: Retry:   sbx run {vm_name} --reprovision --no-attach", file=sys.stderr)
            print(f"sbx: Or stop/start: sbx stop {vm_name}; sbx run {vm_name}", file=sys.stderr)
        else:
            raise
        return 1
    # shortcut: running VMs skip failed boot work; use --reprovision to retry it.

    resolved_command = command
    if validate_agent:
        try:
            resolved = guest_setup.command_path(
                vm_name, run_user or "root", command, run_capture=runtime.run_capture
            )
        except ConfigError as exc:
            print(f"sbx: failed to check agent {agent_name or command!r}: {exc}", file=sys.stderr)
            return 1
        if resolved is None:
            print(
                f"sbx: agent {agent_name or command!r} was not found for user "
                f"{run_user or 'root'!r}; request its runtime installer or install it manually",
                file=sys.stderr,
            )
            return 1
        resolved_command = resolved

    if not attach:
        return 0
    session_state.register_session(vm_name, session_kind)
    try:
        rc = guest_setup.attach(vm_name, resolved_command, user=run_user, cwd=cwd, run=runtime.run)
        if rc != 0 and validate_agent:
            print(
                f"sbx: agent {agent_name or command!r} at {resolved_command!r} "
                f"exited with status {rc}",
                file=sys.stderr,
            )
        return rc
    finally:
        remaining = session_state.unregister_session(vm_name)
        runtime.debug(f"unregistered session for {vm_name}; remaining={remaining}")
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


def _warn_obsolete_manifest_fields(manifest: Mapping[str, Any]) -> None:
    obsolete = [key for key in ("agent", "launch_command") if key in _manifest_sbx(manifest)]
    if obsolete:
        fields = ", ".join(f"sbx.{key}" for key in obsolete)
        print(
            f"sbx: warning: obsolete image manifest field(s) {fields} are ignored; "
            "remove them from the manifest or rebuild the image",
            file=sys.stderr,
        )


def _manifest_run_user(manifest: Mapping[str, Any]) -> str | None:
    value = _manifest_sbx(manifest).get("run_user")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("image manifest field 'sbx.run_user' must be a string")
    return guest_setup.validate_run_user(value)


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
    if getattr(args, "runtime_install", None) is not None:
        values["runtime_install"] = list(args.runtime_install)
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
) -> bool:
    if getattr(args, "action", None) not in {"run", "create"}:
        return False
    write_config = getattr(args, "write_config", None)
    path = _project_config_path()
    exists = path.exists()
    if exists and write_config is not True:
        return False
    if not exists and not created and write_config is not True:
        return False

    values = _project_config_values(args, config, vm_name=vm_name, agent=agent)
    if not exists:
        values.update(
            {
                "copy_host_credentials": bool(_cfg(config, "sbx", "copy_host_credentials", False)),
                "git_config": bool(_cfg(config, "sbx", "git_config", True)),
            }
        )
    added = _write_project_config_values(path, values)
    if not exists and not created:
        print(f"sbx: wrote {path.name} for project defaults", file=sys.stderr)
    elif added and exists:
        print(
            f"sbx: updated {path.name} with missing project defaults: {', '.join(added)}",
            file=sys.stderr,
        )
    elif exists:
        print(
            f"sbx: {path.name} already contains project defaults; no changes made",
            file=sys.stderr,
        )
    return not exists and created


def _print_created(
    vm_name: str,
    *,
    config_created: bool,
    agent: str,
    attach: bool,
    run_user: str | None = None,
) -> None:
    if config_created:
        print(f"Created sandbox '{vm_name}'.")
        print("Wrote .sbx.toml.\n")
        print("Run agent:  sbx run")
        print("Open shell: sbx shell")
        print("Stop:       sbx stop")
        print("Remove:     sbx rm")
    elif attach:
        user = f" as user {run_user}" if run_user else ""
        print(f"Started '{vm_name}'. Launching {agent}{user}...")
    else:
        print(f"Started '{vm_name}'.")


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
    runtime_install: list[str] | None = None,
    install_timeout: int = 600,
    copy_initial_state: bool = False,
    forward_env: list[str] | None = None,
    port_forwards: Sequence[str] = (),
) -> int:
    from smolvm import SmolVM, VMConfig
    from smolvm.utils import ensure_ssh_key

    _warn_obsolete_manifest_fields(manifest)

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
        "comm_channel": "ssh",
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
        vm_metadata.claim_vm(
            str(vm_name), _project_identity(args, config), run_user or "root", created=True
        )
    except Exception:
        vm.close()
        raise
    vm.close()

    config_created = _maybe_write_project_config(
        args, config, vm_name=str(vm_name), agent=agent, created=True
    )
    json_output = bool(getattr(args, "json", False))
    if not json_output:
        _print_created(
            str(vm_name),
            config_created=config_created,
            agent=agent,
            attach=attach,
            run_user=run_user,
        )
    rc = _post_start_actions(
        vm_name=str(vm_name),
        started_now=True,
        command=LAUNCH_COMMANDS[agent],
        attach=attach,
        run_user=run_user,
        auth_port=auth_port,
        auth_host_port=auth_host_port,
        auth_guest_port=auth_guest_port,
        stop_on_exit=stop_on_exit,
        cwd=cwd,
        git_config_text=git_config_text,
        forward_env=forward_env,
        provision=True,
        runtime_install=runtime_install,
        install_timeout=install_timeout,
        copy_initial_state=copy_initial_state,
        agent_name=agent,
    )
    if rc == 0 and json_output:
        print(json.dumps({"vm": {"name": str(vm_name), "status": "running"}}))
    return rc


def cmd_doctor(args: argparse.Namespace) -> int:
    rc = runtime.run_smolvm(["doctor", "--backend", DEFAULT_BACKEND])
    lifecycle_warnings.doctor_config_state(
        getattr(args, "config_data", {}), smolvm_info=_smolvm_info_vm
    )
    return rc or doctor.run_doctor_checks(fix=bool(getattr(args, "fix", False)))


def cmd_completion(args: argparse.Namespace) -> int:
    print(completion_script(args.shell), end="")
    return 0


def cmd_image_build(args: argparse.Namespace) -> int:
    return build_debian.main_from_args(args)


def cmd_image_ls(args: argparse.Namespace) -> int:
    return sbx.image.ls.main_from_args(args)


def cmd_start(args: argparse.Namespace) -> int:
    config = args.config_data
    sbx_cfg = _sbx_config(config)
    project_identity = _project_identity(args, config)
    project_root = Path(project_identity["project_root"])
    attach = True if args.attach is None else bool(args.attach)
    if args.json and attach:
        print("sbx: run --json requires --no-attach", file=sys.stderr)
        return 2

    agent = args.agent or _cfg_agent(config)
    if agent not in AGENTS:
        print(f"sbx: agent must be one of: {', '.join(AGENTS)}", file=sys.stderr)
        return 2

    configured_backend = _cfg(config, "sbx", "backend", DEFAULT_BACKEND)
    if configured_backend != DEFAULT_BACKEND:
        print("sbx: other backends are not supported yet", file=sys.stderr)
        return 2

    memory_value = _arg_or_config(args, "memory", config, "sbx")
    disk_size_value = _arg_or_config(args, "disk_size", config, "sbx")
    install_timeout_value = _arg_or_config(args, "install_timeout", config, "sbx", default=600)
    try:
        memory_mib = int(memory_value) if memory_value is not None else None
        disk_size_mib = int(disk_size_value) if disk_size_value is not None else None
    except (TypeError, ValueError) as exc:
        raise ConfigError("memory and disk_size must be integers") from exc
    install_timeout = _install_timeout(install_timeout_value)
    runtime_install = _runtime_install(args, config)
    reprovision = (
        bool(getattr(args, "reprovision", False))
        or getattr(args, "runtime_install", None) is not None
    )
    requested_os = str(_arg_or_config(args, "os", config, "sbx", default="ubuntu"))
    boot_timeout = _validate_boot_timeout(
        _arg_or_config(args, "boot_timeout", config, "sbx", default=DEFAULT_BOOT_TIMEOUT)
    )

    mounts = (
        args.mount
        if args.mount is not None
        else _list_value(sbx_cfg.get("mount"), key="[sbx].mount")
    )
    effective_mounts: list[str] = []

    project_path = _arg_or_config(args, "project_path", config, "sbx")
    project_guest_cwd = _project_guest_cwd(project_path)
    if project_path is not None:
        effective_mounts.append(_same_path_mount(str(project_path)))

    for mount in mounts or []:
        effective_mounts.append(mount if ":" in mount else _same_path_mount(mount))

    writable_mounts = bool(_arg_or_config(args, "writable_mounts", config, "sbx", default=False))
    if project_path is not None:
        writable_mounts = True

    run_user = _arg_or_config(args, "run_user", config, "sbx")
    if run_user is not None:
        run_user = guest_setup.validate_run_user(str(run_user))
    copy_host_credentials = bool(
        _arg_or_config(args, "copy_host_credentials", config, "sbx", default=False)
    )
    forward_env = guest_setup.validate_env_names(
        args.env if args.env is not None else _list_value(sbx_cfg.get("env"), key="[sbx].env")
    )
    runtime.debug(
        "run options: "
        f"agent={agent!r}, name={getattr(args, 'name', None)!r}, attach={attach!r}, "
        f"run_user={run_user!r}, copy_host_credentials={copy_host_credentials!r}, "
        f"forward_env={forward_env!r}"
    )

    auth_port = attach and bool(_arg_or_config(args, "auth_port", config, "sbx", default=True))
    auth_host_port = int(_arg_or_config(args, "auth_host_port", config, "sbx", default=1455))
    auth_guest_port = int(_arg_or_config(args, "auth_guest_port", config, "sbx", default=1455))
    stop_on_exit = bool(_arg_or_config(args, "stop_on_exit", config, "sbx", default=True))
    git_config = bool(_arg_or_config(args, "git_config", config, "sbx", default=True))
    git_config_text = None
    cpus_value = _arg_or_config(args, "cpus", config, "sbx")
    cpus = _validate_cpus(cpus_value) if cpus_value is not None else None
    try:
        port_forwards = _list_value(sbx_cfg.get("port_forwards"), key="[sbx].port_forwards")
        parsed_port_forwards = network.port_forwards_from_specs(port_forwards)
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2

    image = lifecycle_warnings.path_from_config(_arg_or_config(args, "image", config, "sbx"))
    requested_name = _arg_or_config(args, "name", config, "sbx")
    if requested_name:
        requested_name = guest_setup.validate_vm_name(str(requested_name))
        existing_status = _get_existing_vm_status(str(requested_name))
        runtime.debug(f"existing VM lookup: name={requested_name!r}, status={existing_status!r}")
        if existing_status is not None:
            if image is not None:
                existing_manifest = lifecycle_warnings.local_image_manifest(image)
                _warn_obsolete_manifest_fields(existing_manifest)
                if run_user is None:
                    run_user = _manifest_run_user(existing_manifest)
            if run_user is None:
                run_user = guest_setup.host_username()
            args.run_user = run_user
            metadata = vm_metadata.claim_vm(
                str(requested_name), project_identity, str(run_user or "root"), created=False
            )
            was_provisioned = bool(metadata.get("provisioned"))
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
                json_output=bool(args.json),
            )
            if start_rc != 0:
                return start_rc
            _maybe_write_project_config(
                args, config, vm_name=str(requested_name), agent=str(agent), created=False
            )
            if reprovision and not was_provisioned:
                if copy_host_credentials:
                    _warn_initial_state_copy(str(requested_name))
                if git_config:
                    git_config_text = guest_setup.host_git_config(project_root)
            rc = _post_start_actions(
                vm_name=str(requested_name),
                started_now=existing_status != "running",
                command=LAUNCH_COMMANDS[str(agent)],
                attach=attach,
                run_user=str(run_user) if run_user is not None else None,
                auth_port=auth_port,
                auth_host_port=auth_host_port,
                auth_guest_port=auth_guest_port,
                stop_on_exit=stop_on_exit,
                cwd=project_guest_cwd,
                git_config_text=git_config_text,
                forward_env=forward_env,
                provision=reprovision,
                was_provisioned=was_provisioned,
                runtime_install=runtime_install,
                install_timeout=install_timeout,
                copy_initial_state=copy_host_credentials,
                agent_name=str(agent),
            )
            if rc == 0 and args.json:
                print(json.dumps({"vm": {"name": str(requested_name), "status": "running"}}))
            return rc

    if image is not None:
        try:
            manifest = lifecycle_warnings.local_image_manifest(image)
            if run_user is None:
                run_user = _manifest_run_user(manifest)
            if run_user is None:
                run_user = guest_setup.host_username()
            args.run_user = run_user
            if copy_host_credentials:
                _warn_initial_state_copy(str(requested_name or "new VM"))
            if git_config:
                git_config_text = guest_setup.host_git_config(project_root)
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
                runtime_install=runtime_install,
                install_timeout=install_timeout,
                copy_initial_state=copy_host_credentials,
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

    if run_user is None:
        run_user = guest_setup.host_username()
    args.run_user = run_user

    temp_home_ctx = None
    host_env = guest_setup.sanitize_forwarded_env(dict(os.environ), forward_env)
    if copy_host_credentials:
        _warn_initial_state_copy(str(requested_name or "new VM"))
    else:
        temp_home_ctx = tempfile.TemporaryDirectory(prefix="sbx-no-credentials-")
        host_env = guest_setup.credential_free_env(
            Path(temp_home_ctx.name), forward_env=forward_env
        )
        runtime.debug(f"credential-free HOME: {temp_home_ctx.name}")

    vm = None
    try:
        vm = smolvm_preset.create_preset(
            str(agent),
            vm_name=str(requested_name) if requested_name else None,
            guest_os=requested_os,
            cpus=cpus,
            memory_mib=memory_mib,
            disk_size_mib=disk_size_mib,
            mounts=effective_mounts,
            writable_mounts=writable_mounts,
            port_forwards=parsed_port_forwards,
            boot_timeout=boot_timeout,
            install_timeout=install_timeout,
            host_env=host_env,
            before_provision=lambda new_vm_name: vm_metadata.claim_vm(
                new_vm_name, project_identity, str(run_user or "root"), created=True
            ),
        )
        vm_name = str(vm.vm_id)
    except Exception as exc:  # noqa: BLE001 - keep CLI errors user-friendly.
        if _maybe_print_boot_timeout_running_hint(
            str(requested_name) if requested_name else None, boot_timeout
        ):
            return 1
        print(f"sbx: failed to create preset {str(agent)!r}: {exc}", file=sys.stderr)
        return 1
    finally:
        if vm is not None:
            with suppress(Exception):
                vm.close()
        if temp_home_ctx is not None:
            temp_home_ctx.cleanup()

    config_created = _maybe_write_project_config(
        args, config, vm_name=vm_name, agent=str(agent), created=True
    )
    if not args.json:
        _print_created(
            vm_name,
            config_created=config_created,
            agent=str(agent),
            attach=attach,
            run_user=str(run_user) if run_user is not None else None,
        )

    if git_config:
        git_config_text = guest_setup.host_git_config(project_root)
    rc = _post_start_actions(
        vm_name=vm_name,
        started_now=True,
        command=LAUNCH_COMMANDS[str(agent)],
        attach=attach,
        run_user=str(run_user) if run_user is not None else None,
        auth_port=auth_port,
        auth_host_port=auth_host_port,
        auth_guest_port=auth_guest_port,
        stop_on_exit=stop_on_exit,
        cwd=project_guest_cwd,
        git_config_text=git_config_text,
        forward_env=forward_env,
        provision=True,
        runtime_install=runtime_install,
        install_timeout=install_timeout,
        copy_initial_state=copy_host_credentials,
        agent_name=str(agent),
    )
    if rc == 0 and args.json:
        print(json.dumps({"vm": {"name": vm_name, "status": "running"}}))
    return rc


def _confirm_destructive_action(message: str, *, force: bool) -> bool:
    if force:
        return True
    if not sys.stdin.isatty():
        print(f"sbx: refusing destructive action without --force: {message}", file=sys.stderr)
        return False
    answer = input(f"{message} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def cmd_shell(args: argparse.Namespace) -> int:
    config = args.config_data
    project_identity = _project_identity(args, config)
    name = runtime.vm_name_from_arg_or_config(args, config, "shell")
    if name is None:
        return 2
    try:
        forward_env = guest_setup.validate_env_names(
            _list_value(_sbx_config(config).get("env"), key="[sbx].env")
        )
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2
    run_user = None if args.root else args.run_user or _cfg(config, "sbx", "run_user")
    image = lifecycle_warnings.path_from_config(_cfg(config, "sbx", "image"))
    if image is not None:
        shell_manifest = lifecycle_warnings.local_image_manifest(image)
        _warn_obsolete_manifest_fields(shell_manifest)
        if run_user is None and not args.root:
            run_user = _manifest_run_user(shell_manifest)
    if run_user is not None:
        run_user = guest_setup.validate_run_user(str(run_user))
    elif not args.root:
        run_user = guest_setup.host_username()
    project_path = args.project_path or _cfg(config, "sbx", "project_path")
    project_guest_cwd = _project_guest_cwd(project_path)
    keep_running = bool(getattr(args, "keep_running", False))
    stop_on_exit = bool(_cfg(config, "sbx", "stop_on_exit", True)) and not keep_running
    try:
        port_forwards = _list_value(
            _sbx_config(config).get("port_forwards"), key="[sbx].port_forwards"
        )
        network.port_forwards_from_specs(port_forwards)
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2
    existing_status = _get_existing_vm_status(name)
    if existing_status is None:
        print(f"sbx: {runtime.missing_vm_message(name)}", file=sys.stderr)
        return 1
    vm_metadata.claim_vm(
        name, project_identity, str(run_user) if run_user is not None else None, created=False
    )
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
    start_rc = _start_existing_vm_if_needed(name, existing_status, DEFAULT_BOOT_TIMEOUT)
    if start_rc != 0:
        return start_rc
    return _post_start_actions(
        vm_name=name,
        started_now=existing_status != "running",
        diagnostic_root=bool(args.root),
        command="bash",
        attach=True,
        run_user=run_user,
        auth_port=False,
        stop_on_exit=stop_on_exit,
        cwd=project_guest_cwd,
        session_kind="shell",
        forward_env=forward_env,
        validate_agent=False,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    config = args.config_data
    name = runtime.vm_name_from_arg_or_config(args, config, "stop")
    if name is None:
        return 2
    return runtime.run_smolvm(["sandbox", "stop", name])


def cmd_remove(args: argparse.Namespace) -> int:
    config = args.config_data
    name = runtime.vm_name_from_arg_or_config(args, config, str(args.action))
    if name is None:
        return 2
    if not _confirm_destructive_action(f"Destroy VM '{name}'?", force=args.force):
        return 2
    return _delete_vm(name)


def _delete_vm(vm_id: str, extra_args: Sequence[str] | None = None, *, quiet: bool = False) -> int:
    extra = list(extra_args or [])
    completed = runtime.run_smolvm_capture(["sandbox", "delete", vm_id, *extra, "--json"])
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
            vm_metadata.remove_vm(vm_id)
        if not quiet:
            print(f"Destroyed VM '{vm_id}'.")
        return 0

    if any(item.get("error") == f"VM '{vm_id}' not found" for item in failed):
        with suppress(ConfigError):
            vm_metadata.remove_vm(vm_id)
        if not quiet:
            print(f"VM '{vm_id}' not found; nothing to destroy.")
        return 0

    if completed.stdout:
        print(completed.stdout, end="")
    return completed.returncode


def cmd_create(args: argparse.Namespace) -> int:
    args.attach = False
    args.auth_port = False
    return cmd_start(args)


def cmd_recreate(args: argparse.Namespace) -> int:
    config = args.config_data
    force = args.force
    name = args.name or _cfg(config, "sbx", "name")
    if not name:
        print("sbx: recreate requires a VM name argument or [sbx].name", file=sys.stderr)
        return 2

    if not _confirm_destructive_action(f"Destroy and recreate VM '{name}'?", force=force):
        return 2

    json_output = bool(getattr(args, "json", False))
    destroy_rc = _delete_vm(str(name), quiet=True) if json_output else _delete_vm(str(name))
    if destroy_rc != 0:
        return destroy_rc
    args.name = str(name)
    args.attach = False
    args.auth_port = False
    return cmd_start(args)


def _add_start_options(parser: argparse.ArgumentParser) -> None:
    session = parser.add_argument_group("Session")
    session.add_argument(
        "--agent", choices=AGENTS, help="Agent preset to run (default: [sbx].agent or pi)."
    )
    session.add_argument(
        "--run-user",
        help="When attaching, create/use this guest user and run the agent as that user.",
    )
    session.add_argument(
        "--env",
        action="append",
        default=None,
        metavar="KEY",
        help="Forward this host environment variable into the guest. Can be repeated.",
    )
    session.add_argument(
        "--no-attach",
        dest="attach",
        action="store_false",
        default=None,
        help="Create or start the VM but do not launch the agent.",
    )
    stop_on_exit = session.add_mutually_exclusive_group()
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

    workspace = parser.add_argument_group("Workspace")
    workspace.add_argument("--mount", action="append", metavar="HOST_PATH[:GUEST_PATH]")
    workspace.add_argument(
        "--project-path",
        help="Mount this host path at the same absolute guest path as read-write.",
    )
    workspace.add_argument("--writable-mounts", action="store_true", default=None)

    resources = parser.add_argument_group("VM resources")
    resources.add_argument("--memory", type=int, metavar="MIB")
    resources.add_argument("--cpus", type=int, metavar="COUNT", help="Number of virtual CPUs.")
    resources.add_argument("--disk-size", type=int, metavar="MIB")
    resources.add_argument("--image", help="Local ready-to-run image directory.")
    resources.add_argument(
        "--boot-timeout",
        type=float,
        help="Seconds to wait for VM boot/SSH readiness (default: [sbx].boot_timeout or 60).",
    )
    resources.add_argument("--install-timeout", type=int)

    provisioning = parser.add_argument_group("Provisioning")
    provisioning.add_argument(
        "--runtime-install",
        action="append",
        default=None,
        metavar="TOOL",
        help="Install this supported runtime tool. Can be repeated and replaces config values.",
    )

    output = parser.add_argument_group("Configuration and output")
    output.add_argument(
        "--write-config",
        action="store_true",
        default=None,
        help="Create or update .sbx.toml with missing project defaults.",
    )
    output.add_argument("--json", action="store_true", default=None)
    parser.add_argument("name", nargs="?", metavar="NAME", help="Sandbox name.")


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
    run.add_argument(
        "--reprovision",
        action="store_true",
        help="Rerun idempotent user and runtime-tool provisioning for an existing VM.",
    )
    run.set_defaults(func=cmd_start)

    create = sub.add_parser("create", help="Create and provision a sandbox without attaching.")
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
            "--running", action="store_true", help="List only running sandboxes."
        )
        list_parser.add_argument("--json", action="store_true", help="Print JSON.")
        list_parser.set_defaults(func=cmd_list)

    network_parser = sub.add_parser("network", help="Expert networking helpers.")
    network_sub = network_parser.add_subparsers(dest="network_action", required=True)
    forward = network_sub.add_parser(
        "forward",
        help="Forward a host TCP port to a running sandbox until Ctrl-C.",
    )
    forward.add_argument(
        "specs",
        nargs="+",
        metavar="SPEC",
        help="GUEST_PORT, HOST_PORT:GUEST_PORT, or BIND_HOST:HOST_PORT:GUEST_PORT.",
    )
    forward.add_argument("--name", help="Sandbox name or ID. Defaults to [sbx].name.")
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
    network_status.add_argument("--json", action="store_true", help="Print JSON.")
    network_status.set_defaults(func=network.cmd_status)

    image = sub.add_parser("image", help="Advanced local image helpers.")
    image_sub = image.add_subparsers(dest="image_action", required=True)
    build_parser = image_sub.add_parser("build", help="Build the curated local image for sbx.")
    build_debian.add_arguments(build_parser)
    build_parser.set_defaults(func=cmd_image_build)

    for image_list_name in ("list", "ls"):
        list_images_parser = image_sub.add_parser(image_list_name, help="List local sbx images.")
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


def main(argv: Sequence[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    runtime.DEBUG = bool(args.debug)
    runtime.debug(f"argv: {raw_argv}")
    if args.action in {"completion", "image"}:
        args.config_data = {}
    else:
        try:
            args.config_data = load_config(args.config)
        except ConfigError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 2
    try:
        with runtime.without_stale_sudo_env():
            return args.func(args)
    except KeyboardInterrupt:
        # Shell convention: signal exits are 128 + signal number.
        return 128 + signal.SIGINT
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
