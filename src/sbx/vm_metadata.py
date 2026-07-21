import json
from collections.abc import Mapping

from sbx.constants import SBX_STATE_DIR, SBX_VMS_FILE
from sbx.runtime import ConfigError


def load_vm_metadata() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(SBX_VMS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid sbx VM metadata: {SBX_VMS_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"invalid sbx VM metadata: {SBX_VMS_FILE}: expected object")
    data: dict[str, dict[str, str]] = {}
    for name, value in raw.items():
        if isinstance(name, str) and isinstance(value, dict):
            project_root = value.get("project_root")
            config_path = value.get("config_path")
            if isinstance(project_root, str) and isinstance(config_path, str):
                data[name] = {"project_root": project_root, "config_path": config_path}
    return data


def save_vm_metadata(data: Mapping[str, Mapping[str, str]]) -> None:
    SBX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    SBX_VMS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_vm_project(vm_name: str, project: Mapping[str, str]) -> None:
    data = load_vm_metadata()
    data[vm_name] = {"project_root": project["project_root"], "config_path": project["config_path"]}
    save_vm_metadata(data)


def validate_vm_project(vm_name: str, project: Mapping[str, str]) -> None:
    saved = load_vm_metadata().get(vm_name)
    if saved and saved.get("project_root") != project["project_root"]:
        raise ConfigError(
            f"VM {vm_name!r} belongs to {saved.get('project_root')}; "
            f"refusing to update it from {project['project_root']}. "
            "Run `sbx doctor --fix` to repair stale metadata."
        )
