# Image build supply-chain implementation tasks

## Source of truth

Implement against:

```text
specification/image-build-supply-chain/specs/image-build-supply-chain-design.md
```

Implementation belongs only in:

```text
/home/nueces/code/sbx/feature/image-build-supply-chain
branch: feature/image-build-supply-chain
```

Specification updates belong only in:

```text
/home/nueces/code/sbx/specification/image-build-supply-chain
branch: specification/image-build-supply-chain
```

Do not edit or commit directly to `main/` or `specification/main/`. Do not commit unless explicitly requested.

## Implementation principles

- Land Docker-by-default and its supply-chain hardening in the same feature; do not leave an intermediate releasable state that routes every build through mutable scripts.
- Build one packaged image variant: Debian + rootless Docker + Pi.
- Use packaged resources instead of build-time script downloads.
- Reuse the smallest available SmolVM rootfs/ext4 seam, but never call an API that downloads a kernel or injects the guest agent.
- Keep `smolvm==0.0.28` pinned; do not combine a SmolVM upgrade with this feature.
- Keep custom Containerfiles supported without pretending they contain Docker.
- Use SSH explicitly for local images; do not add a communication-channel option.
- Preserve existing image compatibility through each image's manifest.
- Test with fakes and temporary files; automated tests must not contact Docker, GitHub, package registries, QEMU, or SSH.

## Phase 0 — establish the baseline and implementation seam

### T001 — Verify both worktrees

- [ ] Confirm `feature/image-build-supply-chain/` is on `feature/image-build-supply-chain`.
- [ ] Confirm `specification/image-build-supply-chain/` is on `specification/image-build-supply-chain`.
- [ ] Confirm the implementation worktree is clean before editing.
- [ ] Read the workspace and implementation-worktree `AGENTS.md` files that apply.
- [ ] Confirm `pyproject.toml` remains pinned to `smolvm==0.0.28`.
- [ ] Do not regenerate `uv.lock` in this feature.

### T002 — Run and record the pre-change baseline

- [ ] Run the full test suite:

  ```bash
  cd /home/nueces/code/sbx/feature/image-build-supply-chain
  UV_PROJECT_ENVIRONMENT=/tmp/sbx-image-build-supply-chain-venv \
    uv run --python /usr/bin/python3 --extra dev pytest --no-cov
  ```

- [ ] Run lint:

  ```bash
  UV_PROJECT_ENVIRONMENT=/tmp/sbx-image-build-supply-chain-venv \
    uv run --python /usr/bin/python3 --extra dev ruff check src tests
  ```

- [ ] Record any pre-existing failure instead of hiding it with unrelated changes.

### T003 — Trace all affected image-build and local-image paths

- [ ] Locate every source, test, completion, README, and documentation reference to:
  - `--with-docker`;
  - `with_docker`;
  - `--kernel-url`;
  - `BASE_KERNELS` in the image builder;
  - `SMOLVM_KERNEL_REF`, `SMOLVM_RAW_BASE`, and `_download()`;
  - `vmlinux-docker.bin`;
  - `build_debian_ssh_key()`;
  - `SbxDockerImageBuilder`;
  - `smolvm-guest-agent`; and
  - local-image `VMConfig` construction.
- [ ] Confirm existing local images resolve their kernel from `smolvm-image.json` rather than assuming a filename.
- [ ] Confirm custom Containerfile tests and behavior before changing their Docker feature metadata.

### T004 — Use the narrow rootfs-only compatibility seam

- [ ] Use SmolVM 0.0.28 `DockerRootfsBuilder._build_rootfs()` with an `ImageBuilder` helper for Docker export and ext4 conversion.
- [ ] Keep that private call contained in `src/sbx/image/build_debian.py`, covered by tests, and recorded in `docs/fragile-glue.md`.
- [ ] Do not call `ImageBuilder.build_debian_ssh_key()` or `DockerRootfsBuilder.build_boot_image()`; both add an unwanted artifact.

## Phase 1 — vendor executable kernel inputs

