# SmolVM guest control plane

SmolVM 0.0.28 normally places a static Rust control-plane binary at:

```text
/usr/local/bin/smolvm-guest-agent
```

SmolVM downloads the architecture-specific binary from its GitHub release, verifies a package-pinned SHA-256 digest, and starts it as root on AF_VSOCK port `1024`. It supports readiness checks, command execution, upload, and download.

Anyone able to open that vsock endpoint controls the guest. This matches SmolVM's host-owns-guest model, but it is unnecessary attack surface for sbx local images.

## sbx local images

`sbx image build` intentionally omits the guest-agent binary. Local images instead set:

```python
comm_channel="ssh"
```

SSH handles readiness, commands, file transfer, environment synchronization, shell, and agent attachment. Administration uses root; shell/agent attachment authenticates directly as the runtime user. Creation or explicit reprovisioning authorizes the existing public key in the user's `authorized_keys`, after optional initial credential copying. Installation follows `ssh-copy-id`'s append-only approach with required permissions and filesystem safeguards, not temporary-file replacement or custom interruption recovery. Existing identities are detected without parsing their free-text comments as shell syntax. Existing entries are preserved and sshd enforces their restrictions; SBX does not interpret policy or fall back to root. Normal-user public-key login uses image SSH defaults, with any required adjustments made in the image Containerfile. SmolVM's generated init may retain a harmless executable-file check for the agent, but no listener starts because the binary is absent.

This policy applies only to sbx-built local images. SmolVM preset and published images may still include and use the guest agent.
