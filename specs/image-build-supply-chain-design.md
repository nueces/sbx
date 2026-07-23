# Image build supply-chain design

## Status

Proposed. This document records the current `sbx image build-debian` supply chain, the remaining SmolVM-sourced components, their security implications, and the minimum hardening work recommended for the matching implementation branch.

Analysis is based on `feature/smolvm-create-preset` with `smolvm==0.0.28`.

## Intent

Offer one curated local sbx image through `sbx image build` while making every executable build input pinned, integrity-checked, and necessary. The image includes Debian, Pi, rootless Docker, and its required kernel support; Debian remains an implementation detail rather than part of the user-facing command.

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
| Six SmolVM kernel recipe files | Git commit URL only | Remote executable build inputs lack sbx-owned integrity | Pin immutable URLs and verify each SHA-256 |
| Linux `6.12.85` source | SHA-256 `e35ac999f40a6874493d8d60f33f1150d7a89ae5841c428da82257fbcd070aed` | Kernel controls guest data and can attack the QEMU boundary | Keep verification |
| Moby `check-config.sh` | Mutable `master`, no digest | Can modify the compiled kernel through the writable build mount | Pin an immutable URL and verify its SHA-256 |
| Debian/Alpine base images and apt/npm/Git installers | Tags or unversioned repositories | Builds are not fully reproducible | Defer broader package pinning |

No SmolVM preset or prebuilt SmolVM rootfs is downloaded; the rootfs is assembled locally.

## Docker-by-default decision

The packaged Debian/Pi image includes Docker userland, rootless Docker startup, and the Docker-capable kernel by default. Remove `--with-docker` and the non-Docker packaged variant.

This makes builds and artifacts larger and slower and exposes every build to the custom-kernel pipeline. It also adds guest attack surface through dockerd, containerd, runc, BuildKit, namespaces, overlay filesystems, and virtual networking. Rootless Docker avoids a privileged daemon and host Docker socket, but `agent` already has passwordless sudo, so it is not a security boundary against the coding agent. QEMU remains the host isolation boundary.

Docker must become the default in the same change that replaces mutable script downloads with immutable, SHA-256-verified downloads and removes the unused published-kernel download. Existing images remain usable through their manifests.

Custom `--containerfile` builds receive the Docker-capable kernel but advertise the Docker feature only when their rootfs includes Docker userland.

## Proposed design

### 1. Expose one curated image command

Rename `sbx image build-debian` to:

```bash
sbx image build
```

Remove `build-debian` rather than keeping an alias; add another recipe only when one exists. The internal Python module and Debian resource names may remain implementation-specific.

The curated image name defaults to `sbx`, producing:

```text
~/.smolvm/images/sbx
```

Keep `--name` as an explicit override. After a successful build, print the image path and show a next-step command using the existing project configuration flow:

```bash
sbx run the-quest \
  --image ~/.smolvm/images/sbx \
  --run-user agent \
  --project-path . \
  --writable-mounts \
  --write-config
```

`--write-config` remains a `run`/`create` option. `image build` must not modify the current project. The supplied `--image`, `--run-user`, `--project-path`, and `--writable-mounts` values are persisted by the existing project-config writer. Documentation must show this suggested configuration, including the safe defaults:

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

### 2. Make Docker the single packaged image variant

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

### 3. Download immutable, verified build inputs

Do not distribute copies of the SmolVM kernel recipe or Moby checker in the sbx wheel. Download the six SmolVM files from commit `20e1fdf72c2139622eb32ab21f288c7290bba7bf` and Moby `contrib/check-config.sh` from commit `b780867932842071ca38968da81ec52d8b70f0bc`.

Pin and verify each file independently before it is copied, modified, made executable, or executed:

