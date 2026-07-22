# Environment variable sync design

## Goal

Make configured host environment variables current each time a user attaches to an existing `sbx` VM.

Expected workflow:

```bash
# .sbx.toml
# [sbx]
# name = "sbx"
# env = ["TEST_ENV"]

export TEST_ENV=old
sbx run sbx

export TEST_ENV=new
sbx shell sbx   # new shell sees TEST_ENV=new
sbx run sbx     # newly started agent sees TEST_ENV=new
```

The VM does not need to be recreated or rebooted for new attach sessions to see the updated values.

## Problem

Today `[sbx].env` and `--env` are only used while creating/applying a SmolVM preset. After a named VM exists, `sbx run NAME` starts/reuses it and attaches without updating the guest-managed environment. `sbx shell NAME` does not read `[sbx].env` at all.

This leaves stale values in the guest until the VM is recreated or the user manually updates SmolVM env state.

## Scope

In scope:

- Sync configured env allowlist before `sbx run` attaches to an existing VM.
- Sync configured env allowlist before `sbx shell` attaches to a VM.
- Use the current host process environment as the source of truth.
- Set configured keys that exist in the host environment.
- Unset configured keys that are missing from the host environment, so stale values do not remain in the guest.
- Use SmolVM's Python API (`SmolVM.from_id(...).set_env_vars(...)` / `unset_env_vars(...)`) instead of passing secret values in CLI argv.
- Keep create-time behavior working for newly created preset VMs.
- Keep env names validated with the existing validation rule.

Out of scope:

- Updating environment variables inside an already-running `pi`/`claude`/`codex` process.
- FUSE/9p/host secret broker designs.
- Hot-reloading Pi with new process environment.
- Syncing all host env vars by default.
- Creating a new user-facing `sbx env` command.
- Changing SmolVM's env storage format.
- Changing auth-file/keyring behavior.

## Design

Use the lazy path: keep one small helper in `src/sbx/guest_setup.py` and call it before attach.

```python
def sync_forwarded_env(vm_id: str, names: list[str]) -> None:
    values = {name: os.environ[name] for name in names if name in os.environ}
    missing = [name for name in names if name not in os.environ]
    if not values and not missing:
        return

    from smolvm.facade import SmolVM

    vm = SmolVM.from_id(vm_id)
    try:
        if values:
            vm.set_env_vars(values)
        if missing:
            vm.unset_env_vars(missing)
    finally:
        vm.close()
```

The exact implementation may differ, but it must not put secret values in subprocess argv.

## Where to call it

### Existing VM `sbx run`

In `cmd_start()`'s existing-VM branch, after `_start_existing_vm_if_needed(...)` succeeds and before `_post_start_actions(...)`:

```python
guest_setup.sync_forwarded_env(str(requested_name), forward_env)
```

This covers both stopped VMs that just started and running VMs that are being reused.

### New-VM attach paths

Avoid duplicate env writes:

- Fresh preset VM: do not post-sync. SmolVM preset apply already injects allowlisted env vars during install because `sbx` exposes only `[sbx].env` / `--env` keys to that process.
- Fresh local-image VM: post-sync before attach. Local-image startup does not run SmolVM preset install, so nothing else applies `[sbx].env`.
- `--no-attach` / `create`: skip post-sync. No new agent/shell process is being started by `sbx`; the next `run` or `shell` attach will sync first.

Call `guest_setup.sync_forwarded_env(...)` directly in the few paths that need it; do not add a generic flag unless another caller appears.

### `sbx shell`

In `cmd_shell()`, read and validate `[sbx].env` from config, then call `guest_setup.sync_forwarded_env(name, forward_env)` before attaching. `sbx shell` does not need a new `--env` flag for the first version; config is enough and keeps the CLI small.

## Missing host variables

If a key is listed in `[sbx].env` / `--env` but is absent from the host environment, unset that key from the guest-managed env. This prevents stale secrets from lingering after the host value is removed.

If unsetting a missing guest key is a no-op, treat it as success.

## Error behavior

If env sync fails, fail before attach with a user-facing error such as `sbx: failed to sync environment for VM 'sbx': <reason>`. Starting an agent with stale credentials is worse than stopping early. Do not print env values; debug output may log key names only.

## Security notes

- Env values must not be passed as command-line arguments.
- Env values may still be visible to same-user host processes through the parent `sbx` process environment and may be persisted in SmolVM's guest-managed env file. This feature is convenience forwarding, not a secret broker.
- Higher-security package/auth mediation remains a separate design.

## Test strategy

Use focused tests with fake SmolVM modules/classes. Do not boot real VMs.

Test cases:

- `guest_setup.sync_forwarded_env` sets host-present keys through `SmolVM.from_id(...).set_env_vars(...)`.
- `guest_setup.sync_forwarded_env` unsets configured-but-host-missing keys through `unset_env_vars(...)`.
- `guest_setup.sync_forwarded_env` does nothing for an empty allowlist.
- Existing-VM `sbx run` calls env sync before attach.
- `sbx shell` reads `[sbx].env` and calls env sync before attach.
- Invalid env names still fail before VM operations.

## Verification plan

Run focused tests first:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
```

Then run lint:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
