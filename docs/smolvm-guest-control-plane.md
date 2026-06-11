# SmolVM guest control plane

SmolVM-built Linux images include a guest-side control-plane helper at:

```text
/usr/local/bin/smolvm-guest-agent
```

The file is copied from SmolVM's source module:

```text
smolvm/guest_agent/agent.py
```

## Purpose

The guest agent gives the host a control channel into the guest over AF_VSOCK. This lets SmolVM perform basic VM operations without depending solely on guest networking or SSH being ready.

Supported operations include:

- readiness ping
- run a shell command
- upload a file
- download a file

The agent listens on vsock port `1024` by default.

## Runtime model

The script is standalone and Python stdlib-only. It is copied into the image and launched by SmolVM's custom `/init` process.

SmolVM's Debian image builder appends this to its generated Dockerfile:

```dockerfile
COPY smolvm-guest-agent /usr/local/bin/smolvm-guest-agent
RUN chmod +x /usr/local/bin/smolvm-guest-agent
```

## Security model

The guest agent is a host-owned sandbox control channel. Anyone who can open the guest's vsock port can run commands in the guest, so it must not be exposed beyond the local host/sandbox boundary.

This matches SmolVM's existing trust model: the host owns the disposable guest.

## Disabling or avoiding the guest agent

There are two separate levels:

1. **Avoid using the guest agent from the host.** SmolVM APIs can be forced to use SSH with `comm_channel="ssh"`. In that mode, command execution and file transfer should use SSH instead of vsock. This does not remove the agent from the image; it only avoids selecting it as the control channel.
2. **Prevent the agent from running inside the guest.** SmolVM's custom `/init` starts the agent only when both `python3` and `/usr/local/bin/smolvm-guest-agent` exist. Removing the agent file from the rootfs, or patching `/init`, prevents it from starting.

Expected consequences of disabling the in-guest agent:

- Explicit vsock use, such as `comm_channel="vsock"` or CLI `--comm-channel vsock`, will fail or time out.
- Auto channel selection on Linux/QEMU may first probe vsock, wait briefly, then fall back to SSH.
- SSH-based functionality should continue to work if SSH is configured and reachable.
- `sbx` attach/shell/auth-port flows are primarily SSH-based and should not require the guest agent.
- SmolVM SDK operations such as `run`, upload, download, and env helpers should still work when the channel is explicitly set to SSH.

If we later decide to support agent-less images in `sbx`, the preferred approach is to force the SmolVM SDK channel to SSH for local ready-to-run image mode rather than exposing more user-facing options.

Related local-image workflow notes are in [`build-local-debian-pi-image.md`](build-local-debian-pi-image.md).
