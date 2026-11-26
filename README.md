# sbx

`sbx` runs coding agents in disposable [SmolVM](https://github.com/CelestoAI/smolVM) virtual machines. Selected project directories are shared with the guest; tools and optional rootless Docker run inside it.

Host agent configuration is not copied by default. Explicit environment forwarding can give the guest real values; gateway credential substitution is a separate host-side mechanism. Shared writable directories are writable by guest workloads—do not mount secrets or host evidence directories.

## Install

**Documentation version:** this checkout. The tagged command below installs a released version, not every unreleased feature described here. In particular, `v0.2.5` predates gateway mode.

Released installation, on the host:

```bash
uv tool install git+https://github.com/nueces/sbx.git@v0.2.5
```

For unreleased features, use a local source checkout that contains them. From that repository's root on the host:

```bash
uv tool install --editable .
```

This does not fetch or publish an unreleased tag. Check the installed command before creating a sandbox:

```bash
sbx --version
sbx doctor
```

`doctor` checks host, QEMU, configuration and VM-state requirements without starting a VM. Linux amd64 gateway functionality has been tested; native macOS gateway execution remains unverified. See [gateway support and open gates](docs/rootless-network-gateway.md#support-and-prerequisites), not a cross-build alone, for the platform boundary.

## First sandbox

### 1. Build the image

On a host with working Docker:

```bash
sbx image build
sbx image list
```

`sbx image list` shows `SIZE` (actual host disk usage, sparse-aware) and `VIRTUAL` (rootfs size, the minimum `[sbx].disk_size`).

The default image is `~/.smolvm/images/sbx`. It contains common tools, Node.js, SSH and rootless Docker, but no agent. New presets without `--image` also use this prepared Debian base; build it first. Old `[sbx].os` settings must be removed or set to `"debian"`.

Kernel builds take time and need network access. See [image building and recovery](docs/build-local-debian-pi-image.md) for prerequisites, sizes, Landlock and optional nested KVM. Nested KVM is never enabled by default and increases the attack surface.

### 2. Create and launch

From the host project directory:

```bash
sbx run the-quest \
  --image '~/.smolvm/images/sbx' \
  --project-path . \
  --writable-mounts \
  --runtime-install pi
```

This creates/provisions the VM, creates a runtime account matching the host username unless configured otherwise, mounts the project at the same absolute path, writes missing project settings to `.sbx.toml`, and launches Pi. `--writable-mounts` permits guest writes to reach the host. Mount only the project data you intend to expose.

Agent selection and installation are independent. Other supported installers include `claude`, `opencode`, `codex` and `specify`; installation runs third-party code inside the guest as the runtime user. Existing tools are not silently upgraded or removed.

### 3. Use it day to day

From the configured project directory on the host:

```bash
sbx run
sbx shell
sbx stop
sbx ls
```

`sbx ls` shows each VM's agent (chosen at creation; for older VMs, `[sbx].agent` from the project's `.sbx.toml`, else `~/.config/sbx/config.toml`, default `pi`, or `-` if the project config is missing or invalid), its image, the image's host disk usage, and the VM disk's host usage (`DISK USED`) and virtual size (`DISK SIZE`). The image is recorded when the VM is created; for older VMs it is inferred from the kernel's image directory, else `-`.

The nearest ancestor `.sbx.toml` supplies the sandbox name and project anchor; explicit names still win. Existing VM disks survive stop/start. Adding mounts or changing memory/CPUs requires stop/start; user/tool setup changes require explicit reprovisioning:

```bash
sbx run --reprovision --no-attach
```

For recovery, `sbx shell --root --keep-running` opens a diagnostic root session without normal workspace or environment setup. See [sandbox lifecycle and recovery](docs/ergonomics.md).

**Destructive:** `sbx recreate` and `sbx rm` delete the VM disk. Preserve guest-only data and private gateway evidence before using them. They are not substitutes for stop/start or reprovisioning.

## Project configuration

Complete project example (replace the name and paths for your project):

```toml
[sbx]
name = "the-quest"
agent = "pi"
runtime_install = ["pi"]
image = "~/.smolvm/images/sbx"
project_path = "."
writable_mounts = true
copy_host_credentials = false
git_config = true
```

User defaults, the nearest project config, an explicit `--config PATH` override and CLI options apply in that order. Relative config paths belong to the file supplying them; CLI-relative paths belong to the invoking directory. Nested invocations preserve the corresponding mounted subdirectory. See [project context and configuration](docs/ergonomics.md#project-context) and [`sbx.toml.example`](sbx.toml.example).

Security/configuration fragments:

```toml
[sbx]
copy_host_credentials = false
git_config = true
env = ["OPENAI_API_KEY", "APP_MODE=development", "SERVICE_TOKEN=${HOST_TOKEN}"]
```

- [Environment forwarding](docs/environment-forwarding.md) sends selected **real values** into persistent guest-managed environment; it is not a secret broker. Never commit literal secrets.
- [Git configuration forwarding](docs/git-config-forwarding.md) copies safe identity/workflow settings, not Git credentials or keys.
- [Host-side network credentials](docs/network-credentials.md) give the guest placeholders and substitute real values only on authorized HTTPS requests.

## Authentication and networking

OAuth callback tunnels only forward traffic; they do not fetch credentials, authenticate the agent, or select its model. On the host:

```bash
sbx run --auth-port=openai
sbx run --auth-port=anthropic
sbx network auth-port --auth-port=both
sbx network close-auth-port
```

The profiles use OpenAI port 1455 and Anthropic port 53692. Omitted configuration allows session forwarding only after first successful attached provisioning; explicit profiles can enable it on later runs. See [callback profiles and tunnel ownership](docs/auth-ports.md).

Without `[network]`, the QEMU backend normally uses SLIRP. Adding `[network]` enables a separate **default-deny gateway**; it never falls back to SLIRP on failure. Policies can explicitly allow DNS destinations or [exact IPv4 TCP/HTTP(S)](docs/rootless-network-gateway.md#direct-ipv4-policies). See the [gateway guide](docs/rootless-network-gateway.md) for policy, TLS, durable audit and platform limits.

Useful host commands:

```bash
sbx network forward 3000
sbx network status
sbx network diagnostics
```

A foreground forward lasts until Ctrl-C. Optional `[network].mitmweb = true` enables a private authenticated read-only UI; stop/start applies that setting. Run `sbx network ui` (or `sbx network ui NAME`) to print its current login URL, then open it manually. **The URL contains a login token: do not share it or capture it in shared logs.** The command never opens a browser, enables the UI or restarts a sandbox. Diagnostics remain for troubleshooting and never print the token. The [networking command reference](docs/network-command-roadmap.md#gateway-ui) covers setup and failure guidance.

## Completion and command help

On the host, select the appropriate shell:

```bash
eval "$(sbx completion bash)"
eval "$(sbx completion zsh)"
sbx completion fish | source
```

Shell completion support does not imply Zsh guest-shell support; Bash is the supported guest shell. See [guest setup and shell behavior](docs/ergonomics.md#guest-setup-lifecycle).

```bash
sbx --help
sbx run --help
sbx network --help
```

## Documentation

Start at the [documentation index](docs/README.md) for user guides, networking/security reference and contributor instructions. Documentation describes supported behavior, not an approval to publish a release or a claim that every platform and long-duration gate has passed.
