# sbx

`sbx` runs coding agents in disposable [SmolVM](https://github.com/CelestoAI/smolVM) virtual machines. Your project is mounted into the VM, while the agent, tools, and optional rootless Docker daemon run inside it.

Host credentials are not copied by default. `sbx` forwards only selected environment variables and a safe subset of Git settings; it never forwards Git credentials or keys.

## Install

```bash
uv tool install git+https://github.com/nueces/sbx.git@v0.2.4
```

Alternatively, install an editable clone:

```bash
git clone https://github.com/nueces/sbx.git
uv tool install --editable ./sbx
```

After either installation, check that the host, QEMU backend, and project configuration are ready before creating a sandbox:

```bash
sbx doctor
```

`sbx doctor` reports missing host requirements and configuration or VM-state problems without starting a VM.

## Recommended workflow

### 1. Build the curated image

The curated image contains Pi, common development tools, and rootless Docker. Building it requires a working host Docker installation:

```bash
sbx image build
```

The image is written to `~/.smolvm/images/sbx`. The first build compiles a QEMU kernel and can take a while. Later builds reuse Docker layers.

Check the result with:

```bash
sbx image list
```

See the [image guide](docs/build-local-debian-pi-image.md) for custom sizes, Docker details, and troubleshooting.

### 2. Create the project sandbox

Run this from the project you want the agent to work on:

```bash
cd ~/code/my-project
sbx run the-quest \
  --image '~/.smolvm/images/sbx' \
  --project-path . \
  --writable-mounts
```

On first creation, `sbx`:

1. creates and starts the VM;
2. mounts the project at the same absolute path inside the VM;
3. writes the project settings to `./.sbx.toml`; and
4. launches Pi as the `agent` user in the mounted project.

### 3. Use the sandbox day to day

The sandbox name is stored in `.sbx.toml`, so commands run from the project directory do not need it again:

```bash
sbx run       # start if needed and launch the agent
sbx shell     # open a shell in the mounted project
sbx stop      # stop the VM without deleting its disk
sbx ls        # list all sandboxes, including stopped ones
sbx rm        # remove the VM after confirmation
```

## Customize the sandbox

### Add another folder

Mounts are applied when a VM starts; they cannot be hot-added to an already-running VM. Add the folder to `.sbx.toml` using an absolute host path:

```toml
[sbx]
# Existing project settings remain here.
mount = [
  "/home/me/code/shared-tools",
  "/home/me/data:/workspace/data",
]
writable_mounts = true
```

A bare path appears at the same absolute path in the VM. `HOST:GUEST` chooses a different guest path.

If the VM is running, restart it through `sbx` to apply the mounts:

```bash
sbx stop
sbx run
```

This stop/start cycle does not recreate the sandbox or delete its disk, so applying a new mount does not lose the agent session. Use the agent's normal resume/continue flow after restarting.

If you try to run with different mounts while the VM is already running, `sbx` keeps the current mounts and prints the same stop-and-run guidance.

### Install more tools

The curated image includes Pi. For one project, install other tools directly on the sandbox disk instead of rebuilding the image:

```bash
sbx shell

# Run these inside the VM as the configured agent user:
npm install -g opencode-ai
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
exit
```

Claude Code and OpenCode require their npm postinstall scripts to prepare native binaries; do not install either with `--ignore-scripts`.

Installed software survives `sbx stop` and remains available on later runs:

```bash
sbx run --agent claude
sbx run --agent codex

# OpenCode is not an sbx agent preset; launch it inside a sandbox shell.
sbx shell
opencode
```

`sbx recreate` and `sbx rm` delete the sandbox disk. Add frequently used tools to the curated image if every new or recreated sandbox should contain them.

## Authentication and networking

Run `/login` inside Pi when authentication is needed. `sbx run` automatically forwards the browser callback port to the VM.

Temporarily expose a VM service to the host until Ctrl-C:

```bash
sbx network forward 3000       # host 3000 -> guest 3000
sbx network forward 8080:3000  # host 8080 -> guest 3000
```

These commands use the sandbox named in `.sbx.toml`. Use `--name OTHER_VM` only when forwarding from a different sandbox.

## Other operations

```bash
# Create or start without launching an agent.
sbx run --no-attach

# Keep the VM running after the agent exits.
sbx run --keep-running

# Delete and recreate the VM from the current configuration.
sbx recreate --force
```

Use `sbx --help` or `sbx COMMAND --help` for the complete command and option reference.

## Project configuration

The first project creation produces a small `.sbx.toml` resembling:

```toml
[sbx]
name = "the-quest"
agent = "pi"
image = "~/.smolvm/images/sbx"
project_path = "."
run_user = "agent"
writable_mounts = true
copy_host_credentials = false
git_config = true
```

`sbx` reads configuration in this order, with later values winning:

1. `~/.config/sbx/config.toml` — user defaults;
2. `./.sbx.toml` — project defaults; and
3. `--config PATH` — an explicit override.

CLI options override configuration. Keep durable choices such as the image, VM resources, user, mounts, and forwarded environment names in `.sbx.toml`; use commands for actions such as running, stopping, or recreating.

Security-sensitive behavior is configuration-only:

```toml
[sbx]
# Disabled by default. Enable only when the VM should receive host agent configs.
copy_host_credentials = false

# Copies safe Git identity/workflow settings, never credentials or keys.
git_config = true

# Forward only named host environment variables.
env = ["OPENAI_API_KEY"]
```

See [`sbx.toml.example`](sbx.toml.example) for available settings.

## Shell completion

```bash
# One-off setup for the current shell.
eval "$(sbx completion bash)"
eval "$(sbx completion zsh)"

# fish
sbx completion fish | source
```

The same commands can be redirected into your shell's normal completion directory for permanent installation.

## More documentation

- [Build and run the curated image](docs/build-local-debian-pi-image.md)
- [Environment forwarding](docs/environment-forwarding.md)
- [Safe Git configuration forwarding](docs/git-config-forwarding.md)
- [Networking commands](docs/network-command-roadmap.md)
- [CLI ergonomics and behavior](docs/ergonomics.md)
- [Contributor setup and tests](docs/development.md)

`sbx` uses QEMU by default to avoid per-VM TAP and nftables setup. See [QEMU defaults](docs/qemu-default.md) for the rationale.
