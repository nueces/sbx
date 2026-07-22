# sbx project association fixes design

## Intent

Prevent existing named VMs from being silently reconfigured by the wrong project and make sbx commands show/use project context consistently:

1. stopped VM mounts/port-forwards can be rewritten from the wrong working directory,
2. `sbx list`/`sbx ls` cannot show project directory while it is only a SmolVM CLI passthrough,
3. host Git identity forwarding ignores repo-local config.

This is a design only. No implementation is included here.

## Problem 1: stopped VM config can be rewritten from the wrong project

### Current behavior

`sbx` loads project config from the current directory's `.sbx.toml`. Relative paths are resolved against `Path.cwd()`. When a named VM already exists and is stopped, `sbx run` syncs configured mounts/port-forwards into SmolVM's stored VM config before starting it.

That means two directories can accidentally target the same VM name:

```text
/home/nueces/code/dt/.sbx.toml      name = "dt", project_path = "."
/tmp/other/.sbx.toml                name = "dt", project_path = "."
```

Running `sbx run dt` from `/tmp/other` can rewrite the stopped `dt` VM mounts away from `/home/nueces/code/dt`.

### Desired behavior

A named VM must have an sbx-owned project association. Once associated, only the same project/config origin may update its sbx-managed start config.

If a different project attempts to sync mounts/port-forwards for that VM, fail before modifying state:

```text
sbx: VM 'dt' belongs to /home/nueces/code/dt; refusing to update it from /tmp/other
```

Starting/attaching without a conflicting sync should continue to work.

### State model

Use sbx-owned JSON state, not SmolVM's database:

```text
~/.local/state/sbx/vms.json
```

Shape:

```json
{
  "dt": {
    "project_root": "/home/nueces/code/dt",
    "config_path": "/home/nueces/code/dt/.sbx.toml"
  }
}
```

Required fields:

- `project_root`: canonical absolute directory used as the project/config origin.
- `config_path`: canonical absolute path to the project config, when one exists.

Do not add SQLite unless JSON becomes insufficient.

### Project root selection

Pick one root, in this order:

1. directory containing explicit `--config PATH`, if provided,
2. directory containing the local `.sbx.toml` that was loaded,
3. resolved `[sbx].project_path`, if set and no local config exists,
4. current working directory, only as a compatibility fallback.

Store canonical absolute paths using `resolve(strict=False)`.

### Write rules

Record/update the association when:

- creating a named VM,
- recreating a named VM,
- successfully running an existing named VM with no prior metadata and no conflict.

Do not write metadata for unnamed/ad-hoc VMs unless SmolVM returns a stable VM name and sbx is managing it.

### Validation rules

Before `_sync_existing_vm_start_config(...)` changes mounts or port-forwards:

1. load `vms.json`,
2. if VM has metadata and current project root differs: fail,
3. if VM has no metadata: allow once for backward compatibility, then save current association after success,
4. if metadata is corrupt/unreadable: fail closed with a repair hint.

Repair stale/corrupt metadata with `sbx doctor --fix`; see the doctor section below.

### Existing SmolVM state

Do not add columns/tables to SmolVM's DB. It is backend-owned state and may change independently. sbx should only mutate SmolVM config through the already existing mount/port-forward sync path.

## Problem 2: `sbx list` cannot show project context while it is a passthrough

### Current behavior

`sbx ls` delegates to SmolVM's CLI:

```text
smolvm sandbox list
```

That output can only show SmolVM-owned fields. It cannot include sbx-owned metadata such as project root/config path.

### Desired behavior

`sbx list` should become an sbx-owned command that combines:

1. structured VM data from SmolVM's Python API,
2. sbx VM metadata from `~/.local/state/sbx/vms.json`.

Do not parse SmolVM CLI table output. Do not read SmolVM's DB directly unless the Python API lacks a required field.

### Output

Keep the table small:

```text
NAME   STATUS   PROJECT                 IMAGE              SSH
 dt    running  /home/nueces/code/dt    debian-sbx-docker  2204
```

Use `-` for unknown values. `PROJECT` comes from sbx metadata; `IMAGE` and `SSH` come from SmolVM VM info when available.

### Command naming and `--all` behavior

`sbx list` is canonical; `sbx ls` is a compatibility alias. Both show active/running VMs by default and include stopped VMs with `--all` / `-a`.

The filtering should happen against structured SmolVM VM status values, not by parsing text.

### API preference

Use the SmolVM Python API list operation and merge rows by VM name. If SmolVM exposes multiple API surfaces, prefer the stable public facade. Avoid private DB schema reads.

## Problem 3: `rm` should have a canonical full-word command

### Current behavior

`sbx rm` is the only remove command.

### Desired behavior

Add `sbx remove` as the canonical command and keep `sbx rm` as an alias. Both must behave identically, including optional `NAME` from `[sbx].name`, confirmation, and `--force`.

Keep internal helper names boring; `_delete_vm(...)` does not need to be renamed unless the code is already being touched.

## Problem 4: Git identity forwarding misses repo-local config

### Current behavior

`sbx` forwards safe Git config by reading only global values:

```text
git config --global --get user.name
git config --global --get user.email
```

This misses repo-local identity configured in the mounted project.

### Desired behavior

When a project root is known, read effective Git values using Git's normal precedence:

```text
git -C PROJECT_ROOT config --get KEY
```

