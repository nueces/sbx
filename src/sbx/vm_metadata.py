import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sbx.constants import SBX_VMS_DIR
from sbx.runtime import ConfigError

VM_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def metadata_path(vm_name: str) -> Path:
    if not VM_NAME_RE.fullmatch(vm_name):
        raise ConfigError(f"invalid VM name for metadata: {vm_name!r}")
    return SBX_VMS_DIR / f"{vm_name}.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid sbx VM metadata: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"invalid sbx VM metadata: {path}: expected an object")
    return value


def load_vm(vm_name: str) -> dict[str, Any] | None:
    path = metadata_path(vm_name)
    return _read(path) if path.exists() else None


def load_vm_metadata() -> dict[str, dict[str, Any]]:
    if not SBX_VMS_DIR.exists():
        return {}
    data: dict[str, dict[str, Any]] = {}
    for path in sorted(SBX_VMS_DIR.glob("*.json")):
        name = path.stem
        if VM_NAME_RE.fullmatch(name):
            data[name] = _read(path)
    return data


def _ensure_directory() -> None:
    SBX_VMS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SBX_VMS_DIR.chmod(0o700)


def _write(vm_name: str, data: Mapping[str, Any]) -> None:
    _ensure_directory()
    path = metadata_path(vm_name)
    fd, temporary = tempfile.mkstemp(prefix=f".{vm_name}.", suffix=".tmp", dir=SBX_VMS_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _create(vm_name: str, data: Mapping[str, Any]) -> bool:
    _ensure_directory()
    path = metadata_path(vm_name)
    fd, temporary = tempfile.mkstemp(prefix=f".{vm_name}.", suffix=".tmp", dir=SBX_VMS_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        path.chmod(0o600)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_vm_project(vm_name: str, project: Mapping[str, str]) -> None:
    saved = load_vm(vm_name)
    if saved and saved.get("project_root") != project["project_root"]:
        raise ConfigError(
            f"VM {vm_name!r} belongs to {saved.get('project_root')}; "
            f"refusing to update it from {project['project_root']}. "
            "Run `sbx doctor --fix` to repair stale metadata."
        )


def claim_vm(
    vm_name: str,
    project: Mapping[str, str],
    run_user: str | None,
    *,
    created: bool,
) -> dict[str, Any]:
    saved = load_vm(vm_name)
    if saved is None:
        if not created:
            raise ConfigError(
                f"existing VM {vm_name!r} has no trusted sbx runtime identity; "
                f"run `sbx recreate {vm_name} --force`"
            )
        if run_user is None:
            raise ConfigError(f"cannot claim new VM {vm_name!r} without a runtime user")
        saved = {
            "project_root": project["project_root"],
            "config_path": project["config_path"],
            "run_user": run_user,
            "provisioned": False,
        }
        if _create(vm_name, saved):
            return saved
        saved = load_vm(vm_name)
        if saved is None:
            raise ConfigError(f"failed to claim metadata for VM {vm_name!r}")

    validate_vm_project(vm_name, project)
    recorded_user = saved.get("run_user")
    if run_user is not None and recorded_user != run_user:
        raise ConfigError(
            f"VM {vm_name!r} is provisioned for {recorded_user!r}, not {run_user!r}; "
            f"run `sbx recreate {vm_name} --force`"
        )
    saved.update(project_root=project["project_root"], config_path=project["config_path"])
    _write(vm_name, saved)
    return saved


def mark_provisioned(vm_name: str) -> None:
    data = load_vm(vm_name)
    if data is None:
        raise ConfigError(f"VM {vm_name!r} has no sbx metadata")
    data["provisioned"] = True
    _write(vm_name, data)


def remove_vm(vm_name: str) -> None:
    metadata_path(vm_name).unlink(missing_ok=True)
