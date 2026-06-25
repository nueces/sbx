# Build and run a local Debian Pi image

`sbx image build-debian` builds a ready-to-run SmolVM image directory that already contains Pi. `sbx` boots that image directly and attaches to run `pi`; it does not run SmolVM's preset installer.

## 1. Image build command

The builder is an advanced `sbx` subcommand:

```bash
sbx image build-debian
```

The image recipe is packaged with `sbx` and split into two Containerfile fragments:

```text
src/sbx/image/resources/Containers/
├── Debian/
│   └── Base.Containerfile
└── Agents/
    └── Pi.Containerfile
```

`src/sbx/image/resources/Containers/Debian/Base.Containerfile` defines the reusable Debian base OS layer. It currently:

- starts from `debian:stable-slim`
- configures the NodeSource Node.js 22.x apt repository directly
- installs Node.js and basic tools
- creates an `agent` user with passwordless sudo

`src/sbx/image/resources/Containers/Agents/Pi.Containerfile` defines the agent/tooling layer. It currently:

- installs `@earendil-works/pi-coding-agent` for the `agent` user
- links `pi` into `/home/agent/.local/bin/pi`
- installs `uv`
- installs spec-kit CLI with `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git`

The build command reads these packaged resources with `importlib.resources` and combines them into a temporary Containerfile. Docker layer caching still makes the base OS layer reusable across tooling changes.

Because Pi and uv tools are installed for `agent`, the matching `sbx` config should use:

```toml
run_user = "agent"
```

## 2. Install the tools

Install the currently supported tools first:

```bash
uv tool install --editable .
uv tool install 'smolvm==0.0.19'
```

`sbx` pins SmolVM `0.0.19` for now. Newer SmolVM compatibility is separate work.

## 3. Build the image

Run the image build command:

```bash
sbx image build-debian
```

Useful options:

```bash
sbx image build-debian \
  --name debian-sbx \
  --rootfs-size-mb 40960
```

The subcommand combines the base/agent Containerfiles, plus Docker when `--with-docker` is set, builds that combined Containerfile first, and passes the resulting Docker image into SmolVM's Debian image builder. If the combined Containerfile ends with `USER agent`, the subcommand wraps it with a tiny `USER root` image so SmolVM's builder can still run its root-level SSH/init setup.

For local experiments, you can override the packaged fragments with your own files:

```bash
sbx image build-debian \
  --base-containerfile path/to/Base.Containerfile \
  --agent-containerfile path/to/Pi.Containerfile
```

Or pass a fully composed Containerfile directly:

```bash
sbx image build-debian --containerfile path/to/Containerfile
```

By default the subcommand prints only the built image paths and a minimal `sbx` config snippet. To also print a SmolVM SDK usage sketch after building, pass `--sdk-sketch`.

To print the SDK sketch later without rebuilding the image, run:

```bash
sbx image build-debian --print-sdk-sketch ~/.smolvm/images/debian-sbx
```

The subcommand also writes a local image manifest:

```text
~/.smolvm/images/debian-sbx/smolvm-image.json
```

and uses SmolVM's QEMU-compatible kernel by default. With `--with-docker`, it builds a Docker-capable kernel from pinned SmolVM kernel build inputs and stores it as `vmlinux-docker.bin` in the image directory.

## 4. Local image directory layout

After a successful build, the image directory should look like:

```text
~/.smolvm/images/debian-sbx/
├── smolvm-image.json
├── vmlinux.bin
├── vmlinux-docker.bin  # only when built with --with-docker
└── rootfs.ext4
```

Example manifest:

```json
{
  "name": "debian-sbx",
  "kernel": "vmlinux.bin",
  "rootfs": "rootfs.ext4",
  "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init",
  "sbx": {
    "agent": "pi",
    "launch_command": "pi"
  }
}
```

## 5. Configure `sbx`

Create or update `.sbx.toml`:

```toml
[sbx]
agent = "pi"
name = "debian-pi-test"
image = "~/.smolvm/images/debian-sbx"
memory = 2048
cpus = 2
boot_timeout = 60
run_user = "agent"

project_path = "."
writable_mounts = true

stop_on_exit = false
copy_host_credentials = false
env = []

# True by default. Copies safe Git identity/config only, not credentials.
git_config = true
```

Important: `copy_host_credentials = false` prevents host credential/config copying. The `run_user` preparation may copy files already inside the guest from `/root` to `/home/agent`, but it does not copy host credentials into the VM.

`git_config = true` is the default and only copies a safe subset of host Git identity/workflow settings, such as `user.name` and `user.email`, so commits inside the VM have the expected author. It does not copy Git credentials, SSH keys, GPG/signing keys, credential helpers, includes, or URL rewrite rules.

