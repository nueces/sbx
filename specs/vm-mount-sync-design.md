# Existing VM mount sync design

## Goal

Let users add, remove, or change mounts for an already-created `sbx` VM by editing `.sbx.toml`, stopping the VM, and starting it again. The VM disk must be preserved.

Expected workflow:

```bash
# edit .sbx.toml mount/project_path/writable_mounts
sbx stop sbx
sbx run sbx
```

## Problem

Today `.sbx.toml` is only used when `sbx` creates a VM. If a VM with `[sbx].name` already exists, `sbx` starts the existing SmolVM VM and ignores new mount config. SmolVM stores mounts in its persisted VM config, so a plain stop/start does not pick up `.sbx.toml` mount edits.

## Scope

In scope:

- Sync existing stopped VM `workspace_mounts` from effective `sbx` mount config.
- Support both `project_path` and `mount` entries.
- Preserve current bare mount behavior: `mount = ["./dir"]` mounts at the same absolute guest path.
- Preserve global `writable_mounts`; `project_path` still forces writable mounts.
- Work for VMs created from local images and preset-based VMs.
- Avoid rewriting SmolVM state when mounts are already correct.
- Refuse to change mounts while the VM is running.

Out of scope:

- Hot-adding mounts to a running VM.
- Per-mount read-only/read-write flags.
- Syncing memory, CPU, disk, image, OS, or other VM settings.
- Recreating or destroying the VM.
- Adding a new user-facing command.

## Design

Use the smallest fix: `cmd_start()` already handles `run` and `create`, so sync mounts in its existing-VM branch before starting a stopped VM.

The logic belongs in `cmd_start()` in the existing-VM branch, before `_start_existing_vm_if_needed(...)`:

```python
if existing_status is not None:
    if existing_status != "running":
        _sync_existing_vm_start_config(
            vm_name=str(requested_name),
            mounts=effective_mounts,
            writable_mounts=writable_mounts,
            port_forwards=effective_port_forwards,
        )
    start_rc = _start_existing_vm_if_needed(...)
```

`effective_mounts` is already built before the existing-VM branch and already includes:

1. `project_path` as a same-path mount, first.
2. Any configured/CLI `mount` entries, with bare paths converted to same-path mounts.

The sync helper reads SmolVM's state DB, loads the target row's JSON config, replaces `workspace_mounts` and `port_forwards` only when needed, and writes the row back only when the stored config actually changed.

Desired stored mount shape:

```json
{
  "host_path": "/absolute/host/path",
  "guest_path": "/absolute/guest/path",
  "mount_tag": null,
  "writable": true
}
```

Parsing rules:

- `HOST:GUEST` keeps explicit `GUEST`.
- `HOST` uses the resolved absolute host path as guest path.
- Host path must exist and be a directory.
- Guest path must be absolute.
- Duplicate guest paths are config errors.

## State DB access

Use SmolVM's existing SQLite DB path:

```text
~/.local/state/smolvm/smolvm.db
```

Implementation should keep this private/internal.

SQL shape:

```sql
SELECT id, status, config FROM vms WHERE id = ?
UPDATE vms SET config = ? WHERE id = ?
```

If the DB or VM row is missing, return without update and let the existing start path report the missing VM. The sync helper must not create VMs.

## User-visible behavior

When mounts change:

```text
sbx: updated mounts for existing VM 'sbx'
```

When mounts are already correct, print nothing.

When the VM is running, do not sync. A running VM should keep current behavior; QEMU mounts are boot-time config.

## Test strategy

Add focused tests with a temporary SQLite DB and monkeypatch the DB path used by the helper.

Test cases:

- Existing stopped VM with stale mounts gets `workspace_mounts` updated.
- Existing stopped VM with matching mounts does not execute an UPDATE.
- Running VM is not modified by `cmd_start()` existing-VM path.
- Bare mount resolves to same absolute host/guest path.
- Explicit `HOST:GUEST` is preserved.
- Duplicate guest paths fail with a `ConfigError`.
- Local-image and preset-created VMs need no separate implementation path; one existing-VM sync test is enough because both use the same stored `workspace_mounts` config.

## Verification plan

Run focused tests first:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
```

Then run the normal checks:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```

## Open questions

None for the minimal sync feature.
