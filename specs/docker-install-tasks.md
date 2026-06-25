# Docker-in-sbx implementation tasks

## 1. Image build subcommand

- [x] Move the local Debian image builder into `src/sbx/image/build_debian.py`.
- [x] Move image-builder Containerfiles, kernel fragment, and rootless helper under `src/sbx/image/resources/` package resources.
- [x] Load packaged builder assets with `importlib.resources`; do not depend on repository-root paths.
- [x] Include Containerfile/kernel/helper package data in built wheels.
- [x] Wire it as `sbx image build-debian` instead of a standalone script or separate executable.
- [x] Update shell completions for `sbx image build-debian`.
- [x] Add `--with-docker` to the image builder.
- [x] Make `--with-docker` install Docker userspace and build/use a Docker-capable local kernel.
- [x] Keep Docker disabled by default.
- [x] When `--with-docker` is false, preserve the default compose order:

  ```text
  Containers/Debian/Base.Containerfile
  Containers/Agents/Pi.Containerfile
  ```

- [x] When `--with-docker` is true, compose:

  ```text
  Containers/Debian/Base.Containerfile
  Containers/Debian/fragments/Docker.Containerfile
  Containers/Agents/Pi.Containerfile
  ```

- [x] Ensure the Docker fragment contents are part of the composed Containerfile so the existing image fingerprint changes automatically.
- [x] Add/update the smallest image-builder tests covering default composition, Docker-fragment composition, Docker-kernel selection, and Docker init injection.
- [x] Do not add a Docker fragment override flag or generic fragment pipeline; use one optional `docker_containerfile: Path | None` in `main()` when `--with-docker` is set.
- [x] Keep Docker usage docs short and describe the final boot-time rootless startup only.

## 2. Docker image fragment

- [x] Create Docker package fragment; final packaged path is `src/sbx/image/resources/Containers/Debian/fragments/Docker.Containerfile`.
- [x] Install Docker/rootless packages from Docker's official Debian apt repository:

  ```dockerfile
  RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg iptables procps uidmap fuse-overlayfs slirp4netns \
      && install -m 0755 -d /etc/apt/keyrings \
      && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
      && chmod a+r /etc/apt/keyrings/docker.gpg \
      && . /etc/os-release \
      && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list \
      && apt-get update \
      && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras \
      && rm -rf /var/lib/apt/lists/*
  ```

- [x] Configure subordinate uid/gid ranges for `agent`, matching Docker rootless docs' 65,536-ID minimum:

  ```dockerfile
  RUN echo 'agent:100000:65536' >> /etc/subuid && \
      echo 'agent:100000:65536' >> /etc/subgid
  ```

- [x] Add `procps`; rootless startup needs `sysctl`.
- [x] Use `/usr/bin/dockerd-rootless.sh` from `docker-ce-rootless-extras`; do not include Debian `docker.io` contrib paths.
- [x] Do not mix Debian `docker.io`/`containerd` packages with Docker CE packages.
- [x] Keep the validated Docker CE package list.

## 3. Docker-capable kernel build

- [x] Do not modify or require a local SmolVM checkout.
- [x] Fetch SmolVM kernel build inputs from pinned GitHub raw URLs under `https://raw.githubusercontent.com/CelestoAI/SmolVM/20e1fdf72c2139622eb32ab21f288c7290bba7bf/`:

  ```text
  kernel/microvm/build.sh
  kernel/microvm/config.fragment
  kernel/microvm/config.amd64.fragment
  kernel/microvm/config.arm64.fragment
  kernel/microvm/linux.version
  kernel/microvm/linux.sha256
  ```

- [x] Add sbx-owned kernel fragment; final packaged path is `src/sbx/image/resources/kernel/docker.config.fragment`:

  ```text
  CONFIG_CGROUP_BPF=y
  CONFIG_CGROUP_DEVICE=y
  CONFIG_BPF=y
  CONFIG_BPF_SYSCALL=y
  CONFIG_MEMCG=y
  CONFIG_CGROUP_PIDS=y
  CONFIG_CPUSETS=y
  CONFIG_USER_NS=y
  CONFIG_TUN=y
  CONFIG_BRIDGE=y
  CONFIG_BRIDGE_NETFILTER=y
  CONFIG_VETH=y
  CONFIG_IP6_NF_TARGET_MASQUERADE=y
  CONFIG_IP_VS=y
  CONFIG_NETFILTER_XT_MATCH_IPVS=y
  CONFIG_IP_NF_RAW=y
  CONFIG_IP6_NF_RAW=y
  CONFIG_IP6_NF_NAT=y
  ```

