# Build and run the local Debian image

`sbx image build` builds the curated agent-neutral SmolVM image under `~/.smolvm/images/sbx`. It contains shared development tooling and rootless Docker, but no agent package. `sbx` boots that image directly and installs only explicitly requested runtime tools.

## 1. Image build command

The builder is an `sbx` subcommand:

```bash
sbx image build
```

The image recipe is packaged with `sbx` and split into Debian and Docker Containerfile fragments:

```text
src/sbx/image/resources/Containers/Debian/
├── Base.Containerfile
└── fragments/Docker.Containerfile
```

`src/sbx/image/resources/Containers/Debian/Base.Containerfile` defines the reusable Debian base OS layer. It currently:

- starts from `debian:stable-slim`
- configures the NodeSource Node.js 22.x apt repository directly
- installs Node.js, npm, uv, Specify, and basic tools on the system PATH
- remains user-neutral so `sbx` can create the selected runtime user after boot

The build command reads these packaged resources with `importlib.resources` and combines them into a temporary Containerfile. An optional `--agent-containerfile` can add custom build-time tooling, but the packaged image has no Pi-specific layer or installer helper.

At first startup, `sbx` creates the configured user or a user matching the current host username. If Pi was explicitly requested, npm installs it directly under that user's `$HOME/.local`.

## 2. Install the tools

Install the currently supported tools first:

```bash
uv tool install --editable .
uv tool install 'smolvm==0.0.28'
```

`sbx` pins SmolVM `0.0.28`.

## 3. Build the image

Run the image build command:

```bash
sbx image build
```

The default image name is `sbx`. Override its size or name only when needed:

```bash
sbx image build --rootfs-size-mb 40960
sbx image build --name custom-image
```

The subcommand combines the Debian and Docker fragments, builds that image, adds SSH and the SmolVM-compatible init, exports `rootfs.ext4`, and builds the Docker-capable QEMU kernel. SmolVM's published kernel and guest agent are not downloaded; local images communicate through SSH.

For local experiments, you can override the packaged fragments with your own files:

```bash
sbx image build \
  --base-containerfile path/to/Base.Containerfile \
  --agent-containerfile path/to/OptionalTooling.Containerfile
```

Or pass a fully composed Containerfile directly:

```bash
sbx image build --containerfile path/to/Containerfile
```

The subcommand prints the built image paths, a minimal config snippet, and the recommended `sbx run ... --write-config` command. Image building never modifies the current project configuration.

The subcommand also writes a local image manifest:

```text
~/.smolvm/images/sbx/smolvm-image.json
```

List built images:

```bash
sbx image list
sbx image list --json
```

The builder downloads the reviewed SmolVM kernel recipe and Moby checker from pinned commit URLs, verifies each file's SHA-256 before use, and then compiles a Docker-capable QEMU kernel from a separately SHA-256-verified Linux source tarball. It stores the kernel as `vmlinux.bin`. Builds therefore require access to GitHub, kernel.org, and package repositories.

## 4. Local image directory layout

After a successful build, the image directory should look like:

```text
~/.smolvm/images/sbx/
├── smolvm-image.json
├── vmlinux.bin
└── rootfs.ext4
```

Example manifest:

```json
{
  "name": "sbx",
  "kernel": "vmlinux.bin",
  "rootfs": "rootfs.ext4",
  "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init",
  "sbx": {
    "features": ["docker"]
  }
}
```

## 5. Configure and run `sbx`

Use the built image and write the selected project settings:

```bash
sbx run the-quest \
  --image '~/.smolvm/images/sbx' \
  --project-path . \
  --writable-mounts \
  --runtime-install pi \
  --write-config
```

When CLI and project configuration do not select a user, `sbx` uses the current host username. `--project-path .` remains explicit because it creates a writable host mount.

The suggested `.sbx.toml` is:

```toml
[sbx]
name = "the-quest"
agent = "pi"
runtime_install = ["pi"]
image = "~/.smolvm/images/sbx"
project_path = "."
run_user = "me"
writable_mounts = true
copy_host_credentials = false
git_config = true
```

Important: `copy_host_credentials = false` prevents host credential/config copying. Runtime-user provisioning does not recursively change ownership beneath the user's home, so same-path mounts are not traversed.

`git_config = true` is the default and only copies a safe subset of host Git identity/workflow settings, such as `user.name` and `user.email`, so commits inside the VM have the expected author. It does not copy Git credentials, SSH keys, GPG/signing keys, credential helpers, includes, or URL rewrite rules.

`--write-config` belongs to `run` and `create`; `image build` never writes `.sbx.toml`. Later runs can use the saved configuration:

```bash
sbx run
```

## Docker guest usage

### Host

