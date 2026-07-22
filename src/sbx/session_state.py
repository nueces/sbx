import os
from collections.abc import Mapping
from typing import Any

from sbx.constants import SBX_STATE_DIR, SESSIONS_FILE
from sbx.runtime import pid_is_alive, read_json_object, write_json_object


def load_sessions() -> dict[str, Any]:
    return read_json_object(SESSIONS_FILE)


def save_sessions(data: Mapping[str, Any]) -> None:
    write_json_object(SESSIONS_FILE, data, state_dir=SBX_STATE_DIR)


def live_sessions(raw_sessions: object) -> list[dict[str, Any]]:
    sessions = (
        [item for item in raw_sessions if isinstance(item, dict)]
        if isinstance(raw_sessions, list)
        else []
    )
    return [
        item
        for item in sessions
        if isinstance(item.get("pid"), int) and pid_is_alive(int(item["pid"]))
    ]


def active_sessions(vm_id: str) -> list[dict[str, Any]]:
    data = load_sessions()
    raw_sessions = data.get(vm_id, {}).get("sessions", [])
    sessions = [item for item in raw_sessions if isinstance(item, dict)]
    active = live_sessions(raw_sessions)
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