- [x] Append the sbx Docker fragment to the downloaded SmolVM `config.fragment` in a temp build directory.
- [x] Add `src/sbx/image/resources/Containers/Build/Kernel.Containerfile` for kernel compile dependencies, including `ca-certificates` for HTTPS downloads.
- [x] Build/tag the kernel builder image from `_build_docker_kernel()`.
- [x] Run the downloaded SmolVM `kernel/microvm/build.sh` inside the kernel builder with `/work` bind-mounted.
- [x] Run Docker `check-config.sh` inside the kernel builder.
- [x] `chown -R <uid>:<gid> /work` after containerized kernel commands so temp cleanup is not blocked by root-owned files.
- [x] Store the produced QEMU kernel inside the image directory after the rootfs build:

  ```text
  ~/.smolvm/images/<name>/vmlinux-docker.bin
  ```

- [x] Make `smolvm-image.json` reference `vmlinux-docker.bin` when `--with-docker` is used.
- [x] Keep the kernel fragment minimal; rely on SmolVM `merge_config.sh` verification instead of adding speculative symbols.
- [x] Document Docker as the only extra host dependency for Docker-capable kernel/image builds; kernel compiler packages live in the packaged `Containers/Build/Kernel.Containerfile`.

- [x] Validate the built kernel config with Docker's own checklist:

  ```text
  https://raw.githubusercontent.com/moby/moby/master/contrib/check-config.sh
  ```

- [x] Add only missing symbols proven by `check-config.sh` or the Docker smoke tests.
- [x] Do not add `--build-docker-kernel`; `--with-docker` is the single user-facing flag.
- [x] Include `CONFIG_USER_NS=y` and `CONFIG_TUN=y` for rootless Docker.
- [x] Rebuild the Docker-capable image/kernel after kernel fragment changes.

## 4. Guest rootless Docker boot helper

- [x] Install one packaged guest helper in Docker-capable images:

  ```text
  /usr/local/bin/sbx-start-rootless-docker
  ```

- [x] Make the helper prepare runtime-only boot state: cgroup v2 mount, `/dev/net/tun`, `/run/user/1000`, `DOCKER_HOST`, `XDG_RUNTIME_DIR`, and temporary DNS workaround.
- [x] Make the helper start `dockerd-rootless.sh` as `agent` and exit successfully once Docker responds.
- [x] Do not ship repo-level `scripts/start-dockerd.sh`, `scripts/start-dockerd-rootless.sh`, or `scripts/start-dockerd-no-bridge.sh`; document rootful debug commands instead.
- [x] Keep Docker disabled by default unless the image is built with `--with-docker`.

## 5. SmolVM init injection

- [x] Add `SbxDockerImageBuilder` for `--with-docker` builds only.
- [x] Use SmolVM's protected `_base_init_script(custom_commands=...)` hook to start `/usr/local/bin/sbx-start-rootless-docker` from generated `/init`.
- [x] Add a `ponytail:` code comment at the protected SmolVM method override explaining the internal API dependency and upgrade path.
- [x] Keep `docs/fragile-glue.md` updated with this protected-method dependency.
- [x] Keep manifest `sbx.launch_command = "pi"`; do not wrap Pi with Docker startup.

## 6. Smoke tests

- [x] Rootful `hello-world` works as a troubleshooting path.
- [x] Rootless `docker run --rm hello-world` works after boot.
- [x] Use QEMU slirp DNS `10.0.2.3` until SmolVM fixes gateway resolver `10.0.2.2` upstream.
- [x] Validate `CONFIG_USER_NS=y` kernel change.
- [x] Validate `CONFIG_TUN=y` kernel change.
- [x] Rebuild after init injection and confirm rootless Docker is ready after VM boot without wrapping Pi or manually starting `dockerd`.
- [x] Run the smallest image-builder tests after cleanup.
- [x] Rebuild/smoke test the full image after cleanup.

## 7. Documentation

- [x] Document that this branch requires `smolvm==0.0.19` installed with `uv tool install`.
- [ ] Update `sbx` code to support the latest SmolVM version.
- [x] Update image build docs to use `sbx image build-debian`.
- [x] Document host usage: build the Docker-capable image, configure `.sbx.toml`, create/start a fresh VM.
- [x] Document rootless Docker starts at VM boot for Docker-capable images.
- [x] Keep Docker docs happy path short; move manual restart/rootful/DNS commands to Troubleshooting.
- [x] Document manual fallback command: `sbx-start-rootless-docker`.
- [x] Document rootful Docker as troubleshooting commands only, not shipped helper scripts.
- [x] Create `docs/fragile-glue.md` to track internal SmolVM method use and other fragile glue.
- [x] Document Docker data locations for rootless and rootful modes.
- [x] Document that mounted workspaces are allowed as build contexts initially; copy context into the VM disk if 9p is too slow or fails.
- [x] Do not add `sbx docker` helper tasks.