Install Docker on the host, then build the image and point `.sbx.toml` at it. Kernel compile tools run inside the packaged `Containers/Build/Kernel.Containerfile`; the guest image installs Docker from Docker's official Debian apt repository. No host kernel compiler packages are required.

Ensure Docker works on the host for the image build:

```bash
docker version
```

```bash
sbx image build --rootfs-size-mb 81920
```

```toml
[sbx]
image = "~/.smolvm/images/sbx"
run_user = "me"
```

Create or recreate the VM after changing the image. If the rootfs was built with `--rootfs-size-mb 81920`, omit `disk_size` unless you intentionally want SmolVM to grow the per-VM disk.

Rootless Docker starts after `sbx` provisions the selected user. Inside the guest, use Docker normally:

```bash
docker run --rm hello-world
```

Rootless Docker data lives on the VM disk under Docker's default rootless data directory:

```text
/home/<run-user>/.local/share/docker
```

The runtime socket/state is separate and recreated at boot:

```text
/run/user/<uid>/docker.sock
/run/user/<uid>/dockerd-rootless
```

SmolVM does not run systemd/logind, so the provisioning helper creates `/run/user/<uid>` and exports `XDG_RUNTIME_DIR`/`DOCKER_HOST` for the selected user's actual UID.

To persist pulled images/build cache across VM recreation, mount a host directory at the rootless Docker data path:

```toml
[sbx]
mount = ["/host/path/docker-data:/home/me/.local/share/docker"]
writable_mounts = true
```

Use an ext4-backed host path if possible. 9p/workspace mounts may be slow or unsupported for Docker storage; if it fails, keep Docker data on the VM disk. Mounted workspaces can still be used as build contexts initially; if 9p is slow or fails, copy the context into the VM disk first.

## Troubleshooting

### Rootless Docker did not start

Check the boot logs:

```bash
sudo cat /var/log/dockerd-rootless.log
```

Restart manually if needed:

```bash
sudo SBX_DOCKER_USER="$(id -un)" /usr/local/bin/sbx-start-rootless-docker
```

The helper applies the temporary SmolVM/QEMU DNS workaround: `10.0.2.3` is QEMU slirp DNS; `10.0.2.2` is the gateway and can add ~5s resolver delays.

### Rootful Docker troubleshooting

Run rootful Docker directly only for debugging:

```bash
sudo mkdir -p /sys/fs/cgroup
sudo mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true
sudo dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 &
sudo docker run --rm hello-world
```

Rootful Docker data lives under `/var/lib/docker`.

### `QEMU exited early` / `Error loading uncompressed kernel without PVH ELF Note`

The image likely references an incompatible or older kernel. Rebuild it with the current `sbx image build` command to produce the Docker-capable QEMU kernel.

### VM starts but SSH readiness times out

If a cold boot reports that `wait_for_ssh` timed out, but `sbx ls` shows the VM as `running`, the guest may simply need more time before SSH is ready. Retry:

```bash
sbx run
```

If it happens repeatedly, increase the timeout:

```bash
sbx run --boot-timeout 90
```

or set it in `.sbx.toml`:

```toml
[sbx]
boot_timeout = 90
```

### `agent 'pi' was not found`

The image intentionally contains no Pi package. Add `runtime_install = ["pi"]` and run:

```bash
sbx run --reprovision --no-attach
```

Alternatively use `--runtime-install pi`, which implies reprovisioning for an existing VM. Installation requires npm registry access and writes directly to `/home/<run-user>/.local` without creating or changing `.npmrc`.

### `e2fsck and resize2fs are needed to grow the disk`

If `.sbx.toml` sets `disk_size` larger than the local image's `rootfs.ext4`, SmolVM grows a host-side per-VM ext4 disk before the VM boots. This requires the host tools `e2fsck` and `resize2fs`; installing them inside the guest VM is not enough.

On some systems the tools are installed under `/usr/sbin` or `/sbin`, but those directories are not on a normal user's `PATH`. Try:

```bash
PATH="$PATH:/usr/sbin:/sbin" sbx run
```

Alternatively, remove `disk_size` if the image is already large enough, or rebuild the image with the desired size.

Resizing is per VM. SmolVM normally materializes an isolated disk for each sandbox, so growing `the-quest` does not resize the shared image directory or other VMs that use the same local image. If you rebuilt the base image and want an existing VM to pick it up, remove/recreate that VM.

### Existing VM keeps using old image contents

If you changed and rebuilt the image, remove/recreate the VM:

```bash
sbx rm the-quest --force
sbx run
```

### Auth callback port already open

Inspect or close tracked tunnels:

```bash
sbx network status the-quest
sbx network close-auth-port the-quest
```

## Notes

Local image mode is intentionally minimal: one `image` key points at a ready-to-run image directory. Kernel, rootfs, boot args, and image features live in `smolvm-image.json`; agent selection and runtime installation stay in box configuration.
