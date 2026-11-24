# Gateway development

This page covers implementation boundaries and specialized stability validation. Shared setup/build/test commands live in [development](../docs/development.md#gateway-validation); user policy, support and audit semantics live in the [gateway guide](../docs/rootless-network-gateway.md). Check [dependency upgrade gates](../docs/dependency-upgrade.md) before changing pins. Cross-builds and wheel resolution do not establish native macOS execution.

## Runtime boundaries

Each VM has a persistent unprivileged supervisor and separate worker/mediator pair. QEMU belongs to the VM lifecycle. No production gateway path needs sudo, host capabilities, TAP or firewall changes. Privileged **test setup and observation** are separate and documented in the development guide.

The worker and QEMU resolve from explicit `SBX_GATEWAY_BIN` / `SBX_QEMU_BIN` before installation/PATH candidates. The mediator launcher uses the installed sbx interpreter, then resolves `mitmdump` through the initiating CLI's preserved PATH (or uses its packaged mitmweb wrapper). Other host environment values are not inherited implicitly. Provider environments are a separate, explicit inherited-descriptor boundary.

The final QEMU argv has one stream NIC, not SLIRP/TAP/bridge or a second fallback path. On Linux x86 without accessible KVM, automatic gateway-backed guests use q35/TCG `max`; explicit machine selections and unrelated backend paths remain preserved. Record CPU profile, filesystem and artifact identity in measurements, rather than attributing every timing difference to gateway code.

## Worker, DNS and capabilities

The controller passes an immutable digested policy, authenticated incarnation token, owned listeners, supervisor-lifetime descriptor and private append-only DNS journal via inherited FDs. Tests must pass `--dns-state-fd`; missing/empty initialized state is not recreated automatically.

Workers validate journal VM/generation/policy identity and restore every cached mapping before traffic. Mapping publication follows append/sync; exclusive file locking excludes overlapping workers. Capacity refuses new names instead of recycling guest-cached addresses. Explicit literal IPv4 registrations are separate, unique exact addresses and must match the controller's `direct_ip_routes` readiness count. Numeric DNS queries do not acquire synthetic identities.

Default-allow workers (`"default": "allow"`, encoded only for allow so deny digests are unchanged) additionally put the NIC in promiscuous/spoofing mode, silently drop UDP without a matching endpoint and report `egress_default` in readiness. Explicit rule lookup completes before the default is considered; matched-rule failures never fall through. Unmatched TCP is dialed only to addresses passing `config.DefaultAllowsIP`, never through the mediator. Unmatched DNS names receive real filtered IPv4 answers that are never registered or journaled. Landlock keeps filesystem confinement but does not restrict TCP connect ports in allow mode.

SOCKS capability version 2 binds logical DNS/IP destination, port, flow/rule, time/nonce, VM and incarnation. Destination travels in the username, signed compact metadata in the password; both fit RFC 1929's 255-byte limit even for the largest accepted DNS host/rule. Tokens are single-use, not host credentials or outbound request signatures.

QEMU deadlines bound incomplete frame transfers and blocked writes, not quiet time between frames. Tests use shortened deadlines rather than waiting ten minutes. Replacement probes the guest with ARP and still requires an authenticated genuine frame before network readiness.

## Confinement and bounded mediation

Production Linux workers require `CGO_ENABLED=0`. The all-thread syscall barrier applies no-new-privileges/Landlock to existing threads and future threads by inheritance; cgo-linked workers fail rather than claim process-wide confinement. Unavailable Landlock remains explicitly reported. Do not disable nondumpability for resource observation.

The mediator bounds glibc arena reservations before exec under its 512 MiB address-space limit. HTTP inactivity is 300 seconds, not a ten-second connect timeout. Its native async request hook must keep blocking broker/provider waits off the master loop and retain capacity reservations until outstanding work actually ends. Authorization, credential context, authority and headers are rechecked after awaits.

Numeric routing and logical authorization are separate immutable bindings. Keep mitmproxy's public routing setters/pool behavior, fresh DNS validation on new named connections, numeric IP identity for IP-SAN verification, matching wire authority and connected-peer validation. Never relabel an open connection, bypass certificate verification or silently replay an unknown request.

The [audit contract](../docs/rootless-network-gateway.md#host-audit-log-and-guest-denials) has one five-second lock/transport/durable-ACK budget, a 4 KiB ACK cap and sync-before-HTTP-forwarding. Outcomes remain asynchronous; logging/display windows are not an exactly-once guarantee. [Credential providers](../docs/network-credentials.md#process-and-trust-boundaries) keep their separate supervisor-owned cache and bounded helper lifecycle.

## Actual-VM stability validation

From the repository root, use the clean-installed wheel's interpreter and an existing TCG VM with the prepared Debian tools and non-root `agent` account:

```sh
"$INSTALLED_PYTHON" gateway/scripts/task19_stability.py \
  --vm "$VM_NAME" --origin "$SYNTHETIC_HTTPS_ORIGIN" \
  --git-commit "$FIXTURE_COMMIT" --gateway-root "$PRIVATE_GATEWAY_STATE" \
  --ingress-port "$HOST_PORT_FOR_GUEST_3000" --artifacts "$FRESH_PRIVATE_DIRECTORY"
```

The owned synthetic origin must implement `GUEST_WORKLOAD` endpoints and fake command/CodeArtifact credentials. The guest receives only `GWC_TOKEN` / `GWC_AWS` placeholders. Use populated Git, an installable `gwv-package==0.1`, incremental SSE and WebSocket echo, not real accounts. Public tunnels can buffer SSE; verify origin pacing independently. Keep normal TLS verification and isolate test CA trust.

The default smoke requires 600 real seconds, real mixed guest work, ingress and a replacement. Every pair gets the same 150-second representative warm-up and two-second sampling/quiet checkpoints. Gaps over ten seconds, missing observations, failed work or shortened runs fail. PID/start-time identities prevent replacement from hiding growth. Use the separate count-only observer from [development](../docs/development.md#linux-integration).

For a one-hour run without replacement, add `--duration 3600 --no-replacement`. The conditional 16 MiB RSS allowance is **one-time retained growth**, not recurring growth or a change to the 256 MiB idle target / 512 MiB ceiling. It requires two independent long-soak passes plus two replacement smokes. The final half-hour needs six five-minute RSS medians spanning at most 2 MiB with continuing mixed work; `--minimum-tail-iterations` freezes a calibration-derived work floor before holdouts.

Threads retain zero-growth checks unless `--thread-budgets` supplies ownership-derived caps with hash-verified evidence (`ceiling`, `evidence`, `sha256`). Observed maxima are not finite-pool proofs; long-run high-water must also stop increasing. These are validation controls, not user runtime options, a proven leak repair or automatic release clearance.

Keep failure/partial attempts, executed harness identity and origin counts. Before removing a fixture, stop writers, finish all bounded diagnostic readers and verify required archive members. Missing output is incomplete evidence, not a successful archive merely because tar exited zero. Never restart/replay failed admitted work just to recover logs.
