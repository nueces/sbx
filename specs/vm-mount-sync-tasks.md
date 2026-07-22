# Existing VM mount sync tasks

## Implementation

1. [x] Add `_sync_existing_vm_start_config(...)` in `src/sbx/cli.py` that parses effective mount/port-forward specs, opens SmolVM's SQLite state DB, compares stored start config, and writes only when different.
2. [x] Call the helper from `cmd_start()` only when the named VM exists and is not running.
3. [x] Keep running VMs unchanged; users must `sbx stop` before mount edits take effect.
4. [x] Print one short message only when mounts or port forwards are updated.

## Tests

1. [x] Temporary DB test: stale stopped VM mounts are updated.
2. [x] Temporary DB test: matching stopped VM mounts are not rewritten.
3. [x] `cmd_start()` existing running VM path does not sync.
4. [x] Mount parsing covers bare same-path and explicit `HOST:GUEST` specs.
5. [x] Duplicate guest paths raise `ConfigError`.
6. [x] Missing DB/VM row returns without update and lets the existing start path report the missing VM.
7. [x] Do not add separate local-image/preset sync tests; existing VM config is shared.

## Checks

```bash
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
