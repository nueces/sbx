# Docker-in-sbx install plan

## Goal

Enable Docker to run inside an `sbx` managed SmolVM Debian VM.

Preference order:

1. Rootless Docker for the `agent` user.
2. Rootful Docker as an acceptable first milestone/proof of concept.

The implementation should preserve the existing `sbx` workflow and avoid requiring host Docker socket passthrough. Docker should run inside the guest VM.

## Current state

The current Debian sbx image:

- boots with a custom `/init`, not systemd;
- starts `sshd` directly;
- mounts basic filesystems: `/proc`, `/sys`, `/dev`, `/dev/pts`, `/run`;
- does not appear to mount cgroup v2 at `/sys/fs/cgroup`;
- uses a custom SmolVM kernel with built-in drivers/modules only;
- already has `CONFIG_OVERLAY_FS=y` for workspace mount overlay support;
- already has some nftables/netfilter options for Podman/Netavark-style networking.

Because the guest does not run systemd, commands like this should not be expected to work initially:

```bash
systemctl start docker
```

Docker daemons will need to be started directly or by `/init`/an sbx helper.

## Main blockers

### 1. Kernel support

Docker needs namespaces, cgroups, overlayfs, veth/bridge networking, and netfilter/NAT support. Rootless Docker also needs user namespaces and typically FUSE/TUN support.

Since SmolVM images currently use a kernel without loadable modules, required kernel features must be built in with `=y`.

### 2. Cgroup mount

The custom `/init` currently mounts basic filesystems but not cgroup v2. Docker/containerd normally require:

```text
/sys/fs/cgroup
```

### 3. Guest packages

The Debian image does not currently install Docker/containerd/rootless support packages.

### 4. No systemd

Docker package defaults assume systemd service management. The sbx image will need direct daemon startup.

## Required changes

### Kernel config

Update `SmolVM/kernel/microvm/config.fragment` and tests to include or verify required Docker symbols.

Rootful Docker baseline:

```text
CONFIG_NAMESPACES=y
CONFIG_PID_NS=y
CONFIG_NET_NS=y
CONFIG_IPC_NS=y
CONFIG_UTS_NS=y
CONFIG_CGROUPS=y
CONFIG_CGROUP_BPF=y
CONFIG_CGROUP_PIDS=y
CONFIG_CGROUP_FREEZER=y
CONFIG_CGROUP_DEVICE=y
CONFIG_MEMCG=y
CONFIG_CPUSETS=y
CONFIG_CFS_BANDWIDTH=y
CONFIG_VETH=y
CONFIG_BRIDGE=y
CONFIG_BRIDGE_NETFILTER=y
CONFIG_NETFILTER=y
CONFIG_NF_CONNTRACK=y
CONFIG_NF_NAT=y
CONFIG_IP_NF_IPTABLES=y
CONFIG_IP_NF_NAT=y
CONFIG_OVERLAY_FS=y
```

Rootless Docker additions:

```text
CONFIG_USER_NS=y
CONFIG_FUSE_FS=y
CONFIG_TUN=y
```

Notes:

- `CONFIG_OVERLAY_FS=y` already exists today.
- Existing netfilter/nftables entries may cover some of the list above; confirm with the final built kernel config.
- Add a test similar to the existing kernel config tests so regressions are caught.

### Guest init changes

Update SmolVM's generated `/init` script in `SmolVM/src/smolvm/images/builder.py` to mount cgroup v2:

```sh
mkdir -p /sys/fs/cgroup
mount -t cgroup2 none /sys/fs/cgroup || true
```

For rootless Docker, also evaluate whether this is needed/available:

```sh
sysctl -w kernel.unprivileged_userns_clone=1 || true
```

Also create rootless runtime directory for the default agent user if rootless support is enabled in the image:

```sh
mkdir -p /run/user/1000
chown agent:agent /run/user/1000
chmod 700 /run/user/1000
```

This can be image-specific rather than unconditional if desired.

### Guest image packages

Create a Docker-capable image variant, probably using a new Containerfile fragment.

Possible path:

```text
main/Containers/Debian/Docker.Containerfile
```

Rootful package baseline:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    containerd \
    runc \
    iptables \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
```

Rootless package additions:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    uidmap \
    dbus-user-session \
    fuse-overlayfs \
    slirp4netns \
    rootlesskit \
    && rm -rf /var/lib/apt/lists/*
```

Package names must be verified against the Debian release used by the image.

Configure subordinate uid/gid ranges for `agent`:

```dockerfile
RUN echo 'agent:100000:65536' >> /etc/subuid && \
    echo 'agent:100000:65536' >> /etc/subgid
```

