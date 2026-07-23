# Image build supply-chain design

## Status

Proposed. This document records the current `sbx image build-debian` supply chain, the remaining SmolVM-sourced components, their security implications, and the minimum hardening work recommended for the matching implementation branch.

Analysis is based on `feature/smolvm-create-preset` with `smolvm==0.0.28`.

## Intent

Keep local Debian/Pi images locally assembled while making every executable build input pinned, integrity-checked, and necessary. The supported packaged image includes rootless Docker and its required kernel support by default; the separate `--with-docker` variant is removed.

The image builder should not:

- download unused boot artifacts;
- execute scripts from mutable branches; or
- include a root guest control plane unless `sbx` needs it.

## Current build flow

`sbx image build-debian` currently:

1. combines the packaged Debian base and Pi Containerfiles;
2. optionally inserts the packaged Docker fragment;
3. builds the combined Containerfile as a local Docker image;
4. passes that image to SmolVM `ImageBuilder.build_debian_ssh_key()`;
5. lets SmolVM add SSH packages, `/init`, and its guest agent;
6. exports the Docker filesystem into `rootfs.ext4`;
7. downloads a SmolVM-published QEMU kernel;
8. when `--with-docker` is selected, separately builds a Docker-capable kernel and points `smolvm-image.json` at that kernel; and
9. writes the local image manifest.

The resulting rootfs is assembled locally. No SmolVM preset or prebuilt SmolVM rootfs is downloaded for this path.

## Current supply chain and risks

The builder processes remote content with the host user's Docker access, which is commonly equivalent to host root on Linux. Built images later receive project mounts, selected environment values, and network access.

| Component | Current integrity | Risk | Required action |
| --- | --- | --- | --- |
| `smolvm==0.0.28` from PyPI | Version and wheel/sdist SHA-256 pinned in `uv.lock` | Its host build logic and generated `/init` are trusted code | Keep pinned |
| SmolVM QEMU kernel | Release SHA-256 verified | Downloaded and retained even when replaced by the Docker kernel | Stop downloading it |
| SmolVM Rust guest agent | Release SHA-256 verified | Root command/upload/download endpoint on vsock `1024` | Remove it and use SSH |
| Six SmolVM kernel recipe files | Git commit URL only | Remote executable build inputs lack sbx-owned integrity | Vendor the reviewed files |
| Linux `6.12.85` source | SHA-256 `e35ac999f40a6874493d8d60f33f1150d7a89ae5841c428da82257fbcd070aed` | Kernel controls guest data and can attack the QEMU boundary | Keep verification |
| Moby `check-config.sh` | Mutable `master`, no digest | Can modify the compiled kernel through the writable build mount | Vendor the reviewed file |
| Debian/Alpine base images and apt/npm/Git installers | Tags or unversioned repositories | Builds are not fully reproducible | Defer broader package pinning |

No SmolVM preset or prebuilt SmolVM rootfs is downloaded; the rootfs is assembled locally.

## Docker-by-default decision

The packaged Debian/Pi image includes Docker userland, rootless Docker startup, and the Docker-capable kernel by default. Remove `--with-docker` and the non-Docker packaged variant.

This makes builds and artifacts larger and slower and exposes every build to the custom-kernel pipeline. It also adds guest attack surface through dockerd, containerd, runc, BuildKit, namespaces, overlay filesystems, and virtual networking. Rootless Docker avoids a privileged daemon and host Docker socket, but `agent` already has passwordless sudo, so it is not a security boundary against the coding agent. QEMU remains the host isolation boundary.

Docker must become the default in the same change that vendors the mutable scripts and removes the unused published-kernel download. Existing images remain usable through their manifests.

Custom `--containerfile` builds receive the Docker-capable kernel but advertise the Docker feature only when their rootfs includes Docker userland.

## Proposed design

### 1. Make Docker the single packaged image variant

Remove `--with-docker`. The packaged build always composes:

```text
Debian base layer
Docker layer
Pi/agent tooling layer
```

It always installs and starts rootless Docker, builds the Docker-capable kernel, stores it as `vmlinux.bin`, and writes `sbx.features = ["docker"]`. Remove the `vmlinux-docker.bin` name: with one supported kernel variant, the suffix exposes an implementation detail without distinguishing anything.

Existing image directories remain compatible because their manifests continue to reference their actual kernel filenames. New and rebuilt images use the simplified `vmlinux.bin` layout.

Do not keep a hidden non-Docker switch. Reintroduce a smaller image only when measured build time, distribution size, or an explicit hardened-without-containers use case justifies a second supported artifact.

Because the locally built Docker kernel becomes the only kernel produced by this command, remove `--kernel-url`; it would otherwise configure only the discarded SmolVM kernel and have no effect on the final manifest.

### 2. Vendor executable build inputs

Package the reviewed Moby `check-config.sh` and six SmolVM kernel recipe files with `sbx`. Update them deliberately with the kernel recipe; do not download executable scripts during the build.

### 3. Remove the unused kernel download

Build/export the rootfs without asking SmolVM to download its published kernel. Then build the Docker-capable kernel and write the manifest.

Do not add a cache option for the unused kernel; eliminate the download. Do not retain `--kernel-url`, because this command no longer consumes a published or caller-supplied kernel.

### 4. Use SSH only

Omit `/usr/local/bin/smolvm-guest-agent` from local images and force `comm_channel="ssh"`. Local-image readiness, commands, file transfer, environment sync, and attachment must use the existing SSH path. Do not add an agent toggle.

## Failure behavior

Integrity failure must stop the build before the affected input is executed or copied into the image. Error output should identify:

- the artifact URL or packaged path;
- the expected digest;
- the actual digest; and
- the action required to update the pin deliberately.

The builder must remove partial kernel output after failure. It must not silently retry from another URL or disable verification.

## Validation

Automated tests should verify:

- `--with-docker` and `--kernel-url` are no longer accepted;
- the packaged image always includes the Docker layer and advertises the Docker feature;
- the build does not request or retain the unused published kernel;
- custom Containerfiles do not falsely advertise Docker when Docker userland is absent;
- no build path downloads or executes `moby/moby/master`;
- every remotely fetched executable input has an expected digest;
- a digest mismatch fails before execution;
- the locally built Docker-capable kernel is stored and referenced as `vmlinux.bin`;
- local images omit the guest agent and force SSH.

Tests must use fakes and temporary files. They must not contact GitHub, package registries, Docker, QEMU, or SSH.

## Acceptance criteria

This design is satisfied when:

- the default rootfs remains locally built rather than downloaded from a SmolVM preset;
- `--with-docker` and the non-Docker packaged variant are removed;
- packaged images include rootless Docker and advertise `sbx.features = ["docker"]`;
- the locally built Docker-capable kernel is the only kernel produced by the command and is named `vmlinux.bin`;
- builds no longer download the unused published kernel;
- no mutable branch script is executed during image construction;
- all remaining remote executable inputs are content-verified before execution;
- `--kernel-url` is removed with the discarded published-kernel path;
- the guest agent is removed and local-image communication explicitly uses SSH;
- the guest-control-plane documentation matches SmolVM 0.0.28 behavior; and
- focused tests cover integrity failures and SSH-only local-image communication.

