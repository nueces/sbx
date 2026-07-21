# Remove `--force-start` design

## Goal

Use `sbx doctor --fix` as the only safe repair path for existing VMs whose SmolVM status is `error`.

## Previous behavior

`sbx run VM --force-start` and `sbx shell VM --force-start` reset the SmolVM DB row from `error` to `stopped`, cleared stale pid/socket fields when present, then started the VM.

## Desired behavior

- Remove `--force-start` from `sbx run` and `sbx shell`.
- `sbx run VM` / `sbx shell VM` with status `error` must refuse to start and print the doctor flow.
- `sbx doctor --fix` repairs safe local bookkeeping, including marking SmolVM `error` VMs as stopped.
- The user then reruns `sbx run VM` or `sbx shell VM` normally.

Error message shape:

```text
sbx: VM 'dt' is in error state.
sbx: Run `sbx doctor --fix` to repair local VM bookkeeping, then retry `sbx run dt`.
sbx: If it still fails, run `sbx recreate dt --force`.
```

## Why remove it

`--force-start` combines repair and start in one command. `doctor --fix` is clearer: repair state first, then start normally. No compatibility window is needed.

## Non-goals

- No deprecated alias for `--force-start`.
- No automatic repair during `run` or `shell`.
- No guest disk repair.
- No VM deletion/recreation.