Fallback to global behavior only when no project root is known or the repo lookup fails.

Keep the existing allowlist only:

```text
user.name
user.email
init.defaultBranch
pull.rebase
push.default
core.autocrlf
core.eol
```

No credential helpers, signing keys, include paths, or arbitrary config.

### Minimal API change

Change the internal helper from:

```text
_host_git_config()
```

to accept an optional project root:

```text
_host_git_config(project_root: Path | None = None)
```

Callers pass the same project root computed for VM association.

## Doctor repair

`sbx doctor` should report stale/corrupt sbx metadata and SmolVM VMs stuck in `error` state. `sbx doctor --fix` may repair safe local bookkeeping only: remove stale sbx metadata, move corrupt metadata aside, mark SmolVM `error` VMs as stopped using the existing internal restart repair, and clean stale session/tunnel records.

It must not start/stop VMs, delete VMs, mutate guest disks, rewrite mounts, or change credentials.

Error-state commands should recommend doctor before destructive recovery:

```text
sbx: VM 'dt' is in error state.
sbx: Run `sbx doctor --fix` to repair local VM bookkeeping, then retry `sbx run dt`.
sbx: If it still fails, run `sbx recreate dt --force`.
```

Boot-timeout hints should mention `sbx doctor` only for repeated failures.

## Remove `--force-start`

`sbx doctor --fix` replaces `--force-start`. `sbx run` and `sbx shell` must not repair SmolVM `error` state inline. If a VM is in `error`, they should refuse to start it and print the doctor flow above.

Remove `--force-start` from `run`, `shell`, completions, and tests. Keep the error-state bookkeeping repair in `doctor --fix` only.

## Running VM mount drift

Do not hot-reload mounts in a running VM and do not stop a running VM automatically. Other terminals may already be using it.

Default behavior for `sbx run` and `sbx shell`:

- stopped VM: sync stored mounts as today, then start,
- running VM with matching mounts: attach normally,
- running VM with different mounts: warn, then continue attaching with the VM's existing mounts.

Warning shape:

```text
sbx: VM 'dt' is running with mounts that differ from current config.
sbx: Current session will use the VM's existing mounts.
sbx: Run `sbx stop dt` then `sbx run dt` to apply config mounts.
```

No extra flag is needed unless users later ask for strict failure behavior.

## Module extraction plan

Keep `cli.py` as command glue, not the owner of state and repair logic. Extract in this order so new modules do not import `cli.py`:

1. `src/sbx/vm_metadata.py`
   - `load_vm_metadata`
   - `save_vm_metadata`
   - `record_vm_project`
   - `validate_vm_project`
2. `src/sbx/session_state.py`
   - `load_sessions`
   - `save_sessions`
   - `live_sessions`
   - `active_sessions`
   - `register_session`
   - `unregister_session`
3. `src/sbx/vm_state.py`
   - `smolvm_vms`
   - `existing_vm_start_config`
   - `mark_error_vm_stopped_for_restart`
4. `src/sbx/lifecycle_warnings.py`
   - `existing_vm_config_mismatches`
   - `local_image_config_warnings`
   - `doctor_config_state`
   - helper functions only needed by those warnings
5. `src/sbx/doctor.py`
   - `doctor_metadata`
   - `doctor_sessions`
   - `doctor_tunnels`
   - `doctor_error_vms`
   - `run_doctor_checks(fix: bool) -> int`
6. `src/sbx/guest_setup.py`
   - host-derived setup: env validation/sanitizing, credential-free env, host Git config, host timezone lookup
   - guest-applied setup: hostname, clock/timezone sync, Git config install, run-user preparation, forwarded env sync
   - one generic `attach(...)` helper using the shared runtime SSH command

Leave CLI config resolution, parser setup, and `cmd_*` wrappers in `cli.py`. Keep project identity resolution in `cli.py` unless config loading is extracted later. Keep `_post_start_actions` in `cli.py` because it orchestrates auth-port, session tracking, attach, and stop policy.

`cmd_doctor` should stay small:

```python
def cmd_doctor(args):
    rc = runtime.run_smolvm(["doctor", "--backend", DEFAULT_BACKEND])
    lifecycle_warnings.doctor_config_state(args.config_data)
    return rc or doctor.run_doctor_checks(fix=args.fix)
```

## Suggested implementation order

1. Add sbx VM metadata JSON helpers and tests.
2. Validate project association before stopped-VM mount/port-forward sync.
3. Record association on create/recreate/successful legacy reuse.
4. Add canonical `sbx list`, keep `sbx ls` as an alias, and replace the passthrough with Python API listing merged with sbx metadata.
5. Add canonical `sbx remove`, keeping `sbx rm` as an alias.
6. Thread project root into Git config forwarding and use `git -C`.
7. Extract reusable non-CLI modules in the order above.
8. Add `sbx doctor --fix` for safe metadata/error-state repairs using the extracted modules.
9. Remove `--force-start`; use `sbx doctor --fix` as the only error-state repair path.
10. Extract guest setup helpers so `cli.py` only decides when to mutate or attach to the guest.

## Non-goals

- No hot mount reload for running VMs.
- No SmolVM CLI parsing for `sbx list`/`sbx ls`.
- No SmolVM DB schema changes.
- No new database unless JSON state proves insufficient.
- No broad config refactor.
- No new public config keys for Git identity.
- No top-level `reassociate` command.
