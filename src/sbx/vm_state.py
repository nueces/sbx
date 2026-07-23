import json
import sqlite3
from typing import Any

from sbx.constants import SMOLVM_DB_PATH


def smolvm_vms(all_vms: bool = False) -> list[Any]:
    from smolvm.types import VMState
    from smolvm.vm import SmolVMManager

    status = None if all_vms else VMState.RUNNING
    return SmolVMManager().list_vms(status=status)


def existing_vm_start_config(vm_name: str) -> tuple[str, dict[str, Any]] | None:
    db_path = SMOLVM_DB_PATH.expanduser()
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, config FROM vms WHERE id = ?", (vm_name,)).fetchone()
    if row is None:
        return None
    return str(row["status"]), json.loads(row["config"])


def mark_error_vm_stopped_for_restart(vm_id: str) -> None:
    db_path = SMOLVM_DB_PATH.expanduser()
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE vms
            SET status = 'stopped', pid = NULL, socket_path = NULL
            WHERE id = ? AND status = 'error'
            """,
            (vm_id,),
        )
