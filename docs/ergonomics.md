# sbx Ergonomics

This document captures the intended user experience for `sbx` commands and configuration.

## Goals

`sbx` should be project-context first:

- Use the nearest `.sbx.toml` in the current directory or its ancestors as the project anchor.
- Avoid requiring users to repeat the sandbox name after a project is configured.
- Keep common workflows short: `sbx run`, `sbx shell`, `sbx doctor`.
- Prefer safe, explicit behavior for destructive actions.
- Make existing VM reuse visible when it can surprise users.

## Project context

The nearest `.sbx.toml` describes the sandbox associated with a project. When `[sbx].name` is configured, all config-consuming commands, including stop/remove, default to it unless an explicit name is given. Nested configs select the nearest project; ancestor configs are not merged. Session-directory mapping uses the most-specific host mount; overlapping guest targets remain user-controlled and can intentionally expose different content. No guest-shadowing guard is applied. Relative paths belong to the config file that supplies them, while CLI-relative paths belong to invocation cwd.

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

Guest bootstrap uses the configured control transport (SSH for the curated image) and has three normal-operation scopes:

SSH requires explicit VM metadata (`comm_channel="ssh"`, `ssh_capable=true`, and the injected public key) and the matching existing local key pair. Local-image and preset creation record this metadata. Bootstrap, command checks, interactive shells and SSH tunnels do not discover another identity or generate replacement keys for existing VMs. Legacy unset-channel VMs must be explicitly recreated (which deletes their disk). For missing or mismatched keys, restore the original pair or recreate the VM; existing VM keys are never replaced automatically. Explicit vsock control remains a separate transport, not a legacy SSH fallback.

| Scope | Operations |
| --- | --- |
| Creation or requested reprovisioning | Prepare the runtime account, apply optional initial-state copying, authorize the existing public key for user SSH, install requested tools, and apply Git configuration safeguards. |
| A normal sbx-managed start or requested reprovisioning | Check/create the runtime account and home before workspace targets/mounts; restore hostname/clock and optional runtime-user KVM/Docker helpers. |
| Each run/shell invocation | Forward selected environment variables, establish requested authentication tunnels, validate the command when applicable, and manage attachment/session cleanup. |

Normal attachment authenticates directly as the recorded runtime user. Existing VMs without user authorization need `sbx run --reprovision --no-attach`; an ordinary restart does not install user keys. Key installation follows `ssh-copy-id`: append absent identities on a fresh line without rewriting existing entries. Key comments are not parsed as shell syntax, and existing restrictions are preserved. Public-key login policy belongs to the image/sshd, not an SBX key-options interpreter. SSH errors never retry as root. Interactive attachment uses the guest-selected login shell after an initial project `cd`; later user startup directory changes are respected. Agent attachment uses a non-interactive remote `cd ... && exec ...`. SBX leaves profile loading to the packaged shell.

Reprovisioning is requested by `--reprovision` or CLI `--runtime-install`. A configured `runtime_install` list alone does not reprovision an existing VM. Starting and reprovisioning together performs boot setup once.

Attaching to an already-running VM does not repeat boot setup. This also means upgrading sbx does not immediately repair an old hostname, and Docker is not automatically restarted if it dies during a boot. Apply setup explicitly:

```bash
sbx run --reprovision --no-attach
# Or restart through sbx without deleting the VM disk:
sbx stop
sbx run
```

Normal boot setup uses the recorded runtime identity. If neither account nor intended home exists, standard account creation initializes the home before workspace target creation. An existing account with a missing home, or an existing home without its intended account, requires explicit repair; sbx does not adopt/chown the home, reset existing UID/GID, or repair dotfiles. Initial installs/copies are not repeated on an ordinary restart.

Diagnostic `sbx shell --root` bypasses mandatory account setup, environment synchronization, guest workspace mounting, hostname/clock and KVM/Docker helpers. It opens from a safe default directory even if `project_path` is configured but absent in the guest. It never marks provisioning successful; host startup, configuration and trusted identity checks still apply. After a diagnostic boot, explicitly reprovision or stop/start normally before expecting workspace/boot setup to be present.

Hostname, clock, account, mount and helper failures stop normal startup/provisioning before attachment or no-attach success. The VM remains available for root inspection and explicit repair/retry.

Supported images must provide the kernel support for 9p workspaces and, when used, overlays. Startup may load modules already included in the image; it never installs kernel packages or repairs an unsupported image. Missing support fails before workspace targets are created, with instructions to rebuild a supported image.

Nested KVM is optional. Default images omit its helper, so the KVM phase skips KVM-specific work without requiring guest `/dev/kvm`. Images built with `--nested-kvm` include the kernel support and helper; if that helper cannot access `/dev/kvm`, normal setup fails with host/CPU-exposure guidance. Root runtime users skip the non-root helper.

There is no persistent boot-readiness tracking: a later ordinary attachment to that running VM does not retry failed setup and may proceed if provisioning eligibility checks pass. Use the explicit repair commands above. sbx also does not detect a reboot performed outside sbx when it next finds the VM already running.

### Current limitation after interrupted provisioning

Environment updates check the guest execution lock but do not hold it throughout the managed-environment API call. Host locks serialize normal CLI mutations; after a CLI disconnect, however, a surviving guest phase can acquire its lock between that check and the update. An installer may then consume unintended environment values, including forwarded credential settings. This remains a deferred limitation for the single-operator, disposable-VM workflow, not a guarantee of race-free recovery.

Do not immediately retry environment/provisioning changes after interruption while earlier work may still run. Wait for it to finish or reach its existing deadline; inspect with `sbx shell NAME --root --keep-running` without synchronizing environment or automatically stopping on exit. If state is uncertain, explicitly stop/recreate the disposable VM after preserving needed guest-only data; recreation deletes its disk. A dependency upgrade alone is not known to fix this limitation.

### Automatic stop versus explicit stop

Automatic stop-on-exit applies only when the VM is idle. Already-active sessions suppress both flushing and stopping. If a new session is admitted during the bounded readiness/filesystem flush, automatic stop is canceled, even if the flush failed. Session registration and the final idle decision/stop are coordinated without holding the session registry lock through interactive use or provisioning. If stop commits first, a competing attachment waits and checks VM state again before launch; retry the run if the VM stopped.

An explicit `sbx stop` is a direct request, not an idle-only policy. It may stop a VM with active sessions and disconnect a simultaneous new attachment. Flush failure/timeout warns but does not suppress that explicit stop or replace its result. Best-effort flushing is not guest shutdown and does not guarantee crash durability or cover writes made afterward.

## Shell completion

Completion should be static and project-context oriented.

`sbx completion bash`, `sbx completion zsh`, and `sbx completion fish` should complete commands, options, and small enum values such as agents and shell names.

Completion should not need VM-name lookup. The project context should make VM names optional for normal workflows.

## Auto-create `.sbx.toml`

`sbx run` and `sbx create` can bootstrap project config automatically.

Behavior:

- If no project/explicit config was selected and a new VM is created, write local `.sbx.toml` by default. Never automatically create a child config that shadows a selected ancestor.
- If `./.sbx.toml` does not exist and an existing VM is reused, write only when `--write-config` is passed.
- If `./.sbx.toml` exists, never modify it by default.
- If `./.sbx.toml` exists and `--write-config` is passed, add missing durable keys only.
- Explicit `--write-config` creation establishes the local config's project identity before creating/claiming a VM. Choose a new VM name when creating a local project below an ancestor; selecting the ancestor-owned VM fails before file or VM changes, including recreate deletion. Generated local defaults retain inherited choices and CLI overrides without rebasing source-relative paths.
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