Rootless shell env:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
export DOCKER_HOST=unix:///run/user/1000/docker.sock
```

## Manual proof of concept

### Rootful Docker POC

1. Rebuild the kernel with Docker-required config.
2. Rebuild a Docker-capable Debian sbx image.
3. Configure `.sbx.toml` to use the new image and a larger disk:

```toml
[sbx]
image = "~/.smolvm/images/debian-sbx-docker"
disk_size = 81920
```

4. Start a fresh VM.
5. Inside the VM, run:

```bash
sudo dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 &
sleep 3
sudo docker version
sudo docker run --rm hello-world
```

Expected result:

- `dockerd` starts;
- `docker version` can contact the daemon;
- `hello-world` runs and exits successfully.

### Rootless Docker POC

After rootful Docker works, test rootless:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
export DOCKER_HOST=unix:///run/user/1000/docker.sock
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

dockerd-rootless.sh >/tmp/dockerd-rootless.log 2>&1 &
sleep 5

docker version
docker run --rm hello-world
```

Expected result:

- rootless daemon starts as `agent`;
- Docker client talks to `/run/user/1000/docker.sock`;
- `hello-world` runs without root.

## sbx changes analysis

### Not required for first POC

No `sbx` CLI changes are strictly required for the first milestone. A Docker-capable image plus manual daemon startup should be enough to validate kernel/init/package support.

### Useful future sbx changes

Once Docker works manually, add ergonomic sbx support.

Potential config:

```toml
[sbx]
docker = "rootless" # false, "rootless", or "rootful"
```

Potential behavior for `sbx shell` / `sbx run`:

- if `docker = "rootless"`:
  - ensure `/run/user/<uid>` exists and is owned by the run user;
  - start `dockerd-rootless.sh` if not already running;
  - inject:

    ```bash
    XDG_RUNTIME_DIR=/run/user/<uid>
    DOCKER_HOST=unix:///run/user/<uid>/docker.sock
    ```

- if `docker = "rootful"`:
  - start `dockerd` if not already running;
  - optionally add `agent` to docker group or use `sudo docker`;
  - inject:

    ```bash
    DOCKER_HOST=unix:///var/run/docker.sock
    ```

Potential commands:

```bash
sbx docker start [NAME]
sbx docker status [NAME]
sbx docker logs [NAME]
sbx docker stop [NAME]
```

Potential diagnostics:

```bash
sbx doctor docker
```

This should check:

- kernel config/capabilities from inside the guest;
- cgroup mount state;
- required packages;
- daemon availability;
- rootless prerequisites: `newuidmap`, `newgidmap`, `/etc/subuid`, `/etc/subgid`, `slirp4netns`, `fuse-overlayfs`, `XDG_RUNTIME_DIR`.

## Implementation phases

### Phase 1: kernel/init/package validation

- Add required kernel config symbols.
- Add kernel config tests.
- Mount cgroup v2 in `/init`.
- Build a Docker-capable image variant.
- Manually start rootful Docker.
- Run `hello-world`.

Deliverable: rootful Docker works manually inside an sbx VM.

### Phase 2: rootless Docker validation

- Add/verify rootless packages.
- Configure `/etc/subuid` and `/etc/subgid` for `agent`.
- Ensure `/run/user/1000` exists with correct ownership.
- Manually start `dockerd-rootless.sh` as `agent`.
- Run `hello-world` rootless.

Deliverable: rootless Docker works manually inside an sbx VM.

### Phase 3: sbx ergonomics

- Add optional `[sbx].docker` config.
- Inject rootless Docker env vars during `sbx shell` / `sbx run`.
- Add daemon start/status/log helpers.
- Add diagnostics.

Deliverable: users can request Docker support from sbx config without remembering manual startup commands.

## Open questions

1. Which Docker distribution should be preferred in the guest?
   - Debian `docker.io`, simpler and apt-native.
   - Docker CE repo, often better rootless extras but adds external repo setup.

2. Should the Docker daemon start automatically on every VM boot, or only when requested by `sbx shell/run`?

3. Should rootful Docker be exposed to `agent` through the `docker` group, or should users use `sudo docker`?

4. Should Docker data live on the VM disk under `/var/lib/docker`, or should sbx support a separate larger extra drive for Docker images/layers?

5. Are 9p-mounted workspaces acceptable as Docker build contexts? If not, document copying build contexts into the VM disk first.

## Recommendation

Start with rootful Docker because it will expose missing kernel/cgroup/networking pieces quickly. Once rootful works, move to rootless Docker and then add `sbx` ergonomics.