## 6. Run it

Use the configured name from `.sbx.toml`:

```bash
sbx --debug run
```

Passing a positional argument changes the VM name. For example:

```bash
sbx --debug run pi
```

means "run a sandbox named `pi`", not "run the Pi agent".

## Docker guest usage

### Host

Install Docker on the host, then build the Docker-capable image and point `.sbx.toml` at it. Kernel compile tools run inside the packaged `Containers/Build/Kernel.Containerfile`; the guest image installs Docker from Docker's official Debian apt repository. No host kernel compiler packages are required.

Ensure Docker works on the host for the image build:

```bash
docker version
```

```bash
sbx image build-debian \
  --with-docker \
  --name debian-sbx-docker \
  --rootfs-size-mb 81920
```

```toml
[sbx]
image = "~/.smolvm/images/debian-sbx-docker"
run_user = "agent"
```

Create or recreate the VM after changing the image. If the rootfs was built with `--rootfs-size-mb 81920`, omit `disk_size` unless you intentionally want SmolVM to grow the per-VM disk.

Rootless Docker starts at VM boot in Docker-capable images. Inside the guest, use Docker normally:

```bash
docker run --rm hello-world
```

Rootless Docker data lives on the VM disk under Docker's default rootless data directory:

```text
/home/agent/.local/share/docker
```

The runtime socket/state is separate and recreated at boot:

```text
/run/user/1000/docker.sock
/run/user/1000/dockerd-rootless
```

SmolVM does not run systemd/logind, so the image creates `/run/user/1000` and exports `XDG_RUNTIME_DIR`/`DOCKER_HOST`; this matches the normal rootless Docker path for uid 1000.

To persist pulled images/build cache across VM recreation, mount a host directory at the rootless Docker data path:

```toml
[sbx]
mount = ["/host/path/docker-data:/home/agent/.local/share/docker"]
writable_mounts = true
```

Use an ext4-backed host path if possible. 9p/workspace mounts may be slow or unsupported for Docker storage; if it fails, keep Docker data on the VM disk. Mounted workspaces can still be used as build contexts initially; if 9p is slow or fails, copy the context into the VM disk first.

## Troubleshooting

### Rootless Docker did not start

Check the boot logs:

```bash
sudo cat /var/log/sbx-rootless-docker.log
sudo cat /var/log/dockerd-rootless.log
```

Restart manually if needed:

```bash
sudo /usr/local/bin/sbx-start-rootless-docker
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

The image was likely built with SmolVM's Firecracker-compatible ELF kernel. Rebuild with the current `sbx image build-debian` command; it selects SmolVM's QEMU-compatible kernel by default.

### VM starts but SSH readiness times out

If a cold boot reports that `wait_for_ssh` timed out, but `sbx ls -a` shows the VM as `running`, the guest may simply need more time before SSH is ready. Retry:

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

### `bash: exec: pi: not found`

Pi is installed under `/home/agent/.local/bin/pi` by the packaged `Containers/Agents/Pi.Containerfile`. Ensure `.sbx.toml` includes:

```toml
run_user = "agent"
```

### `e2fsck and resize2fs are needed to grow the disk`

If `.sbx.toml` sets `disk_size` larger than the local image's `rootfs.ext4`, SmolVM grows a host-side per-VM ext4 disk before the VM boots. This requires the host tools `e2fsck` and `resize2fs`; installing them inside the guest VM is not enough.

On some systems the tools are installed under `/usr/sbin` or `/sbin`, but those directories are not on a normal user's `PATH`. Try:

```bash
PATH="$PATH:/usr/sbin:/sbin" sbx run
```

Alternatively, remove `disk_size` if the image is already large enough, or rebuild the image with the desired size.

Resizing is per VM. SmolVM normally materializes an isolated disk for each sandbox, so growing `reviewhero` does not resize the shared image directory or other VMs that use the same local image. If you rebuilt the base image and want an existing VM to pick it up, remove/recreate that VM.

### Existing VM keeps using old image contents

If you changed and rebuilt the image, remove/recreate the VM:

```bash
sbx rm debian-pi-test --force
sbx run
```

or, if you created a VM named `pi` by running `sbx run pi`:

```bash
sbx rm pi --force
sbx run
```

### Auth callback port already open

Inspect or close tracked tunnels:

```bash
sbx network status debian-pi-test
sbx network close-auth-port debian-pi-test
```

## Notes

Local image mode is intentionally minimal: one `image` key points at a ready-to-run image directory. Kernel, rootfs, boot args, agent, and launch command live in `smolvm-image.json` instead of being exposed as many `sbx` config keys.