| Input | Expected SHA-256 |
| --- | --- |
| SmolVM `build.sh` | `798ee1af08740bcd0348570151ae29b2ccbae9674443ad7c564de6f281bc8f75` |
| SmolVM `config.fragment` | `9df5020319525f1df6ada9ddc29eb334ed625edc1c08f347877eafe4e0d58215` |
| SmolVM `config.amd64.fragment` | `46649f51667d1ede8ae436c9cb8ce5c44cfa45c814847760445faa1aa596a3d7` |
| SmolVM `config.arm64.fragment` | `2f9b9db74a4581d77c8a794a826a6dc688358cf2e8d119e2dc8de8fad89a6b3e` |
| SmolVM `linux.version` | `f692c6ce637cb4bdf81a87c7818080995b9c888f842ae952aadc6cae632d24c1` |
| SmolVM `linux.sha256` | `30781e950c485491db11e248b785fa5e98d91f536889361b72f795d4d5c7d41f` |
| Moby `check-config.sh` | `fda4343e9b50c47896653ca774ccbe9614bfcdb60f080d2b6277baf27efc0a71` |

Use one small downloader based on the Python standard library. It must download to temporary files, reject mismatches with expected/actual diagnostics, and provide no caller-supplied URL, branch override, fallback mirror, or verification bypass.

Keep a developer maintenance script that checks the latest upstream versions of these paths and reports changed commits and digests. It must not silently update pins or executable files; a maintainer reviews changes and updates the commit and checksum together. The check also reports upstream LICENSE or NOTICE changes so a pin update includes a licensing review.

### 4. Remove the unused kernel download

Build/export the rootfs without asking SmolVM to download its published kernel. Then build the Docker-capable kernel and write the manifest.

Do not add a cache option for the unused kernel; eliminate the download. Do not retain `--kernel-url`, because this command no longer consumes a published or caller-supplied kernel.

### 5. Use SSH only

Omit `/usr/local/bin/smolvm-guest-agent` from local images and force `comm_channel="ssh"`. Local-image readiness, commands, file transfer, environment sync, and attachment must use the existing SSH path. Remove the unused build-time `--ssh-public-key`; SmolVM injects the launching VM's key at boot. Do not add an agent toggle.

## Failure behavior

Integrity failure must stop the build before the affected input is executed or copied into the image. Error output should identify:

- the artifact URL or packaged path;
- the expected digest;
- the actual digest; and
- the action required to update the pin deliberately.

The builder must remove partial kernel output after failure. It must not silently retry from another URL or disable verification.

## Validation

Automated tests should verify:

- `sbx image build` dispatches the curated builder, defaults to `~/.smolvm/images/sbx`, and `build-debian` is no longer accepted;
- `image build` does not write project configuration;
- `run`/`create --write-config` persists the selected local image and suggested project options;
- `--with-docker`, `--kernel-url`, and `--ssh-public-key` are no longer accepted;
- the packaged image always includes the Docker layer and advertises the Docker feature;
- the build does not request or retain the unused published kernel;
- custom Containerfiles do not falsely advertise Docker when Docker userland is absent;
- no build path downloads or executes a mutable branch URL such as `moby/moby/master`;
- all seven remotely fetched build inputs use immutable commit URLs and individual expected digests;
- a digest mismatch fails before execution;
- the locally built Docker-capable kernel is stored and referenced as `vmlinux.bin`;
- local images omit the guest agent and force SSH.

Tests must use fakes and temporary files. They must not contact GitHub, package registries, Docker, QEMU, or SSH.

## Acceptance criteria

This design is satisfied when:

- `sbx image build` is the only curated image build command, defaults to image name `sbx`, and `build-debian` is removed;
- documentation leads users from the curated build to `run`/`create --write-config` with the selected image and safe configuration;
- `image build` never modifies `.sbx.toml`;
- the default rootfs remains locally built rather than downloaded from a SmolVM preset;
- `--with-docker` and the non-Docker packaged variant are removed;
- packaged images include rootless Docker and advertise `sbx.features = ["docker"]`;
- the locally built Docker-capable kernel is the only kernel produced by the command and is named `vmlinux.bin`;
- builds no longer download the unused published kernel;
- no mutable branch script is downloaded or executed during image construction;
- all seven remote build inputs are fetched from immutable commits and content-verified before use;
- no copies of the SmolVM recipe or Moby checker are distributed in the sbx wheel;
- the maintenance checker reports upstream file and licensing changes without silently updating pins;
- `--kernel-url` and the unused build-time `--ssh-public-key` are removed;
- the guest agent is removed and local-image communication explicitly uses SSH;
- the guest-control-plane documentation matches SmolVM 0.0.28 behavior; and
- focused tests cover integrity failures and SSH-only local-image communication.

