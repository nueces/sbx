import json
import os
from collections.abc import Mapping
from typing import Any

from sbx.constants import SBX_STATE_DIR, SESSIONS_FILE


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


def load_sessions() -> dict[str, Any]:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sessions(data: Mapping[str, Any]) -> None:
    SBX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def active_sessions(vm_id: str) -> list[dict[str, Any]]:
    data = load_sessions()
    raw_sessions = data.get(vm_id, {}).get("sessions", [])
    sessions = [item for item in raw_sessions if isinstance(item, dict)]
    active = [
        item
        for item in sessions
        if isinstance(item.get("pid"), int) and pid_is_alive(int(item["pid"]))
    ]
    if active != sessions:
        if active:
            data.setdefault(vm_id, {})["sessions"] = active
        else:
            data.pop(vm_id, None)
        save_sessions(data)
    return active


def register_session(vm_id: str, kind: str) -> None:
    sessions = active_sessions(vm_id)
    sessions.append({"pid": os.getpid(), "kind": kind})
    data = load_sessions()
    data.setdefault(vm_id, {})["sessions"] = sessions
    save_sessions(data)


def unregister_session(vm_id: str) -> int:
    data = load_sessions()
    sessions = data.get(vm_id, {}).get("sessions", [])
    remaining = [
        item
        for item in sessions
        if isinstance(item, dict)
        and item.get("pid") != os.getpid()
        and isinstance(item.get("pid"), int)
        and pid_is_alive(int(item["pid"]))
    ]
    if remaining:
        data.setdefault(vm_id, {})["sessions"] = remaining
    else:
        data.pop(vm_id, None)
    save_sessions(data)
    return len(remaining)
