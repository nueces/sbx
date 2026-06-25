# Current local image usage

This document records how `sbx` currently uses local SmolVM images. It is descriptive, not a proposal for future distribution changes.

## Summary

`sbx` local image mode uses a ready-to-run image directory containing separate boot artifacts:

```text
~/.smolvm/images/debian-sbx/
├── smolvm-image.json
├── vmlinux.bin
└── rootfs.ext4
```

The image is not a single self-contained bootable disk. It is a direct-kernel SmolVM image: `sbx` provides SmolVM with a kernel path, root filesystem path, and boot arguments.

## Build flow

The local Debian/Pi image is built with:

```bash
python scripts/build-debian-image.py
```

The build recipe is split into a reusable base OS fragment and an agent/tooling fragment:

```text
Containers/
├── Debian/
│   └── Base.Containerfile
└── Agents/
    └── Pi.Containerfile
```

The build script:

1. combines the base and agent Containerfiles into a temporary Containerfile;
2. builds that combined Containerfile into a Docker image;
3. asks SmolVM's `ImageBuilder.build_debian_ssh_key(...)` to create a Debian SSH-capable rootfs from that Docker image;
4. uses SmolVM's published/base QEMU-compatible kernel;
5. writes `smolvm-image.json` next to the generated kernel and rootfs.

The resulting rootfs already contains Pi and related tooling from `Containers/Agents/Pi.Containerfile`. Therefore, `sbx` does not run SmolVM preset installation for this mode.

## Manifest

The local image manifest is `smolvm-image.json`:

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

`sbx` reads this manifest to locate the kernel/rootfs and to validate the configured agent.

## Runtime flow

A typical `.sbx.toml` uses the local image directory:

```toml
[sbx]
agent = "pi"
name = "reviewhero"
image = "~/.smolvm/images/debian-sbx"
run_user = "agent"
memory = 8192
cpus = 4
boot_timeout = 60
project_path = "."
writable_mounts = true
copy_host_credentials = false
git_config = true
```

When `sbx run` starts a missing VM from this image, `sbx`:

1. loads the image manifest;
2. resolves `kernel`, `rootfs`, optional `initrd`, and `boot_args`;
3. creates a SmolVM `VMConfig` using those paths;
4. starts the VM through the SmolVM SDK;
5. waits for SSH readiness;
6. prepares `run_user`, safe Git config, auth callback forwarding, and project cwd as configured;
7. launches the configured agent command, usually `pi`.

In code this path is handled by `src/sbx/cli.py` in `_start_local_image()`.

## SmolVM pieces used

Even in local image mode, `sbx` still uses SmolVM for:

- VM lifecycle and state management;
- QEMU backend startup;
- direct-kernel boot;
- kernel/rootfs attachment;
- workspace/project mounts;
- SSH readiness checks and SSH command generation;
- isolated per-VM disk materialization.

The kernel file in the image directory is produced from SmolVM's kernel assets. The rootfs is our built Debian/Pi filesystem.

## Disk behavior

By default, SmolVM materializes an isolated per-VM disk from the local rootfs. This means multiple VMs can use the same local image directory without mutating the shared base rootfs.

Conceptually:

```text
base image rootfs:
  ~/.smolvm/images/debian-sbx/rootfs.ext4

per-VM materialized disks:
  ~/.local/state/smolvm/disks/reviewhero.ext4
  ~/.local/state/smolvm/disks/pi.ext4
```

Changing or resizing one VM's materialized disk does not resize the base image or other VMs.

If a base image is rebuilt, existing VMs may continue to use their already-materialized per-VM disks. To force a VM to pick up the rebuilt base image, remove/recreate that VM:

```bash
sbx rm reviewhero --force
sbx run
```

## `disk_size` and filesystem growth

If `[sbx].disk_size` is set and is larger than the image rootfs file size, SmolVM grows the host-side per-VM ext4 disk before boot. This requires host tools:

```bash
e2fsck
resize2fs
```

These tools must be available on the host PATH. Tools installed inside the guest VM do not help, because the resize happens before the VM boots.

If the tools are installed under `/usr/sbin` or `/sbin` but not on PATH, run:

```bash
PATH="$PATH:/usr/sbin:/sbin" sbx run
```

## Boot timeout

Local images can take longer than the default SmolVM SSH readiness timeout during cold boot. `sbx` defaults to:

```toml
boot_timeout = 60
```

This value is passed to SmolVM start/wait operations. If a VM starts but SSH is not ready in time, the VM may still be running and usable shortly afterward. Retry `sbx run`, or increase the timeout:

```bash
sbx run --boot-timeout 90
```

or:

```toml
[sbx]
boot_timeout = 90
```

## What local image mode is not

Current Linux local image mode is not:

- a single self-contained qcow2 disk with an internal bootloader;
- a single compressed distribution artifact;
- SmolVM's Windows local image mode;
- SmolVM preset install-at-boot mode.

For Linux/Pi, the current artifact format is a directory containing kernel, rootfs, and manifest metadata.
