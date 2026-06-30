# Docker-in-sbx design

## Goal

Enable Docker to run inside an `sbx` managed SmolVM Debian VM, without host Docker socket passthrough.

Docker support is opt-in: normal images stay small and unchanged; Docker-capable images are built with:

```bash
sbx image build-debian --with-docker --name debian-sbx-docker
```

Docker-capable images start rootless Docker automatically at boot for the `agent` user. Rootful Docker is a troubleshooting path only.

## Final design

### User workflow

Install `sbx` with `uv tool install`; `sbx` brings its pinned SmolVM dependency with it:

```bash
uv tool install --editable .
```

Build a Docker-capable image:

```bash
sbx image build-debian \
  --with-docker \
  --name debian-sbx-docker \
  --rootfs-size-mb 81920
```

Configure `.sbx.toml` to use the image directory and run as `agent`:

```toml
[sbx]
image = "~/.smolvm/images/debian-sbx-docker"
run_user = "agent"
```

After boot, rootless Docker should already be running:

```bash
docker version
docker run --rm hello-world
```

Keep the image manifest launch command unchanged and mark Docker support explicitly:

```json
{
  "sbx": {
    "launch_command": "pi",
    "features": ["docker"]
  }
}
```

Non-Docker image builds should write `"features": []`. Docker startup is an image boot concern, not a Pi wrapper.

### Packaged image builder

Expose the local Debian image builder as an advanced `sbx` subcommand:

```bash
sbx image build-debian
```

Keep the implementation and assets under package resources so pip/wheel installs contain everything needed:

```text
src/sbx/image/
├── build_debian.py
└── resources/
    ├── Containers/
    │   ├── Agents/Pi.Containerfile
    │   ├── Debian/Base.Containerfile
    │   ├── Debian/fragments/Docker.Containerfile
    │   └── Build/Kernel.Containerfile
    ├── kernel/docker.config.fragment
    └── scripts/sbx-start-rootless-docker
```

Load packaged assets with `importlib.resources`; do not derive paths from a repository root. Include Containerfile, kernel fragment, and helper script package data in the wheel.

Default build composition:

```text
Containers/Debian/Base.Containerfile
Containers/Agents/Pi.Containerfile
```

Docker build composition:

```text
Containers/Debian/Base.Containerfile
Containers/Debian/fragments/Docker.Containerfile
Containers/Agents/Pi.Containerfile
```

The Docker fragment content must be part of the composed Containerfile so the existing build fingerprint changes automatically. Do not add a Docker fragment override flag or a generic fragment pipeline; one opt-in Docker fragment is enough.

The generated `smolvm-image.json` should include `sbx.features = ["docker"]` for Docker-capable images and `sbx.features = []` otherwise, so `sbx image ls` can display features without guessing from file names.

### Docker-capable kernel

`--with-docker` builds a local Docker-capable kernel from pinned SmolVM kernel build inputs. Do not modify or require a local SmolVM checkout.

Pinned SmolVM source ref:

```text
20e1fdf72c2139622eb32ab21f288c7290bba7bf
```

Fetch these files from that ref:

```text
kernel/microvm/build.sh
kernel/microvm/config.fragment
kernel/microvm/config.amd64.fragment
kernel/microvm/config.arm64.fragment
kernel/microvm/linux.version
kernel/microvm/linux.sha256
```

Append sbx's packaged Docker kernel fragment:

```text
src/sbx/image/resources/kernel/docker.config.fragment
```

Required Docker additions:

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

Validate the built config with Docker's checklist:

```text
https://raw.githubusercontent.com/moby/moby/master/contrib/check-config.sh
```

Store the produced QEMU kernel inside the image directory and reference it from `smolvm-image.json`:

```text
~/.smolvm/images/<name>/vmlinux-docker.bin
```

Do not add a separate `--build-docker-kernel` flag; `--with-docker` means Docker userspace plus Docker-capable kernel.

### Kernel builder container

Host kernel compile dependencies are provided by the packaged builder Containerfile:

```text
src/sbx/image/resources/Containers/Build/Kernel.Containerfile
```

The host only needs Docker plus the `uv tool install`ed `sbx`/`smolvm` tools. The builder image installs:

```text
build-essential flex bison bc libssl-dev libelf-dev dwarves
ca-certificates curl xz-utils tar coreutils binutils procps apparmor
```

