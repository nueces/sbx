# Environment variable sync tasks

## Implementation

1. [x] Add `guest_setup.sync_forwarded_env(vm_id, names)` that sets host-present keys and unsets host-missing keys via SmolVM's Python API, with direct-SSH fallback for legacy VMs.
2. [x] Call env sync in the existing-VM `sbx run` path after start/reuse succeeds and before attach.
3. [x] Call env sync only in the local-image new-VM attach path; fresh preset VMs already inject allowlisted env during preset apply.
4. [x] Make `sbx shell` read `[sbx].env`, validate it, and sync before attach.
5. [x] On sync failure, fail before attach without printing secret values.

## Tests

1. [x] Helper sets host-present keys, unsets host-missing keys, and skips SmolVM for an empty allowlist.
2. [x] Existing-VM `sbx run` syncs env before attach.
3. [x] `sbx shell` syncs env from config before attach.
4. [x] Invalid env names still fail before VM operations.

## Checks

```bash
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
