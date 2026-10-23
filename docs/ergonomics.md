# sbx Ergonomics

This document captures the intended user experience for `sbx` commands and configuration.

## Goals

`sbx` should be project-context first:

- Use the current directory's `./.sbx.toml` as the project anchor.
- Avoid requiring users to repeat the sandbox name after a project is configured.
- Keep common workflows short: `sbx run`, `sbx shell`, `sbx doctor`.
- Prefer safe, explicit behavior for destructive actions.
- Make existing VM reuse visible when it can surprise users.

## Project context

`./.sbx.toml` describes the sandbox associated with a project. When `[sbx].name` is configured, commands that operate on a single sandbox should default to that name.

These commands should work without a positional `NAME` from the project directory:

```bash
sbx run
sbx create
sbx recreate --force
sbx shell
sbx stop
sbx rm --force
sbx network status
sbx network auth-port
sbx network close-auth-port
```

If a command needs a sandbox name and neither a positional name nor `[sbx].name` is available, `sbx` should print a clear error explaining that the command requires a VM name argument or `[sbx].name`.

## Command model

`sbx` uses user-intent commands rather than exposing raw VM lifecycle details:

| Command | Intent |
| --- | --- |
| `run` | Main workflow: create if missing, start if stopped, attach/run the agent. |
| `create` | Create/provision without attaching. |
| `recreate` | Destructively delete and create a fresh VM. |
| `shell` | Open an interactive shell for inspection/debugging. |
| `stop` | Stop without deleting VM state. |
| `rm` | Delete VM state. |
| `ls` / `list` | List all sandboxes; `--running` filters to running VMs. |
| `doctor` | Diagnose host backend and project/config state. |
| `network ...` | Expert helpers for auth callback tunnels and network status. |

`recreate` is intentionally separate from a possible future `restart`/`reboot`: `recreate` means delete the existing VM and create a fresh one, while restart/reboot would reuse the existing disk/state.

## Configuration vs command choice

Configuration should describe durable sandbox defaults. Command choice should describe what the user wants to do now.

Good durable config fields include:

```toml
[sbx]
name = "the-quest"
agent = "pi"
runtime_install = ["pi"]
image = "~/.smolvm/images/sbx"
memory = 8192
cpus = 4
disk_size = 40960
project_path = "."
writable_mounts = true
```

Examples of command/action concerns that should generally not be required in config:

- whether this invocation attaches
- whether this invocation recreates
- whether this invocation removes a VM
- `--force`
- `--debug`

Security and forwarding policy such as `auth_port`, `copy_host_credentials`, and `git_config` lives only in config so recreation is reproducible. `copy_host_credentials` remains false by default.

## Existing VM reuse

An existing named VM is reused as-is. Changing `.sbx.toml` does not implicitly mutate an existing VM's durable state. Apply user/tool provisioning changes explicitly with `sbx run --reprovision`.

For example, if `.sbx.toml` changes from:

```toml
disk_size = 81920
```

to:

```toml
disk_size = 10240
```

an already-created VM may still have an 81920 MiB disk. `sbx run` should reuse that VM rather than implicitly shrinking or recreating it.

`sbx doctor` should surface this mismatch:

```text
sbx config/state:
  warning: VM 'the-quest' already exists and differs from .sbx.toml:
    disk_size: config requests 10240 MiB, existing VM has 81920 MiB
  Existing VMs are reused as-is. Run `sbx recreate the-quest --force` to apply config changes.
```

When using a local image, `sbx doctor` should also warn if `[sbx].disk_size` is smaller than the local image rootfs. `sbx run` should fail early with the same explanation: set `disk_size` to at least the image rootfs size, remove `disk_size`, or rebuild the configured local image with a smaller rootfs.

## Guest setup lifecycle

Guest setup uses SSH and has three scopes:

| Scope | Operations |
| --- | --- |
| Creation or requested reprovisioning | Prepare the runtime account, install requested tools, and apply initial-state/Git configuration safeguards. |
| An sbx-managed start or requested reprovisioning | Restore hostname, synchronize clock, restore runtime-user KVM access, and start rootless Docker when supported by the image. |
| Each run/shell invocation | Forward selected environment variables, establish requested authentication tunnels, validate the command when applicable, and manage attachment/session cleanup. |

Reprovisioning is requested by `--reprovision` or CLI `--runtime-install`. A configured `runtime_install` list alone does not reprovision an existing VM. Starting and reprovisioning together performs boot setup once.

Attaching to an already-running VM does not repeat boot setup. This also means upgrading sbx does not immediately repair an old hostname, and Docker is not automatically restarted if it dies during a boot. Apply setup explicitly:

```bash
sbx run --reprovision --no-attach
# Or restart through sbx without deleting the VM disk:
sbx stop
sbx run
```

Boot setup targets the runtime user recorded for the VM, independently of the attachment user. If `sbx shell --root` starts a stopped VM, it still attempts KVM/Docker preparation for the recorded non-root user, then attaches as root. Explicitly root-provisioned VMs skip non-root KVM/Docker setup. Ordinary startup never creates a missing user; account creation is provisioning-only.

Hostname, clock, KVM, and Docker setup failures stop normal startup/provisioning before attachment or no-attach success. The VM remains running for inspection. Diagnostic `sbx shell --root` reports guest boot-setup failures as warnings and allows inspection; start, configuration, and trusted identity errors remain fatal.

There is no persistent boot-readiness tracking: a later ordinary attachment to that running VM does not retry failed setup and may proceed if provisioning eligibility checks pass. Use the explicit repair commands above. sbx also does not detect a reboot performed outside sbx when it next finds the VM already running.

## Shell completion

Completion should be static and project-context oriented.

`sbx completion bash`, `sbx completion zsh`, and `sbx completion fish` should complete commands, options, and small enum values such as agents and shell names.

Completion should not need VM-name lookup. The project context should make VM names optional for normal workflows.

## Auto-create `.sbx.toml`

`sbx run` and `sbx create` can bootstrap project config automatically.

Behavior:

- If `./.sbx.toml` does not exist and a new VM is created, write `.sbx.toml` by default.
- If `./.sbx.toml` does not exist and an existing VM is reused, write only when `--write-config` is passed.
- If `./.sbx.toml` exists, never modify it by default.
- If `./.sbx.toml` exists and `--write-config` is passed, add missing durable keys only.
- Never overwrite existing values.

Example:

```bash
sbx run the-quest --image ~/.smolvm/images/sbx --memory 8192 --cpus 4 --disk-size 40960 --project-path . --runtime-install pi
```

could create:

```toml
[sbx]
name = "the-quest"
runtime_install = ["pi"]
image = "~/.smolvm/images/sbx"
memory = 8192
cpus = 4
disk_size = 40960
project_path = "."
```

On initial creation, `sbx` prints the generated config and the next `run`, `shell`, `stop`, and `rm` commands. After that, project workflows become:

```bash
sbx run
sbx shell
sbx doctor
sbx network status
```
