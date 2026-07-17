# sbx

`sbx` runs coding agents inside disposable [SmolVM](https://github.com/CelestoAI/smolVM) virtual machines.

Agents and authentication run in the VM, not on the host. By default `sbx` does **not** copy host credentials into the guest. Use `--copy-host-credentials` only when you explicitly want SmolVM presets to forward local agent configs/API keys.

## Install

```bash
uv tool install git+https://github.com/nueces/sbx.git@v0.2.0
sbx doctor
```

For local development, install from a checkout instead:

```bash
git clone https://github.com/nueces/sbx.git
uv tool install --editable ./sbx
```

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
# place it first in the mount list, and start the attached agent there.
sbx run my-sbx --project-path .

# Mount extra host directories at their same absolute guest paths.
sbx run my-sbx --mount /home/me/src/tooling --mount /home/me/src/data

# Or choose an explicit guest path.
sbx run my-sbx --mount /home/me/src/tooling:/workspace/tooling

# Disable automatic OAuth callback forwarding.
sbx run my-sbx --no-auth-port

# Temporarily forward running guest services until Ctrl-C.
# SPEC: GUEST_PORT, HOST_PORT:GUEST_PORT, or BIND_HOST:HOST_PORT:GUEST_PORT.
sbx network forward my-sbx 3000
sbx network forward 8080:3000
sbx network forward 0.0.0.0:3000:3000
sbx network forward my-sbx 3000 8080:80

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
| `network forward [NAME] SPEC...` | Temporarily forward host TCP ports to a running sandbox until Ctrl-C.                    |
| `network auth-port [NAME]`       | Expert helper: manually expose the OAuth callback port for an already-running sandbox.   |
| `network close-auth-port [NAME]` | Expert helper: close the tracked OAuth callback tunnel.                                  |
| `image build-debian`             | Advanced helper: build a local Debian/Pi image, optionally with `--with-docker`.         |
| `image ls`                       | List local ready-to-run images under `~/.smolvm/images`.                                |
| `doctor`                         | Run non-sudo SmolVM diagnostics for QEMU.                                                |
| `completion SHELL`               | Generate shell completion for `bash`, `zsh`, or `fish`.                                  |

When `NAME` is omitted, commands that operate on one sandbox use `[sbx].name` from configuration.

We intentionally keep `recreate` separate from a possible future `restart`/`reboot`: `recreate` means delete and create a fresh VM, while a soft restart would reuse the same VM disk/state.

## Images and features

List local ready-to-run images:

```bash
sbx image ls
```

Build the default Debian/Pi image:

```bash
sbx image build-debian --name debian-sbx
```

Use it from `.sbx.toml`:

```toml
[sbx]
image = "~/.smolvm/images/debian-sbx"
run_user = "agent"
```

Configure durable TCP forwards applied when the VM starts:

```toml
[sbx]
port_forwards = ["3000", "8080:3000"]
```

Build with Docker support:

```bash
sbx image build-debian --with-docker --name debian-sbx-docker --rootfs-size-mb 81920
```

Use the Docker-capable image:

```toml
[sbx]
image = "~/.smolvm/images/debian-sbx-docker"
run_user = "agent"
```

These Debian/Pi images should run as `agent`; Docker rootless also uses that user. Docker-capable images show `docker` in `sbx image ls` and start rootless Docker at boot.

For details, see [`docs/build-local-debian-pi-image.md`](docs/build-local-debian-pi-image.md). For contributor setup and test commands, see [`docs/development.md`](docs/development.md).

## Configuration

`sbx` reads TOML configuration from these locations, merging later files over earlier files:

1. `~/.config/sbx/config.toml` — user defaults
2. `./.sbx.toml` — project defaults from the directory where `sbx` is executed
3. `--config PATH` — explicit override file

CLI flags always override config values.

Start with the released defaults; add `.sbx.toml` only when you need a named VM, mounts, image, user, or safety defaults.

When `sbx run` or `sbx create` creates a new named VM and `./.sbx.toml` does not exist, `sbx` writes a minimal project config with the VM name and selected options. Existing VMs do not create or update `.sbx.toml` unless `--write-config` is passed. Use `--no-write-config` to skip this bootstrap.
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
boot_timeout = 30

# Optional: use a local ready-to-run image directory with smolvm-image.json.
# image = "./images/debian-pi"

# Bare mounts use the same absolute guest path.
# Explicit HOST:GUEST mounts keep the configured guest path.
mount = [
  "/foo/bar",
  "/foo/cache:/workspace/cache",
]
# project_path is mounted first and used as the attached working directory.
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
| `[sbx]` | `mount`                 | `--mount`                                                | Host mount(s), as a string or array of strings. Bare host paths mount at the same absolute guest path; `HOST:GUEST` keeps the explicit guest path.          |
| `[sbx]` | `project_path`          | `--project-path`                                         | Mount a path first at the same absolute guest path, force RW mounts, and start the attached agent there.                                                    |
| `[sbx]` | `writable_mounts`       | `--writable-mounts`                                      | Enable writable mounts.                                                                                                                                    |
| `[sbx]` | `run_user`              | `--run-user`                                             | Create/use a guest user and run the attached agent/shell as that user.                                                                                     |
| `[sbx]` | `auth_port`             | `--auth-port` / `--no-auth-port`                         | Automatically expose the OAuth callback port before attaching. Defaults to `true` for `run`.                                                               |
| `[sbx]` | `auth_host_port`        | `--auth-host-port`                                       | Host localhost port for OAuth callback forwarding. Defaults to `1455`.                                                                                     |
| `[sbx]` | `auth_guest_port`       | `--auth-guest-port`                                      | Guest port for OAuth callback forwarding. Defaults to `1455`.                                                                                              |
| `[sbx]` | `stop_on_exit`          | `--stop-on-exit` / `--keep-running`                      | Stop the VM after run/shell exits if no other sbx sessions remain. Defaults to `true`.                                                                     |
| `[sbx]` | `copy_host_credentials` | `--copy-host-credentials` / `--no-copy-host-credentials` | Allow/deny copying host credential files/configs. Defaults to `false`.                                                                                     |
| `[sbx]` | `env`                   | `--env KEY`                                              | Explicit allowlist of host environment variables to forward into the guest. Defaults to empty. See [environment forwarding](docs/environment-forwarding.md). |
| `[sbx]` | `git_config`            | `--git-config` / `--no-git-config`                       | Copy safe host Git identity/config into the guest. Defaults to `true`; does not copy credentials, SSH keys, signing keys, includes, or credential helpers. |
| `[sbx]` | `install_timeout`       | `--install-timeout`                                      | Agent install timeout in seconds. Ignored when `image` is set.                                                                                             |
| `[sbx]` | `boot_timeout`          | `--boot-timeout`                                         | VM boot/SSH readiness timeout in seconds. Defaults to `30`. Increase this if a cold boot leaves the VM running but SSH is not ready yet.                   |

### Environment forwarding

Host environment variables are not forwarded by default. Add selected names to `[sbx].env` or pass `--env KEY` to `sbx run`.

Before `sbx run` or `sbx shell` attaches, `sbx` syncs those names from the current host environment into the VM. Missing host variables are unset in the guest to avoid stale secrets. This affects only new attached processes; already-running agents or shells keep their old environment.

See [`docs/environment-forwarding.md`](docs/environment-forwarding.md) for details and limitations.

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

## Releases

Maintainers start a release with the manual `Start release` workflow:

```bash
gh workflow run start-release.yml --ref main -f version=0.2.1
```

Leave `version` blank to use the next patch version.

Release workflows:

- `.github/workflows/start-release.yml` (`Start release`): manual entry point; opens the package release PR, including the README install tag.
- `.github/workflows/release-pr-checks.yml` (`Release PR checks`): validates `release/v*` PRs only change version files.
- `.github/workflows/publish-release.yml` (`Publish release`): after the release PR is merged, creates the `v0.2.1` tag, GitHub release, website PR, and a PR bumping `main` to the next dev version, for example `0.2.2.dev0`.

## Notes

For the recommended working-directory/worktree layout with project-local Pi resources, see [`docs/project-organization.md`](docs/project-organization.md).

`sbx` defaults to QEMU to avoid per-VM TAP/nftables sudo requirements. See [`docs/qemu-default.md`](docs/qemu-default.md) for the rationale.
