# `--force-start` design

## Goal

Allow a user to retry starting an existing VM whose SmolVM status is `error` without deleting/recreating the VM.

## Scope

In scope:

- Add `--force-start` to `sbx run` and `sbx shell`.
- Keep default behavior safe: `error` VMs still refuse to start unless the flag is passed.
- Retry the normal SmolVM start path after clearing stale error state.

Out of scope:

- Repairing corrupted disks/configs.
- Recreating VMs.
- Adding a persistent config option.

## Behavior

- `sbx run VM` / `sbx shell VM` with status `error` returns a clear error.
- `sbx run VM --force-start` / `sbx shell VM --force-start` resets the SmolVM DB row from `error` to `stopped`, clears stale pid/socket fields when present, then calls `smolvm sandbox start VM` normally.
- If the VM fails again, SmolVM will put it back into `error`; surface the normal start failure.

## Why DB reset

SmolVM currently refuses to start a VM in `error` state. The smallest compatible retry path is to mark the existing VM as stopped before invoking SmolVM's normal start command.