### T005 — Add the reviewed SmolVM kernel recipe files as package resources

- [ ] Add these exact files under a single packaged resource directory such as `src/sbx/image/resources/kernel/smolvm/`:

  ```text
  build.sh
  config.fragment
  config.amd64.fragment
  config.arm64.fragment
  linux.version
  linux.sha256
  ```

- [ ] Source them from SmolVM commit:

  ```text
  20e1fdf72c2139622eb32ab21f288c7290bba7bf
  ```

- [ ] Preserve Linux version `6.12.85` and expected tarball SHA-256:

  ```text
  e35ac999f40a6874493d8d60f33f1150d7a89ae5841c428da82257fbcd070aed
  ```

- [ ] Review the vendored `build.sh` before adding it; do not blindly copy a working-tree or cache file.
- [ ] Record the source repository and commit in a short adjacent resource comment or existing implementation documentation; do not add a generic provenance framework.
- [ ] Confirm all six files are included in the built Python wheel through the existing resource package.

### T006 — Vendor Docker's kernel configuration checker

- [ ] Select and record one reviewed Moby commit rather than `master`.
- [ ] Add that revision's `contrib/check-config.sh` under `src/sbx/image/resources/kernel/`.
- [ ] Record the source commit beside the resource or in existing implementation documentation.
- [ ] Do not retain a URL fallback, refresh flag, downloader, mirror list, or runtime version check.
- [ ] Confirm the checker is included in the built wheel.

### T007 — Replace network downloads with packaged copies

- [ ] Remove `urllib.request`, `_download()`, `SMOLVM_RAW_BASE`, and download-only constants from `build_debian.py`.
- [ ] Replace the six SmolVM download calls with copies from packaged resources into the temporary kernel work directory.
- [ ] Copy the packaged `check-config.sh` into the same temporary directory.
- [ ] Keep temporary scripts executable only for the duration of the build.
- [ ] Use the existing sbx Docker kernel fragment by appending it to the copied SmolVM `config.fragment`.
- [ ] Prove no image-build source references `raw.githubusercontent.com/CelestoAI/SmolVM` or `moby/moby/master`.

### T008 — Preserve Linux source integrity failure behavior

- [ ] Keep the vendored `linux.sha256` check before Linux extraction or compilation.
- [ ] Ensure a mismatch stops the kernel build before any output kernel is installed.
- [ ] Ensure the failure identifies the Linux source artifact, expected digest, actual digest, and deliberate pin-update action.
- [ ] Do not retry another URL or bypass verification.
- [ ] Add the smallest runnable check for mismatch behavior; it may invoke the script with fake/local inputs but must not contact kernel.org.

## Phase 2 — build the rootfs without a kernel or guest agent

### T009 — Define the sbx-owned SSH-ready rootfs Dockerfile content

- [ ] Preserve the current locally composed base image as the `FROM` input.
- [ ] Add only the setup currently supplied by SmolVM's Debian SSH builder:
  - `openssh-server`;
  - `iproute2`;
  - `curl`;
  - `bash`;
  - `ca-certificates`;
  - `python3` if still required by other image behavior;
  - SSH runtime directories and key-only configuration; and
  - `COPY init /init` with executable permissions.
- [ ] Preserve first-boot SSH host-key generation and kernel-command-line public-key injection performed by `/init`.
- [ ] Do not copy `/usr/local/bin/smolvm-guest-agent` into the Docker context or rootfs.
- [ ] Do not add another init system or service manager.

### T010 — Keep one minimal SmolVM-compatible `/init`

- [ ] Rename the existing subclass to `SbxImageBuilder` and use `_base_init_script(custom_commands=...)` for SmolVM mounts, networking, SSH, clock handling, shutdown, and rootless Docker startup.
- [ ] Leave the generated guest-agent check harmless: the rootfs contains no agent binary.
- [ ] Do not add a second boot-hook framework, script registry, or runtime option.

### T011 — Add a rootfs-only build operation