Run SmolVM `build.sh` and Docker `check-config.sh` inside the builder image with the kernel work directory bind-mounted at `/work`, then run:

```bash
chown -R <uid>:<gid> /work
```

so temp cleanup is not blocked by root-owned files.

### Guest Docker packages

Install Docker from Docker's official Debian apt repository, not Debian `docker.io` packages:

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

`procps` is required because `dockerd-rootless.sh` calls `sysctl`. `docker-ce-rootless-extras` installs `/usr/bin/dockerd-rootless.sh`; no Debian `docker.io` contrib path is needed. Do not mix Debian `docker.io`/`containerd` packages with Docker CE packages.

Configure subordinate uid/gid ranges for `agent`:

```dockerfile
RUN echo 'agent:100000:65536' >> /etc/subuid && \
    echo 'agent:100000:65536' >> /etc/subgid
```

Install rootless Docker shell env for `agent`:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
export DOCKER_HOST=unix:///run/user/1000/docker.sock
```

### Guest boot startup

The Debian image boots with SmolVM's custom `/init`, not systemd. Do not expect this to work:

```bash
systemctl start docker
```

Install one guest helper:

```text
/usr/local/bin/sbx-start-rootless-docker
```

For Docker-capable images, use an sbx `ImageBuilder` subclass to inject startup commands through SmolVM's internal `_base_init_script(custom_commands=...)` hook:

```python
class SbxDockerImageBuilder(ImageBuilder):
    def _default_init_script(self) -> str:
        return self._base_init_script(
            custom_commands="""
if [ -x /usr/local/bin/sbx-start-rootless-docker ]; then
    /usr/local/bin/sbx-start-rootless-docker >/var/log/sbx-rootless-docker.log 2>&1 &
fi
"""
        )
```

Mark the protected-method use with a `ponytail:` comment and track it in `docs/fragile-glue.md`. Replace it when SmolVM exposes a public boot-hook API.

The helper should:

- mount cgroup v2 at `/sys/fs/cgroup`;
- create `/dev/net/tun`;
- prepare `/run/user/1000`;
- apply the temporary QEMU slirp DNS workaround with `10.0.2.3`;
- start `/usr/bin/dockerd-rootless.sh` as `agent`;
- exit successfully once Docker responds.

Do not ship repo-level `scripts/start-dockerd*.sh`; keep rootful debug commands in docs only.

### Docker storage

Rootless Docker data lives on the VM disk under:

```text
/home/agent/.local/share/docker
```

Rootful troubleshooting data lives under:

```text
/var/lib/docker
```

Use a larger rootfs/disk size first. Add a separate drive only if space becomes a real problem. Mounted workspaces are allowed as build contexts initially; if 9p is slow or fails, copy the build context into the VM disk before building.

## Validation

Run the smallest code checks after implementation changes:

```bash
ruff check src/sbx/cli.py src/sbx/image/build_debian.py tests/test_build_debian.py
pytest --no-cov tests/test_build_debian.py
sh -n src/sbx/image/resources/scripts/sbx-start-rootless-docker
```

Build and smoke-test a Docker-capable image:

```bash
sbx image build-debian --with-docker --name debian-sbx-docker --rootfs-size-mb 81920
sbx rm <vm-name> --force
sbx run
```

Inside the guest:

```bash
docker version
docker run --rm hello-world
```

Rootful Docker is only a troubleshooting path, documented as commands rather than shipped scripts:

```bash
sudo mkdir -p /sys/fs/cgroup
sudo mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true
sudo dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 &
sudo docker run --rm hello-world
```

## Decisions

1. Use Docker's official Debian apt repository for Docker packages.
2. Make Docker support opt-in with `--with-docker`.
3. Auto-start rootless Docker on boot for Docker-capable images via SmolVM init injection.
4. Keep `sbx.launch_command = "pi"`; Docker startup is an image boot concern, not an agent wrapper.
5. Expose image building as `sbx image build-debian`, not as a separate executable and not as `sbx docker`.
6. Put image commands under `src/sbx/image/` and package image-builder assets under `src/sbx/image/resources/` so pip/wheel installs work outside a source checkout.
7. Follow Docker/Podman's VM model: the guest Linux kernel must provide container features; sbx should not proxy the host Docker socket.
