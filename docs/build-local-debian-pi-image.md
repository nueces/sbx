# Build and run a local Debian Pi image

This project can run `sbx` from a local ready-to-run SmolVM image directory. In this mode the image is expected to already contain Pi, so `sbx` boots the image directly and attaches to run `pi`; it does not run SmolVM's preset installer.

## 1. Customize the Containerfiles

The image recipe is split into two Containerfile fragments:

```text
Containers/
├── Debian/
│   └── Base.Containerfile
└── Agents/
    └── Pi.Containerfile
```

`Containers/Debian/Base.Containerfile` defines the reusable Debian base OS layer. It currently:

- starts from `debian:stable-slim`
- configures the NodeSource Node.js 22.x apt repository directly
- installs Node.js and basic tools
- creates an `agent` user with passwordless sudo

`Containers/Agents/Pi.Containerfile` defines the agent/tooling layer. It currently:

- installs `@earendil-works/pi-coding-agent` for the `agent` user
- links `pi` into `/home/agent/.local/bin/pi`
- installs `uv`
- installs spec-kit CLI with `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git`

The build script combines these fragments into a temporary Containerfile. Docker layer caching still makes the base OS layer reusable across tooling changes.

Because Pi and uv tools are installed for `agent`, the matching `sbx` config should use:

```toml
run_user = "agent"
```

## 2. Build the image

Run:

```bash
./scripts/build-debian-image.py
```

Useful options:

```bash
./scripts/build-debian-image.py \
  --name debian-sbx \
  --rootfs-size-mb 40960
```

The script combines the base and agent Containerfiles, builds that combined Containerfile first, and passes the resulting Docker image into SmolVM's Debian image builder. If the combined Containerfile ends with `USER agent`, the script wraps it with a tiny `USER root` image so SmolVM's builder can still run its root-level SSH/init setup.

You can override the fragments:

```bash
./scripts/build-debian-image.py \
  --base-containerfile Containers/Debian/Base.Containerfile \
  --agent-containerfile Containers/Agents/Pi.Containerfile
```

Or pass a fully composed Containerfile directly:

```bash
./scripts/build-debian-image.py --containerfile path/to/Containerfile
```

By default the script prints only the built image paths and a minimal `sbx` config snippet. To also print a SmolVM SDK usage sketch after building, pass `--sdk-sketch`.

To print the SDK sketch later without rebuilding the image, run:

```bash
./scripts/build-debian-image.py --print-sdk-sketch ~/.smolvm/images/debian-sbx
```

The script also writes a local image manifest:

```text
~/.smolvm/images/debian-sbx/smolvm-image.json
```

and uses SmolVM's QEMU-compatible kernel by default.

## 3. Local image directory layout

After a successful build, the image directory should look like:

```text
~/.smolvm/images/debian-sbx/
├── smolvm-image.json
├── vmlinux.bin
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

## 4. Configure `sbx`

Create or update `.sbx.toml`:

```toml
[sbx]
agent = "pi"
name = "debian-pi-test"
image = "~/.smolvm/images/debian-sbx"
memory = 2048
cpus = 2
disk_size = 40960
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

## 5. Run it

Use the configured name from `.sbx.toml`:

```bash
sbx --debug run
```

Passing a positional argument changes the VM name. For example:

```bash
sbx --debug run pi
```

means "run a sandbox named `pi`", not "run the Pi agent".

## Troubleshooting

### `QEMU exited early` / `Error loading uncompressed kernel without PVH ELF Note`

The image was likely built with SmolVM's Firecracker-compatible ELF kernel. Rebuild with the current `scripts/build-debian-image.py`; it selects SmolVM's QEMU-compatible kernel by default.

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

Pi is installed under `/home/agent/.local/bin/pi` by `Containers/Agents/Pi.Containerfile`. Ensure `.sbx.toml` includes:

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