- [ ] Replace `build_debian_ssh_key()` with one sbx-local rootfs-only helper.
- [ ] Build the SSH-ready Docker image, export its filesystem, and create `rootfs.ext4` through the narrow SmolVM Docker/export/ext4 seam confirmed in T004.
- [ ] Write the rootfs directly under the requested image directory:

  ```text
  <cache-dir>/<name>/rootfs.ext4
  ```

- [ ] Do not call SmolVM kernel resolution, `ensure_base_kernel`, `BASE_KERNELS`, or guest-agent download/build functions.
- [ ] Preserve `--cache-dir`, `--name`, `--rootfs-size-mb`, host architecture selection, and friendly Docker failure reporting.
- [ ] Remove `--ssh-public-key`; local images inject the launching VM's key at boot, so a build-time key is unused and must not be baked into the shared rootfs.
- [ ] Build to a temporary sibling file and atomically replace `rootfs.ext4` only after success.
- [ ] Remove partial temporary rootfs output on exception or interruption.

### T012 — Preserve rootfs cache invalidation without a new cache subsystem

- [ ] Fingerprint only inputs that affect the rootfs:
  - rootfs size;
  - composed/local base image tag or its existing content-derived tag;
  - generated SSH-ready Dockerfile;
  - generated init script; and
  - architecture where it affects the Docker build.
- [ ] Reuse the existing image directory and one fingerprint file.
- [ ] Skip rootfs rebuilding only when both `rootfs.ext4` and its fingerprint match.
- [ ] Do not include SSH public-key contents; keys remain per-VM boot inputs.
- [ ] Do not create a cache class, schema migration framework, or compatibility registry.

### T013 — Remove guest-agent acquisition from image builds

- [ ] Add one focused assertion that the rootfs build context and Dockerfile contain no `smolvm-guest-agent` artifact or `COPY` instruction.

## Phase 3 — make Docker the packaged default

### T014 — Remove the Docker opt-in flag and branches

- [ ] Remove `--with-docker` from `add_arguments()`.
- [ ] Remove `args.with_docker`, conditional builder selection, and the custom-Containerfile incompatibility error.
- [ ] Always select the packaged Docker fragment for the packaged base/agent composition path.
- [ ] Preserve composition order:

  ```text
  Debian base
  Docker fragment
  Pi/agent tooling
  ```

- [ ] Always inject rootless Docker startup into the packaged image init.
- [ ] Do not add `--without-docker`, `--minimal`, an environment variable, or hidden configuration replacement.

### T015 — Preserve explicit custom Containerfile behavior

- [ ] Continue accepting `--containerfile` and its `--dockerfile` alias.
- [ ] Do not append the packaged Docker userland fragment to an arbitrary custom Containerfile.
- [ ] Build the Docker-capable kernel for custom images so their kernel supports Docker when their userland does.
- [ ] Set custom-image `sbx.features` to `[]`; do not infer Docker from filenames or inspect package contents.
- [ ] Keep the Docker startup init command harmless when the custom rootfs does not provide `/usr/local/bin/sbx-start-rootless-docker`.
- [ ] Do not add a custom feature flag or image inspection framework.

### T016 — Remove the kernel URL override and published-kernel dependency

- [ ] Remove `--kernel-url` from the parser.
- [ ] Remove `args.kernel_url`, `BASE_KERNELS`, and all published-kernel selection logic from `build_debian.py`.
- [ ] Remove `kernel_url` from the image-build JSON payload.
- [ ] Remove text describing a SmolVM-published kernel source from build output.
- [ ] Confirm neither packaged nor custom builds download or retain a SmolVM published kernel.

### T017 — Standardize the built kernel filename and manifest

- [ ] Rename the output constant to use:

  ```text
  vmlinux.bin
  ```

- [ ] Build the kernel in temporary output and atomically replace `<image-dir>/vmlinux.bin` only after compilation and `check-config.sh` succeed.
- [ ] Make new packaged manifests contain:

  ```json
  {
    "kernel": "vmlinux.bin",
    "rootfs": "rootfs.ext4",
    "sbx": {
      "agent": "pi",
      "features": ["docker"],
      "launch_command": "pi"
    }
  }
  ```

