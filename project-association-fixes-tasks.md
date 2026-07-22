# Project association fixes implementation tasks

## Phase 1: sbx VM metadata

- [x] Add `SBX_VMS_FILE = SBX_STATE_DIR / "vms.json"`.
- [x] Add helpers to load/save sbx VM metadata JSON.
- [x] Add helper to resolve current project root/config path from `--config`, local `.sbx.toml`, `[sbx].project_path`, or cwd.
- [x] Add tests for metadata load/save, corrupt JSON, missing file, and root selection precedence.

## Phase 2: protect project-scoped VM behavior

- [x] Validate VM project association before `_sync_existing_vm_start_config(...)` mutates mounts/port-forwards.
- [x] Fail with a clear message when current project root differs from saved VM root.
- [x] Record metadata when creating/recreating a named VM.
- [x] Record metadata after successful legacy reuse when metadata is missing.
- [x] Warn when a running VM's stored mounts differ from current config; do not stop, hot-sync, or rewrite mounts.
- [x] Read safe Git config with `git -C PROJECT_ROOT config --get KEY` when project root is known, falling back to global config.
- [x] Add tests for wrong-cwd mount protection, running-VM mount drift warning, and repo-local Git identity winning over global identity.

## Phase 3: owned `list` command

- [x] Add canonical `sbx list`; keep `sbx ls` as alias.
- [x] Replace `ls` passthrough with SmolVM Python API listing.
- [x] Merge SmolVM VM rows with sbx metadata by VM name.
- [x] Print table columns: `NAME STATUS PROJECT IMAGE SSH`; use `-` for unknown values.
- [x] Preserve `--all` / `-a` behavior for both `list` and `ls`.
- [x] Update completion and tests.

## Phase 4: canonical `remove` command

- [x] Add canonical `sbx remove`; keep `sbx rm` as alias.
- [x] Preserve optional `NAME` from `[sbx].name`, confirmation, and `--force` behavior.
- [x] Keep internal `_delete_vm(...)` unless already touched.
- [x] Update completion and tests.

## Phase 5: owned doctor and safe fixes

- [x] Add `sbx doctor --fix` flag.
- [x] Make `doctor` report sbx metadata problems, stale sessions/tunnels, and SmolVM `error` VMs.
- [x] Implement `--fix` for safe local bookkeeping only: stale metadata, corrupt metadata moved aside, stale session/tunnel records, and error-state bookkeeping repair.
- [x] Update error-state messages to recommend `sbx doctor --fix` before `recreate`.
- [x] Update boot-timeout hint to mention `sbx doctor` only for repeated failures.
- [x] Add tests proving `doctor --fix` does not start/stop/delete VMs or rewrite mounts.

## Phase 6: extract non-CLI modules

- [x] Move VM metadata helpers into `src/sbx/vm_metadata.py`: load, save, record, and validate project association.
- [x] Move session bookkeeping into `src/sbx/session_state.py`: session load/save, live filtering, registration, and unregistering.
- [x] Move VM state helpers into `src/sbx/vm_state.py`: VM listing, stored start config lookup, and error-state restart repair.
- [x] Move lifecycle/config warning helpers into `src/sbx/lifecycle_warnings.py`: existing VM config mismatches, local image warnings, and doctor config-state reporting.
- [x] Move safe doctor repair checks into `src/sbx/doctor.py`: metadata, sessions, tunnels, error VMs, and `run_doctor_checks(fix: bool)`.
- [x] Keep parser setup, CLI config resolution, `_project_identity`, and thin `cmd_*` wrappers in `src/sbx/cli.py`.
- [x] Update imports and tests without changing user-visible behavior.

## Phase 7: remove `--force-start`

- [x] Remove `--force-start` from `sbx run` and `sbx shell` parser setup.
- [x] Remove `--force-start` from bash/zsh/fish completions.
- [x] Remove `force_start` handling from `_start_existing_vm_if_needed(...)`; direct start should refuse SmolVM `error` VMs.
- [x] Keep SmolVM `error` bookkeeping repair in `sbx doctor --fix` only.
- [x] Update error-state tests to use doctor repair instead of forced start.
- [x] Add/keep tests proving `run`/`shell` reject `--force-start` as an unknown option.

## Phase 8: extract guest setup

- [x] Add `src/sbx/guest_setup.py`.
- [x] Reuse shared runtime SSH/process helpers instead of duplicating them.
- [x] Move host/env helpers: env-name validation, forwarded-env sanitizing, credential-free env, host timezone lookup, and host Git config rendering.
- [x] Move guest mutators: hostname setting, clock/timezone sync, Git config install, run-user preparation, and forwarded env sync.
- [x] Use one generic `attach(...)` helper for root/user attach.
- [x] Keep `_post_start_actions` in `src/sbx/cli.py`.
- [x] Update imports, call sites, and tests to patch/use `guest_setup` directly.
- [x] Run `ruff check src tests`.
- [x] Run `pytest --no-cov`.

## Final validation

- [x] Run `ruff check src tests`.
- [x] Run `pytest --no-cov`.
- [x] Smoke-check `sbx list`, `sbx ls`, `sbx remove --help`, `sbx rm --help`, `sbx doctor`, and `sbx doctor --fix`.
- [x] Re-run `ruff check src tests` after module extraction.
- [x] Re-run `pytest --no-cov` after module extraction.
- [x] Re-run `ruff check src tests` after removing `--force-start`.
- [x] Re-run `pytest --no-cov` after removing `--force-start`.
