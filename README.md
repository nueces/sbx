# sbx

`sbx` runs coding agents inside disposable [SmolVM](https://github.com/CelestoAI/smolVM) virtual machines.

Agents and authentication run in the VM, not on the host. By default `sbx` does **not** copy host credentials into the guest. Use `--copy-host-credentials` only when you explicitly want SmolVM presets to forward local agent configs/API keys.

## Install

```bash
uv tool install --editable .
uv tool install 'smolvm==0.0.19'
sbx doctor
```

`sbx` currently supports SmolVM `0.0.19`. Newer SmolVM versions changed APIs/behavior that `sbx` still needs to support. Until that compatibility work is done, install both `sbx` and `smolvm` with the pinned SmolVM version via `uv tool install`.

## Usage

```bash
# Run an agent session. Creates the sandbox if missing, starts it if stopped,
# exposes the OAuth callback port, then attaches to Pi.
sbx run my-sbx

# Create/provision only; do not attach and do not open auth forwarding.
sbx create my-sbx

# Open a shell in the sandbox. NAME defaults to [sbx].name when configured.
# If [sbx].run_user is set, the shell opens as that user; use --root to override.
# If [sbx].project_path is set, the shell starts in that mounted directory.
sbx shell my-sbx
sbx shell
sbx shell --root

# Lifecycle helpers.
sbx ls
sbx stop my-sbx
sbx rm my-sbx --force
sbx recreate my-sbx --force
```

### Shell completion

Generate static shell completion scripts with:

```bash
# bash
sbx completion bash

# zsh
sbx completion zsh

# fish
sbx completion fish
```

For one-off use, run `eval "$(sbx completion bash)"` or `eval "$(sbx completion zsh)"`; in fish, run `sbx completion fish | source`.

Common options:

```bash
# Mount a project at the same absolute path in the guest as read-write,
# and start the attached agent in that mounted directory.
sbx run my-sbx --project-path .

# Disable automatic OAuth callback forwarding.
sbx run my-sbx --no-auth-port

# Keep the VM running after the agent/shell exits.
sbx run my-sbx --keep-running
sbx shell my-sbx --keep-running

# Control project config bootstrap.
sbx run my-sbx --write-config
sbx create my-sbx --no-write-config

# Run the attached agent process as a non-root guest user.
sbx run my-sbx --run-user agent

# Explicitly allow copying host agent credentials/configs into the guest.
sbx run my-sbx --copy-host-credentials

# Explicitly forward a selected host environment variable into the guest.
sbx run my-sbx --env OPENAI_API_KEY

# Choose another agent preset.
sbx run my-sbx --agent codex
sbx run my-sbx --agent claude
```

## Command model

`sbx` uses user-intent commands rather than mirroring SmolVM VM lifecycle names:

| Command                          | Meaning                                                                                  |
| -------------------------------- | ---------------------------------------------------------------------------------------- |
| `run [NAME]`                     | Main workflow: create if missing, start if stopped, attach/run the agent.                |
| `create [NAME]`                  | Create/provision a sandbox without attaching.                                            |
| `recreate [NAME]`                | Destructively remove and create a fresh sandbox. Confirmation required unless `--force`. |
| `rm [NAME]`                      | Remove a sandbox. Confirmation required unless `--force`.                                |
| `stop [NAME]`                    | Stop a sandbox without removing it.                                                      |
| `shell [NAME]`                   | Open a shell in a sandbox.                                                               |
| `ls`                             | List running sandboxes. Use `ls -a` / `ls --all` to include stopped ones.                |
| `network status [NAME]`          | Expert helper: show sandbox networking and auth callback tunnel status.                  |
| `network auth-port [NAME]`       | Expert helper: manually expose the OAuth callback port for an already-running sandbox.   |
| `network close-auth-port [NAME]` | Expert helper: close the tracked OAuth callback tunnel.                                  |
| `image build-debian`             | Advanced helper: build a local Debian/Pi image, optionally with `--with-docker`.         |
| `doctor`                         | Run non-sudo SmolVM diagnostics for QEMU.                                                |
| `completion SHELL`               | Generate shell completion for `bash`, `zsh`, or `fish`.                                  |

When `NAME` is omitted, commands that operate on one sandbox use `[sbx].name` from configuration.

We intentionally keep `recreate` separate from a possible future `restart`/`reboot`: `recreate` means delete and create a fresh VM, while a soft restart would reuse the same VM disk/state.

## Development

For a fresh checkout or agent environment, create an isolated test venv outside the
repository. This avoids accidentally reusing a `.venv` copied from another machine
and keeps test dependencies reproducible:

```bash
cd /path/to/sbx/main
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov
```

Useful follow-up commands:

```bash
# Run the full test suite.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov

# Run focused tests while iterating.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov tests/test_cli.py

# Lint/format.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev ruff check .
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev ruff format .
```

Before making code changes in a fresh environment, first run the full test suite
once with the command above to verify the checkout and tool environment are
healthy. After changing CLI behavior, add/update focused tests and run both the
focused tests and the full suite.

## Configuration

`sbx` reads TOML configuration from these locations, merging later files over earlier files:

1. `~/.config/sbx/config.toml` — user defaults
2. `./.sbx.toml` — project defaults from the directory where `sbx` is executed
3. `--config PATH` — explicit override file

CLI flags always override config values.

Configuration should describe the sandbox, while command choice describes the action. For example, use `sbx run` to attach and `sbx create` to create without attaching, rather than relying on config to change interaction style.

Example `.sbx.toml`:

```toml
[sbx]
agent = "pi" # pi, claude, or codex
name = "project-sbx"
memory = 4096
cpus = 2
disk_size = 20480
# QEMU is the only supported backend for now.
backend = "qemu"
os = "ubuntu"
install_timeout = 600
boot_timeout = 60

# Optional: use a local ready-to-run image directory with smolvm-image.json.
# image = "./images/debian-pi"

mount = [".:/workspace"]
project_path = "."
writable_mounts = true
run_user = "agent"

auth_port = true
auth_host_port = 1455
auth_guest_port = 1455

# Stop the VM when an sbx run/shell session exits and no other sbx sessions remain.
stop_on_exit = true

# False by default. Keep host credential files out of the guest unless explicitly allowed.
copy_host_credentials = false

# Empty by default. Host environment variables are not forwarded unless listed here
# or passed with `--env KEY`.
env = []

# True by default. Copies safe Git identity/config only, not credentials.
git_config = true
```

### Config reference

| Section | Key                     | CLI flag                                                 | Description                                                                                                                                                |
| ------- | ----------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `[sbx]` | `agent`                 | `--agent`                                                | Default agent: `pi`, `claude`, or `codex`.                                                                                                                 |
| `[sbx]` | `name`                  | `--name` or positional `NAME`                            | VM name.                                                                                                                                                   |
| `[sbx]` | `memory`                | `--memory`                                               | Memory in MiB.                                                                                                                                             |
| `[sbx]` | `cpus`                  | `--cpus`                                                 | Number of virtual CPUs.                                                                                                                                    |
| `[sbx]` | `disk_size`             | `--disk-size`                                            | Disk size in MiB.                                                                                                                                          |
| `[sbx]` | `backend`               | -                                                        | Must be `qemu` for now. Other backends may be supported later.                                                                                             |
| `[sbx]` | `os`                    | `--os`                                                   | Guest OS value passed to SmolVM. Ignored when `image` is set.                                                                                              |
| `[sbx]` | `image`                 | `--image`                                                | Local ready-to-run image directory containing `smolvm-image.json`, kernel, and rootfs.                                                                     |
| `[sbx]` | `mount`                 | `--mount`                                                | Host mount(s), as a string or array of strings.                                                                                                            |
| `[sbx]` | `project_path`          | `--project-path`                                         | Mount a path at the same absolute guest path, force RW mounts, and start the attached agent there.                                                         |
| `[sbx]` | `writable_mounts`       | `--writable-mounts`                                      | Enable writable mounts.                                                                                                                                    |
| `[sbx]` | `run_user`              | `--run-user`                                             | Create/use a guest user and run the attached agent/shell as that user.                                                                                     |
| `[sbx]` | `auth_port`             | `--auth-port` / `--no-auth-port`                         | Automatically expose the OAuth callback port before attaching. Defaults to `true` for `run`.                                                               |
| `[sbx]` | `auth_host_port`        | `--auth-host-port`                                       | Host localhost port for OAuth callback forwarding. Defaults to `1455`.                                                                                     |
| `[sbx]` | `auth_guest_port`       | `--auth-guest-port`                                      | Guest port for OAuth callback forwarding. Defaults to `1455`.                                                                                              |
| `[sbx]` | `stop_on_exit`          | `--stop-on-exit` / `--keep-running`                      | Stop the VM after run/shell exits if no other sbx sessions remain. Defaults to `true`.                                                                     |
| `[sbx]` | `copy_host_credentials` | `--copy-host-credentials` / `--no-copy-host-credentials` | Allow/deny copying host credential files/configs. Defaults to `false`.                                                                                     |
| `[sbx]` | `env`                   | `--env KEY`                                              | Explicit allowlist of host environment variables to forward into the guest. Defaults to empty.                                                             |
| `[sbx]` | `git_config`            | `--git-config` / `--no-git-config`                       | Copy safe host Git identity/config into the guest. Defaults to `true`; does not copy credentials, SSH keys, signing keys, includes, or credential helpers. |
| `[sbx]` | `install_timeout`       | `--install-timeout`                                      | Agent install timeout in seconds. Ignored when `image` is set.                                                                                             |
| `[sbx]` | `boot_timeout`          | `--boot-timeout`                                         | VM boot/SSH readiness timeout in seconds. Defaults to `60`. Increase this if a cold boot leaves the VM running but SSH is not ready yet.                   |

### Local ready-to-run image directories

When `[sbx].image` is set, `sbx` treats the image as already containing the selected agent. It boots the image directly with the SmolVM SDK and then attaches to run the agent command; it does not run SmolVM's preset installer. If `[sbx].disk_size` is set for a local raw ext4 image, `sbx` asks SmolVM to create an isolated disk of that size and grow the filesystem. For the end-to-end Debian/Pi workflow, including optional Docker support with `--with-docker`, see [`docs/build-local-debian-pi-image.md`](docs/build-local-debian-pi-image.md).

Example layout:

```text
images/debian-pi/
├── smolvm-image.json
├── vmlinux.bin
└── rootfs.ext4
```

Example `smolvm-image.json`:

```json
{
    "name": "debian-pi",
    "kernel": "vmlinux.bin",
    "rootfs": "rootfs.ext4",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init",
    "sbx": {
        "agent": "pi",
        "launch_command": "pi"
    }
}
```

## Git commits from inside the VM

By default, `sbx run` and `sbx shell` copy a small safe subset of the host's global Git configuration into the guest user before attaching. This is intended to make commits inside the VM use the same author identity as the host. See [`docs/git-config-forwarding.md`](docs/git-config-forwarding.md) for details.

Copied keys are limited to safe identity/workflow settings such as:

```text
user.name
user.email
init.defaultBranch
pull.rebase
push.default
core.autocrlf
core.eol
```

`sbx` does not copy Git credentials, SSH keys, GPG/signing keys, credential helpers, includes, or URL rewrite rules. Disable this with:

```bash
sbx run --no-git-config
```

or:

```toml
[sbx]
git_config = false
```

## Browser login from inside the VM

When you run `/login` inside Pi in the VM, Pi starts a callback server inside the guest, commonly on port `1455`. Browser approval redirects to:

```text
http://localhost:1455/auth/callback?code=...
```

Your browser runs on the host, so `sbx run` opens an SSH local forward by default:

```text
host localhost:1455 -> VM localhost:1455
```

For an already-running VM, inspect/open/close the port manually:

```bash
sbx network status my-sbx
sbx network auth-port my-sbx
sbx network close-auth-port my-sbx
```

Known limitation: only one local process can own `localhost:1455` at a time. If
multiple VMs are running, `/login` works only for the VM currently receiving that
host port. Switch the tracked auth tunnel before logging in from another VM:

```bash
sbx network auth-port other-sbx --replace
```

If the port is owned by an untracked/non-`sbx` process, stop that process first.

## Notes

For the recommended working-directory/worktree layout with project-local Pi resources, see [`docs/project-organization.md`](docs/project-organization.md).

`sbx` defaults to QEMU to avoid per-VM TAP/nftables sudo requirements. See [`docs/qemu-default.md`](docs/qemu-default.md) for the rationale.