- [ ] Make custom-image manifests use the same kernel filename with `features: []`.
- [ ] Keep runtime compatibility with older manifests referencing `vmlinux-docker.bin` or another relative kernel filename.

### T018 — Simplify image-build output

- [ ] Remove the `with_docker`, `kernel_url`, and `kernel_source` payload fields.
- [ ] Keep `docker_containerfile` populated for the packaged recipe and `null` for custom Containerfiles.
- [ ] Print the kernel path without a source suffix in the human summary.
- [ ] Do not add new provenance, result, or manifest fields.
- [ ] Preserve the remaining human summary, `--json`, and sbx config snippet shapes.

## Phase 4 — force local-image communication through SSH

### T019 — Set the local-image VM communication channel

- [ ] Add `comm_channel="ssh"` to the `VMConfig` created by `_start_local_image()`.
- [ ] Keep `ssh_capable=True`, SSH public-key injection, boot timeout, mounts, ports, disk growth, and backend behavior unchanged.
- [ ] Do not add a manifest field, TOML setting, or CLI flag for communication-channel selection.
- [ ] Do not alter preset-backed VM communication in this feature.

### T020 — Verify local-image SSH behavior

- [ ] Assert the local-image `VMConfig` has `comm_channel="ssh"` and retain existing startup, environment-sync, hostname, and attachment regression tests.
- [ ] Do not rewrite existing SSH helpers unless a regression test demonstrates incompatibility.

## Phase 5 — update automated tests

### T021 — Rewrite image-builder fakes around rootfs-only output

- [ ] Remove fake `build_debian_ssh_key()` implementations and fake `BASE_KERNELS` modules from `tests/test_build_debian_image.py`.
- [ ] Fake the new rootfs-only helper and Docker-kernel helper independently.
- [ ] Assert neither fake receives or returns a published kernel URL.
- [ ] Keep failure tests for rootfs build failure, kernel build failure, and friendly CLI errors.

### T022 — Replace optional-Docker tests with default-Docker tests

- [ ] Rewrite the default build test to prove Docker fragment ordering and rootless startup injection without `--with-docker`.
- [ ] Assert the default manifest has `features == ["docker"]`.
- [ ] Assert the default kernel and manifest use `vmlinux.bin`.
- [ ] Delete the obsolete test that rejects `--with-docker` plus `--containerfile`.
- [ ] Add parser tests proving `--with-docker` and `--kernel-url` are rejected as unknown arguments.
- [ ] Preserve JSON and human summary coverage with obsolete conditional fields removed.

### T023 — Test vendored kernel inputs

- [ ] Assert all six SmolVM recipe files and packaged `check-config.sh` are available through `_packaged_resources()`.
- [ ] Assert `_build_docker_kernel()` copies packaged inputs and never performs network script downloads.
- [ ] Assert the sbx Docker config fragment is appended once.
- [ ] Assert `build.sh` runs before `check-config.sh`.
- [ ] Assert the kernel is installed only after both commands succeed.
- [ ] Assert failure removes temporary output and does not replace a previously valid `vmlinux.bin`.
- [ ] Preserve the ownership-repair `chown` assertion.

### T024 — Test packaged and custom manifests separately

- [ ] Packaged recipe: assert Docker layer included, Docker boot helper referenced, and `features == ["docker"]`.
- [ ] Custom Containerfile: assert no packaged Docker layer is appended and `features == []`.
- [ ] Both paths: assert the Docker-capable kernel is built and referenced as `vmlinux.bin`.
- [ ] Existing-image listing/runtime tests: retain fixtures with `vmlinux-docker.bin` and prove manifest-driven compatibility.

### T025 — Update CLI dispatch and completion tests

