from pathlib import Path

import sbx.network as network
from sbx import session_state, vm_metadata, vm_state
from sbx.constants import SBX_VMS_FILE
from sbx.runtime import ConfigError


def doctor_metadata(*, fix: bool) -> int:
    try:
        metadata = vm_metadata.load_vm_metadata()
    except ConfigError as exc:
        print(f"sbx metadata: {exc}")
        if fix and SBX_VMS_FILE.exists():
            backup = SBX_VMS_FILE.with_suffix(SBX_VMS_FILE.suffix + ".bak")
            SBX_VMS_FILE.replace(backup)
            print(f"  fixed: moved corrupt metadata to {backup}")
            return 0
        return 1

    if not metadata:
        return 0
    existing = {str(getattr(vm, "vm_id", "")) for vm in vm_state.smolvm_vms(all_vms=True)}
    changed = False
    for name, item in list(metadata.items()):
        reason = None
        if name not in existing:
            reason = "VM missing from SmolVM"
        elif not Path(item["project_root"]).exists():
            reason = f"project path missing: {item['project_root']}"
        if reason is None:
            continue
        print(f"sbx metadata: {name}: {reason}")
        if fix:
            metadata.pop(name, None)
            changed = True
            print(f"  fixed: removed metadata for {name}")
    if changed:
        vm_metadata.save_vm_metadata(metadata)
    return 0


def doctor_sessions(*, fix: bool) -> None:
    data = session_state.load_sessions()
    changed = False
    for vm_id, vm_data in list(data.items()):
        raw = vm_data.get("sessions", []) if isinstance(vm_data, dict) else []
        sessions = [item for item in raw if isinstance(item, dict)]
        active = session_state.live_sessions(raw)
        if active == sessions:
            continue
        changed = True
        print(f"sbx sessions: {vm_id}: stale session record(s)")
        if fix:
            if active:
                data.setdefault(vm_id, {})["sessions"] = active
            else:
                data.pop(vm_id, None)
    if changed and fix:
        session_state.save_sessions(data)
        print("  fixed: removed stale session record(s)")


def doctor_tunnels(*, fix: bool) -> None:
    data = network._load_tunnels()
    changed = False
    for vm_id, vm_data in list(data.items()):
        if not isinstance(vm_data, dict):
            data.pop(vm_id, None)
            changed = True
            continue
        tunnel = vm_data.get("auth_port")
        pid = tunnel.get("pid") if isinstance(tunnel, dict) else None
        if not isinstance(pid, int) or not session_state.pid_is_alive(pid):
            print(f"sbx tunnels: {vm_id}: stale auth tunnel record")
            if fix:
                vm_data.pop("auth_port", None)
                if not vm_data:
                    data.pop(vm_id, None)
                changed = True
    if changed and fix:
        network._save_tunnels(data)
        print("  fixed: removed stale tunnel record(s)")


def doctor_error_vms(*, fix: bool) -> None:
    for vm in vm_state.smolvm_vms(all_vms=True):
        if getattr(getattr(vm, "status", None), "value", getattr(vm, "status", None)) != "error":
            continue
        name = str(getattr(vm, "vm_id", ""))
        print(f"smolvm state: {name}: VM is in error state")
        if fix:
            vm_state.mark_error_vm_stopped_for_restart(name)
            print(f"  fixed: {name} marked stopped")


def run_doctor_checks(*, fix: bool) -> int:
    meta_rc = doctor_metadata(fix=fix)
    doctor_sessions(fix=fix)
    doctor_tunnels(fix=fix)
    doctor_error_vms(fix=fix)
    return meta_rc
