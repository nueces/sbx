# sbx design

## Intent

`sbx` is a command-line tool for running coding agents inside disposable virtual-machine sandboxes.

The design goal is simple: let a user run powerful agents against a project while keeping host credentials, host processes, and host package execution outside the agent environment by default.

An implementation of this design must provide:

1. a small user-facing CLI over a VM backend,
2. safe-by-default credential and environment handling,
3. predictable project mounts,
4. agent attach/session behavior,
5. optional local ready-to-run images, including Docker-capable images.

This document is the product/behavior contract. It should be usable to reimplement `sbx` in another language without copying the current Python code structure.

## Non-goals

`sbx` is not intended to be:

- a general VM manager,
- a full SmolVM API wrapper,
- a Docker socket passthrough helper,
- a secrets manager,
- a package publisher,
- a CI/release orchestration product.

Project release automation is documented separately in `project-release-infrastructure.md`.

## Core model

An `sbx` sandbox is a named VM with:

- a selected coding agent,
- mounted host project paths,
- optional local image metadata,
- optional guest user configuration,
- optional auth callback forwarding,
- local session tracking for stop-on-exit behavior.

The default agent is `pi`. Supported agents are:

```text
pi
claude
codex
```

The VM backend is QEMU through SmolVM. Other backends are out of scope until explicitly designed.

## Backend integration contract

When SmolVM exposes a stable Python API for an operation, `sbx` should use that API directly instead of shelling out to the SmolVM CLI. This is especially required for operations that pass structured data or secrets, because command-line arguments can leak through shell history or process listings.

SmolVM subprocess calls are allowed only when the Python API is unavailable, when preserving interactive terminal behavior is simpler and safe, or when the command is intentionally a CLI passthrough. Subprocess calls must not place secret values in argv.

## Command contract

The CLI must expose user-intent commands instead of mirroring every VM backend command.

| Command | Required behavior |
| --- | --- |
| `sbx run [NAME]` | Create the sandbox if missing, start it if stopped, expose auth callback unless disabled, then attach to the selected agent. |
| `sbx create [NAME]` | Create/provision/start the sandbox without attaching and without auth callback forwarding by default. |
| `sbx recreate [NAME]` | Destroy the sandbox, then create it again. Must require confirmation unless forced. |
| `sbx rm [NAME]` | Destroy the sandbox. Must require confirmation unless forced. |
| `sbx stop [NAME]` | Stop the sandbox without removing disk/state. |
| `sbx shell [NAME]` | Open an interactive shell in the sandbox. |
| `sbx ls` | List sandboxes. `--all`/`-a` includes stopped sandboxes. |
| `sbx network auth-port [NAME]` | Expose a guest callback port to host localhost. |
| `sbx network close-auth-port [NAME]` | Close the tracked callback tunnel. |
| `sbx network status [NAME]` | Show sandbox networking and auth callback tunnel state. |
| `sbx image build-debian` | Build a local ready-to-run Debian/Pi image. |
| `sbx image ls` | List local ready-to-run images. |
| `sbx doctor` | Run non-sudo diagnostics for the configured backend. |
| `sbx completion SHELL` | Print shell completion for supported shells. |
| `sbx --version` | Print the package/tool version. |

For commands that operate on one sandbox, `NAME` may be omitted only when configuration provides `[sbx].name`.

`run`, `create`, and `recreate` must accept both explicit `--name NAME` and positional `NAME` forms.

## Configuration contract

Configuration must be TOML. Implementations must read and merge, from lowest to highest precedence:

1. user defaults: `~/.config/sbx/config.toml`,
2. project defaults: `./.sbx.toml` from the current working directory,
3. explicit config: `--config PATH`,
4. CLI flags.

The main table is `[sbx]`.

Supported keys:

| Key | Meaning |
| --- | --- |
| `agent` | One of `pi`, `claude`, `codex`; default `pi`. |
| `name` | Default sandbox name. |
| `memory` | VM memory in MiB. |
| `cpus` | Virtual CPU count. Must be validated to a sane positive range. |
| `disk_size` | Disk size in MiB for created VMs. |
| `os` | Guest OS preset where supported by the backend. |
| `install_timeout` | Agent preset install timeout. |
| `boot_timeout` | VM boot/SSH timeout; must be greater than zero. |
| `image` | Local ready-to-run image directory. |
| `mount` | String or list of `HOST` / `HOST:GUEST` mount specs. |
| `project_path` | Primary project mount and attached working directory. |
| `writable_mounts` | Global writable mount flag. |
| `run_user` | Guest user used for attached agent/shell commands. |
| `auth_port` | Whether `run` exposes the callback port by default. |
| `auth_host_port` | Host localhost callback port; default `1455`. |
| `auth_guest_port` | Guest callback port; default `1455`. |
| `stop_on_exit` | Stop VM when last tracked `sbx` session exits. |
| `copy_host_credentials` | Allow backend preset credential copying; default false. |
| `env` | Explicit host environment variable allowlist. |
| `git_config` | Copy safe host Git config into the guest; default true. |

When `run` or `create` creates a new named VM and `./.sbx.toml` does not exist, the implementation should write a minimal project config containing the resolved sandbox defaults. Existing project config must not be overwritten; it may be extended only when explicitly requested.

Invalid config must fail before destructive or VM-starting actions.

## Mount contract

Mount behavior must be deterministic and independent of backend defaults.

Rules:

- `project_path` is mounted first.
- `project_path` is resolved to an absolute host path.
- `project_path` is mounted at the same absolute path inside the guest.
- `project_path` is used as the attached agent/shell working directory.
- Bare `mount` entries such as `/host/dir` also mount at the same absolute guest path.
- Explicit `HOST:GUEST` mount entries preserve the configured guest path.
- Host paths must exist and be directories.
- Guest paths must be absolute.
- Duplicate guest paths are invalid.
- `project_path` forces mounts writable.
- Otherwise writability follows `writable_mounts`.

When starting an already-created stopped VM, the implementation must sync the stored mount list from current config before start. It must not recreate the VM just to update mounts. It must not hot-update mounts on a running VM.

## Credential and environment contract

Host credentials must not be visible to the guest by default.

When `copy_host_credentials` is false, any backend preset/install process must run in a credential-isolated environment. The implementation may still expose backend state/cache paths required for VM operation, but normal host credential files must not be reachable through `HOME`.

Host environment forwarding must be explicit. Known agent credential variables such as:

```text
ANTHROPIC_API_KEY
OPENAI_API_KEY
```

must be removed unless the user includes them through `--env KEY` or `[sbx].env`.

Environment variable names must be validated before use.

When `sbx run` reuses an existing VM or `sbx shell` attaches to a VM, configured forwarded environment variables should be synchronized into the guest before attach. The implementation should use SmolVM's Python API for this sync, not `smolvm sandbox env set KEY=value`, so secret values are not exposed in command history or process listings.

## Git config contract

By default, attached agent/shell sessions should receive only safe Git identity/config from the host. The allowed keys are:

```text
user.name
user.email
init.defaultBranch
pull.rebase
push.default
core.autocrlf
core.eol
```

No credential helpers, signing keys, include paths, or arbitrary Git config should be copied by default.

Users must be able to disable this behavior with CLI/config.

## VM lifecycle contract

For a named VM:

- if it exists and is stopped, `run` starts it;
- if it exists and is running, `run` reuses it;
- if it does not exist, `run` creates it;
- `create` creates/provisions without attaching;
- `recreate` destroys then creates;
- `rm` destroys;
- destructive commands require confirmation unless forced;
- destructive commands must refuse non-interactive confirmation unless forced.

When a VM fails to boot before timeout, the tool should produce a user-facing hint if the VM appears to still be running.

## Agent attach contract

After the VM is ready, `run` attaches to the selected agent command:

| Agent | Guest command |
| --- | --- |
| `pi` | `pi` |
| `claude` | `claude` |
| `codex` | `codex` |