- [ ] Rewrite `tests/test_cli.py` image-build dispatch assertions without `with_docker`.
- [ ] Remove `--with-docker` and `--kernel-url` from static completion option tables.
- [ ] Update bash, zsh, and fish completion expectations.
- [ ] Keep `image build-debian` and all remaining options discoverable.

### T026 — Add SSH-only local-image tests

- [ ] Capture `VMConfig` in local-image CLI tests and assert `comm_channel == "ssh"`.
- [ ] Assert local image creation/start does not choose vsock even when SmolVM would otherwise auto-select it.
- [ ] Assert environment sync, hostname setup, and attachment behavior remain unchanged.
- [ ] Do not require a real VM or SSH server.

### T027 — Run focused automated checks

- [ ] Run:

  ```bash
  cd /home/nueces/code/sbx/feature/image-build-supply-chain
  UV_PROJECT_ENVIRONMENT=/tmp/sbx-image-build-supply-chain-venv \
    uv run --python /usr/bin/python3 --extra dev pytest --no-cov \
    tests/test_build_debian_image.py tests/test_cli.py \
    tests/test_cli_extra.py tests/test_completion.py
  ```

- [ ] Run Ruff on touched source and tests.
- [ ] Fix behavior rather than weakening security assertions.

## Phase 6 — update documentation and fragile seams

### T028 — Update image build documentation

- [ ] Update `README.md` and `docs/build-local-debian-pi-image.md` so the normal command produces a Docker-capable image.
- [ ] Remove all examples and prose using `--with-docker` or `--kernel-url`.
- [ ] Use `vmlinux.bin` in new image layouts.
- [ ] Explain that builds compile the kernel and therefore take longer and require network access for the SHA-verified Linux source and package repositories.
- [ ] Keep rootless Docker usage and troubleshooting commands accurate.
- [ ] Do not add a separate Docker tutorial.

### T029 — Update current-image and control-plane documentation

- [ ] Update `docs/current-local-image-usage.md` to describe the rootfs-only build path and locally built kernel.
- [ ] Update `docs/smolvm-guest-control-plane.md` to state that SmolVM 0.0.28 normally uses a static Rust guest agent, not the stale Python implementation description.
- [ ] State that sbx local images intentionally omit that agent and force SSH.
- [ ] Keep preset/prebuilt SmolVM behavior distinct; this feature changes local images only.

### T030 — Update the fragile-glue ledger

- [ ] Remove or rewrite the old optional `SbxDockerImageBuilder` entry.
- [ ] Record the exact private SmolVM rootfs/ext4 and init seams retained by the implementation.
- [ ] Explain why public SmolVM 0.0.28 builders are unsuitable: they download a kernel and/or inject the guest agent.
- [ ] Keep the exit condition concrete: adopt a public rootfs-only API that accepts init/context and does not resolve a kernel or inject an agent.
- [ ] Add one adjacent `ponytail:` comment in code; do not duplicate the ledger throughout the builder.

### T031 — Search for stale claims

- [ ] Search source, tests, README, and docs for:

  ```bash
  rg -n 'with-docker|with_docker|kernel-url|vmlinux-docker|published QEMU kernel|Python stdlib-only' \
    src tests README.md docs
  ```

- [ ] Keep `vmlinux-docker.bin` only in explicit backward-compatibility examples/tests.
- [ ] Remove stale “Docker optional” and “guest agent included” claims for newly built local images.

## Phase 7 — final security and quality validation

### T032 — Review the final build dependency boundary

- [ ] Prove image-build Python code performs no remote script download.
- [ ] Prove the only kernel source network fetch is Linux `6.12.85` and that it is verified before use.
- [ ] Prove no SmolVM published kernel or guest-agent binary is requested during a local image build.
- [ ] Prove packaged scripts come from wheel resources, not repository-relative paths.
- [ ] Prove no custom URL or bypass flag remains.

### T033 — Review failure and replacement behavior

