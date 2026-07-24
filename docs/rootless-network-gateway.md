# Rootless network gateway

The gateway is the non-SLIRP network path for mediated sandboxes:

```text
QEMU virtio-net -> Unix stream socket -> per-VM sbx-gateway worker -> per-VM mitmproxy mediator
```

Linux amd64 ships with an exact prebuilt worker in the Python package. Darwin amd64/arm64 workers are built and checksummed for release artifacts, but macOS runtime is **unverified** and has weaker confinement than Linux. End users do not need Go.

## Runtime provisioning

The worker binary and `gateway_runtime/SHA256SUMS` are verified at startup. The mediator is created only from the committed `gateway_runtime/mediator/uv.lock` using:

```bash
uv 0.11.30 sync --locked --no-build --no-dev --managed-python --python 3.12.13
```

No source build, compiler, TAP/TUN device, sudo, capability, setuid helper, or host firewall rule is part of runtime setup. Set `SBX_UV_BIN` if `uv 0.11.30` is not first on `PATH`. Expert artifact overrides (`SBX_GATEWAY_BIN`, `SBX_GATEWAY_MEDIATOR_PYTHON`, `SBX_GATEWAY_MEDIATOR_LAUNCHER`) must be absolute, regular, non-group/world-writable files.

## Configuration example

```toml
[network]
default = "deny"

[[network.http]]
id = "pypi"
host = "pypi.org"
ports = [443]
methods = ["GET", "HEAD"]

[[network.tls_passthrough]]
id = "pinned"
host = "pinned.example.com"
ports = [443]

[[network.ingress]]
listen = "127.0.0.1:0"
guest_port = 3000
```

Old `[sbx].port_forwards` entries remain accepted and are normalized into ingress rules.

## Task 19 status

Passed: the bounded ten-minute mixed stability smoke completed under explicit TCG with bounded RSS, FDs, processes, and threads.

Deferred, not passed: readiness p95, absolute throughput, sustained 128-active HTTP request performance, and repeat four-VM TCG performance. Current preference for the follow-up is a warm launcher/forkserver with separate per-VM children; do **not** collapse VMs into one shared mitmproxy process without a new design decision.

## SmolVM migration condition

`sbx` keeps its typed external-network facade until a released SmolVM version exposes the same QEMU Unix-stream backend API. Migrate only after the upstream release preserves the single-NIC/no-SLIRP invariant and the existing QEMU argv tests pass unchanged.

## Security notes

- Unknown destinations, direct IP egress, alternate DNS, UDP/443/QUIC, identity mismatches, crashes, and unsupported protocols fail closed.
- Secrets stay host-side; the guest receives placeholders only, scoped by destination and approved fields.
- Linux uses rootless hardening where available. macOS is portable by design but unverified and less confined.
- Same-UID host processes remain a residual risk; use a separate OS account if that matters.

## Troubleshooting

- `uv 0.11.30 required`: install that uv version or set `SBX_UV_BIN` to it.
- `checksum mismatch`: reinstall the package; do not run a modified worker in release mode.
- `QEMU stream unsupported`: use QEMU 7.2+ and explicit TCG/KVM as appropriate.
- `gateway mode cannot fall back`: fix the gateway error; SLIRP is intentionally not used as a backup.

## Release checklist

1. Build Linux amd64 and Darwin amd64/arm64 workers; update `SHA256SUMS`.
2. Verify mediator lock syncs wheel-only with uv 0.11.30 and CPython 3.12.13.
3. Run Section 22 functional/security acceptance under explicit TCG.
4. Run help/completion/doc example parse checks, tests, ruff, Go tests, audits, and `git diff --check`.
5. Record macOS as unverified unless it was actually run.

## Rollback checklist

1. Disable `[network]` gateway mode in affected configs.
2. Stop affected VMs: `sbx stop NAME`.
3. Remove stale gateway state under the user sbx state directory if diagnostics report it unsafe.
4. Reinstall the previous `sbx` package/tag.
5. Preserve `gateway.jsonl`/`mediator.jsonl` logs for investigation; they should be secret-redacted.