If `run_user` is configured, the implementation must create/use that guest user before attaching and run the agent as that user. Without `run_user`, attach may run as the backend default/root user.

`shell` follows the same user/working-directory/Git-config model but runs a shell command instead of an agent command. `shell --root` must ignore configured `run_user`.

## Auth callback port contract

`run` exposes the agent OAuth callback port by default. Defaults:

```text
host localhost port: 1455
guest port: 1455
```

The implementation must track tunnels it opens so it can:

- avoid duplicate tunnels,
- close a tracked tunnel,
- report active/inactive/busy-untracked status,
- replace an existing tracked tunnel when explicitly requested.

The tunnel must bind host localhost only.

## Session and stop-on-exit contract

Attached `run` and `shell` sessions must be tracked locally per VM.

When a tracked session exits:

1. unregister the session,
2. if `stop_on_exit` is true and no other tracked sessions remain for the VM, stop the VM,
3. if `--keep-running` was used for that session, leave the VM running.

This prevents one terminal from stopping a VM that another active `sbx` session still uses.

## Local image contract

A local ready-to-run image is a directory containing `smolvm-image.json`.

The manifest must identify kernel and rootfs paths. Relative paths are resolved relative to the image directory.

The manifest may contain an `sbx` object:

```json
{
  "sbx": {
    "agent": "pi",
    "features": [],
    "launch_command": "pi"
  }
}
```

Rules:

- if `sbx.agent` is present, it must match the selected agent;
- `sbx.launch_command` overrides the default agent command for local-image attach;
- `sbx.features` is a list of strings;
- non-Docker images use `features: []`;
- Docker-capable images use `features: ["docker"]`.

`sbx image ls` must list image manifests from the local image cache and include at least name, path, agent, features, kernel, and rootfs. It must support JSON output for tooling.

## Debian/Pi image builder contract

`sbx image build-debian` builds local ready-to-run images from packaged resources, not from repository-relative paths.

The default image composition is:

```text
Debian base layer
Pi/agent tooling layer
```

With Docker support, the composition is:

```text
Debian base layer
Docker layer
Pi/agent tooling layer
```

Docker support is opt-in. Normal images should not include Docker packages or Docker kernel changes.

A Docker-capable image must:

- install Docker CE packages from Docker's Debian repository,
- include rootless Docker dependencies,
- configure subordinate uid/gid ranges for the agent user,
- start rootless Docker at guest boot,
- build/use a Docker-capable kernel,
- write `sbx.features = ["docker"]` in the image manifest.

The Docker-capable kernel is built from pinned SmolVM kernel inputs plus sbx's packaged Docker kernel fragment. The resulting kernel is stored in the image directory and referenced from the manifest.

## Backend integration contract

The current backend is SmolVM. The implementation must use the installed SmolVM package in the same runtime environment as `sbx`, not an unrelated global executable.

Lifecycle commands should map to SmolVM sandbox commands:

```text
sandbox info NAME --json
sandbox list [--all]
sandbox start NAME --boot-timeout N
sandbox stop NAME
sandbox delete NAME --json
sandbox ssh NAME
```

The product CLI should remain stable even if backend invocation details change internally.

## Diagnostics and completion

`doctor` must run non-sudo diagnostics for the supported backend and report configuration/image warnings useful before starting a VM.

`completion` must generate static shell completion for supported shells. Supported shells are defined by the implementation, currently bash, zsh, and fish.

## Test and tooling contract

The project must have automated tests for:

- config parsing/precedence,
- command mapping to backend lifecycle operations,
- mount normalization and existing-VM mount sync,
- credential isolation and explicit environment forwarding,
- auth tunnel tracking,
- image listing/build metadata,
- release bump scripts where those scripts exist in the project infrastructure.

The Python implementation uses `pytest`, `ruff`, and `pre-commit`. Agent/local runs should keep virtual environments outside worktrees, for example:

```bash
UV_PROJECT_ENVIRONMENT=/home/agent/venv/<feature> uv run --python 3.12 --extra dev ...
```