- [ ] Trace rootfs success, failure, and interruption.
- [ ] Trace kernel compile failure, config-check failure, digest mismatch, and interruption.
- [ ] Confirm a valid existing rootfs/kernel is replaced only after a complete successful replacement exists.
- [ ] Confirm partial files and temporary build directories are cleaned.
- [ ] Confirm integrity errors remain actionable and no fallback disables verification.

### T034 — Review scope and deletion opportunities

- [ ] Confirm there is one packaged image composition and one kernel output name.
- [ ] Confirm there is no Docker feature toggle, kernel URL abstraction, downloader, artifact registry, or communication-channel option.
- [ ] Confirm custom Containerfiles use one explicit conservative metadata rule rather than runtime detection.
- [ ] Confirm no unrelated package pinning, SmolVM upgrade, lifecycle refactor, or hypervisor work entered the diff.
- [ ] Run an over-engineering review and remove dead conditionals/helpers left by the old two-variant path.

### T035 — Run static checks

- [ ] Run:

  ```bash
  cd /home/nueces/code/sbx/feature/image-build-supply-chain
  UV_PROJECT_ENVIRONMENT=/tmp/sbx-image-build-supply-chain-venv \
    uv run --python /usr/bin/python3 --extra dev ruff check src tests
  ```

- [ ] Run configured formatting or pre-commit checks only if required; avoid unrelated formatting churn.

### T036 — Run the full automated suite

- [ ] Run:

  ```bash
  UV_PROJECT_ENVIRONMENT=/tmp/sbx-image-build-supply-chain-venv \
    uv run --python /usr/bin/python3 --extra dev pytest --no-cov
  ```

- [ ] Re-run Ruff after final fixes.
- [ ] Confirm no generated virtual environments, caches, kernels, rootfs files, or Docker exports are tracked.

### T037 — Perform an optional end-to-end image smoke test

Only on a suitable Docker/QEMU host with disposable resources:

- [ ] Build a default image without `--with-docker`.
- [ ] Confirm the image directory contains `smolvm-image.json`, `rootfs.ext4`, and `vmlinux.bin`, with no `vmlinux-docker.bin`.
- [ ] Inspect the rootfs or boot the image and confirm `/usr/local/bin/smolvm-guest-agent` is absent.
- [ ] Start a fresh VM and confirm SSH readiness without a vsock delay.
- [ ] Confirm rootless Docker starts at boot.
- [ ] Run `docker run --rm hello-world` as `agent`.
- [ ] Confirm Pi launches and project mounts, Git config, forwarded environment, auth callback, shell, and stop-on-exit still work.
- [ ] Confirm no host Docker socket is mounted into the guest.
- [ ] Do not use production credentials.
- [ ] Mark this phase skipped, not failed, when the environment lacks Docker/QEMU support.

## Final definition of done

- [ ] `--with-docker`, `--kernel-url`, and the unused `--ssh-public-key` are removed from parser, completion, tests, and docs.
- [ ] Packaged builds always include rootless Docker and report `features: ["docker"]`.
- [ ] Custom Containerfiles remain supported and conservatively report `features: []`.
- [ ] The six SmolVM recipe files and reviewed Moby checker are packaged with sbx.
- [ ] No executable build script is downloaded at image-build time.
- [ ] Linux source remains version-pinned and SHA-256 verified before compilation.
- [ ] Local image builds download neither a SmolVM published kernel nor guest-agent binary.
- [ ] New images contain only `vmlinux.bin`, `rootfs.ext4`, and their manifest as boot artifacts.
- [ ] New local-image rootfs files omit `/usr/local/bin/smolvm-guest-agent`.
- [ ] Local-image `VMConfig` explicitly uses `comm_channel="ssh"`.
- [ ] Existing manifests referencing `vmlinux-docker.bin` remain usable.
- [ ] Rootfs and kernel replacement is atomic and partial output is cleaned on failure.
- [ ] Focused tests pass.
- [ ] Full tests pass.
- [ ] Ruff passes.
- [ ] README, build documentation, control-plane documentation, and fragile-glue ledger are accurate.
- [ ] No unrelated changes or generated build artifacts are present.
