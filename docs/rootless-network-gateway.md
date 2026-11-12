# Rootless network gateway

The gateway is the non-SLIRP network path for mediated sandboxes:

```text
per-VM supervisor -> sbx-gateway worker -> QEMU virtio-net
                  -> mitmproxy mediator
```

The unprivileged supervisor persists after the initiating CLI exits. It owns bounded logs, live
counters, authenticated pair replacement, drain/shutdown control, and the worker/mediator lifetime
channel; QEMU remains owned by the VM lifecycle.

Linux amd64 ships with a prebuilt worker in the Python package. Darwin amd64/arm64 workers are built for release artifacts, but macOS runtime is **unverified** and has weaker confinement than Linux. End users do not need Go.

See the [component diagram](rootless-network-gateway-components.mmd), [manual validation runbook](rootless-network-gateway-manual-validation.md), and implemented [network credential model](network-credentials.md).

## Runtime provisioning

The package installation provides mitmproxy and the mediator dependencies from the root lock. Gateway startup uses the installed `sbx` interpreter; it does not invoke uv, download Python or packages, or create a per-VM virtual environment. CPython 3.12 is the supported baseline; newer versions are best effort. Mitmproxy is pinned to the numbered PyPI release 12.3.1. The dependency audit currently blocks release on Tornado vulnerabilities; see [dependency upgrades](dependency-upgrade.md).

Installed-file and dependency integrity are left to the package installer and OS. Gateway startup still probes required QEMU capabilities. No source build, compiler, runtime download, TAP/TUN device, sudo, capability, setuid helper, or host firewall rule is part of runtime setup. `SBX_GATEWAY_BIN` remains available for selecting a separately built worker. The mediator always uses the installed `sbx` interpreter and packaged launcher; `mitmdump` resolves through the initiating CLI's normal `PATH` ordering, which the persistent supervisor retains without inheriting any other environment values.

## Runtime layout

Policy, logs, CA material, lifecycle locks, and non-secret metadata remain under the user sbx state directory. Live endpoints use owner-only runtime paths independent of the VM name and persistent state depth:

```text
$XDG_RUNTIME_DIR/sbx/<generation>/qemu-net.sock
$XDG_RUNTIME_DIR/sbx/<generation>/control.sock
```

When `XDG_RUNTIME_DIR` is unavailable, unsafe, or too long, sbx uses a short owner-only temporary runtime root and records it in the persistent generation metadata. The authenticated generation remains the full UUIDv4 value; only its runtime-directory encoding may be compacted to meet the portable 103-byte socket-path limit.

## Current generation and retained state

Each VM has an owner-only `current.json` beside its generation directories under the persistent `gateway/<vm>/` state directory. It contains exactly `version: 1`, `vm_id` and `generation`; a null generation explicitly means no current gateway. Lifecycle commands use this reference, never creation timestamps, so equal timestamps or a backward host-clock adjustment cannot select an old stopped generation.

Reference changes are atomic and synced under the ownership lock. Startup publishes a complete generation before starting its data plane; normal stop retains its reference and evidence. Unused startup rollback clears the reference before deleting staging state, without selecting older history. Explicit removal also handles retained generations when the reference is null. Missing, corrupt, unsafe or dangling references fail closed. This unreleased format has no legacy migration: stop pointerless gateways with the prior code and explicitly archive/reset local state before using this version; no audit evidence is discarded automatically.

## Configuration example

```toml
[network]
default = "deny"

[[network.policy]]
id = "pypi"
mode = "http-mediated"
hosts = ["pypi.org"]
ports = [443]
methods = ["GET", "HEAD"]

[[network.policy]]
id = "pinned"
mode = "tls-passthrough"
hosts = ["pinned.example.com"]
ports = [443]

[[network.ingress]]
listen = "127.0.0.1:0"
guest_port = 3000
```

Old `[sbx].port_forwards` entries remain accepted and are normalized into ingress rules.

All supported policy modes use TCP. Overlapping host/port scopes are rejected even when their mode names differ: for example, HTTP mediation for `*.example.com:443` cannot coexist with raw TCP for `db.example.com:443`. Distinct ports or disjoint host scopes remain valid. Merely adding `[network]` still enables default-deny outbound networking.

## Admission and retained state

The mediator atomically reserves one of its 128 request slots before hooks, credential retrieval or durable audit acknowledgement. Excess requests receive HTTP 503 without entering that work. Rejections, failures and completed requests release their reservations, including when terminal audit delivery fails. Unexpected admission errors kill the affected flow and propagate rather than allow forwarding to continue.

Completed WebSocket message history is discarded while the relay retains the current message long enough to forward it. Existing destination checks and the 16 MiB per-message limit remain enforced; a long-lived connection does not accumulate every previously relayed message.

Before replacement readiness, queued ingress connections are accepted and closed using non-blocking socket operations. Only an empty kernel backlog completes the drain; unexpected errors or exhaustion of 4,096 drain attempts per listener abort readiness rather than allow stale connections through. New connections remain usable after the drain.

## Synthetic DNS identities and recovery

Synthetic DNS answers advertise a 30-second TTL, but an issued address remains bound to its hostname for the entire VM generation. At the configured mapping limit, new names receive DNS REFUSED; existing mappings keep working. Addresses are not recycled merely because their connections close or their TTL expires.

The supervisor opens an owner-only `dns-mappings.jsonl` in the generation directory and passes a dedicated append-capable descriptor to each worker. The 1 MiB journal is bound to the VM, generation and immutable worker-policy digest. Each mapping is appended and synced before any DNS answer exposes it. A replacement restores the mappings and registers all cached wildcard addresses before accepting guest frames; new allocations skip restored addresses. An exclusive journal lock prevents overlapping workers from allocating independently.

Missing/empty initialized state is never recreated automatically. Malformed, truncated, cross-generation or non-private state fails startup; persistence I/O failure produces DNS SERVFAIL and terminates the worker rather than silently falling back to an empty resolver. Journal capacity exhaustion refuses new mappings without rebinding existing ones. Do not edit the journal while the generation is running. It contains guest-supplied hostnames and must remain private. Stop preserves it; explicit VM removal deletes it with retained generation state.

A quiet QEMU stream has no between-frame idle deadline. The existing 600-second transfer deadline starts only after the first frame byte, covers the remaining header/payload, and still bounds blocked writes. Authenticated first-frame readiness and configured recovery deadlines are unchanged.

## Optional read-only mitmweb UI

```toml
[network]
mitmweb = true
```

The boolean defaults to `false` (`mitmdump`); `true` runs a hardened mitmweb wrapper using the installed package interpreter. Changing it requires stop/start, just like a policy change. No browser opens automatically. Each mediator owns a distinct OS-assigned `127.0.0.1` port and retains mitmweb authentication. The UI is stopped with the mediator, including on supervisor death; recovery creates a new port and authentication token.

Use `sbx network diagnostics NAME` for the address and private `web-auth.json` path. Read that owner-only file on the host to obtain the login URL, then open it manually. Never share that URL, copy it into project config, or expose/forward the UI to the guest or a public interface. Diagnostics and process arguments never print its token. Read the file again after recovery.

The UI shows completed **metadata snapshots** with full request URLs and targets, including query strings, plus the separate canonical policy path, method, destination, status and outcome. It retains at most 256 whole events within an 8 MiB serialized-event budget, evicting oldest whole snapshots rather than trimming fields; the persistent audit log is independent of that display window. It never retains live proxy flows, request/response headers, bodies, WebSocket messages, provider values, or guest placeholders. Flow export contains the same sanitized snapshots. Editing, importing, replay, arbitrary commands and option mutations return HTTP 403; this is an observer, not an interactive proxy debugger. TLS passthrough and traffic denied before HTTP mediation do not produce HTTP snapshots.

## Host audit log and guest denials

The supervisor owns one `audit.log` JSON Lines file per VM generation, under the private persistent state directory. `sbx network diagnostics NAME` prints its host-only path. It is mode `0600` below `0700` directories and remains the same across worker/mediator replacement. Normal stop and stop-on-exit preserve it with the generation's diagnostics, including across the next start. Runtime sockets, CA keys and UI authentication files are removed on stop. Explicit VM removal deletes the VM's retained generations under its ownership lock; a failed startup before mediation may discard its unused staging state.

```bash
# Substitute the audit path printed by host diagnostics.
tail -f /path/from/diagnostics/audit.log
```

Before forwarding an allowed mediated HTTP request's headers or body, the mediator emits an `admitted` record and waits for the supervisor to confirm durable storage. The supervisor appends the complete record, flushes and syncs the file, and persists new directory entries before acknowledgement; Darwin additionally requires `F_FULLFSYNC`. Receipt or a buffered write is insufficient. Admission failure or a one-second end-to-end timeout prevents forwarding and stops mediation. DNS/connection establishment, non-HTTP traffic and TLS passthrough are outside this HTTP guarantee.

Records use schema `version: 2`, `event: admitted|outcome` and `request_id`, scoped by generation/incarnation. Admission records have `outcome: admitted` and status 0. Each completed mediated request produces a terminal `outcome` record with the same request ID (denied requests have no admission). Records include UTC `timestamp`, `generation`, `incarnation`, original `url`, full `request_target`, original `path`, separate `canonical_path`, original `method`, `host`, `scheme`, `authority`, `port`, admitted public `policy` (or null), HTTP `status` (0 on transport failure), `outcome`, and stable internal `reason`. Original request details are captured before hooks and credential injection. URL, path, hostname and method fields are **not truncated or normalized away**: malformed/encoded targets and query strings remain evidence even when policy canonicalization rejects them. Any fragment actually sent in a request target is retained; ordinary clients do not transmit URL fragments. Headers, bodies, provider output and raw exceptions are not recorded. Known placeholders and cached host-provider values remain redacted. JSON encoding escapes control characters without losing them or allowing log-line injection.

**Sensitive evidence:** guest-supplied tokens, personal information and other secrets in URLs/query strings are intentionally visible in these private logs and the authenticated UI. Do not publish logs or flow exports without reviewing them.

Audit records use a dedicated serialized pipe writer and routing thread, with a private per-incarnation reverse acknowledgement pipe capped at 4 KiB; admission transactions are serialized without batching. Both admission and outcome records consume the log byte/rate budgets. UI snapshots remain terminal-outcome-only. Audit records larger than a pipe buffer or ordinary 64 KiB diagnostic event are transferred in full, not fragmented into truncated log lines. Named safety limits remain: 1 MiB per serialized audit event, a one-second transport/lock deadline, the 100 MiB aggregate per-generation log budget, and 1,000 events/second (2,000-event burst). These limits do **not** slice URL fields or silently drop records. Delivery/record-size, rate, storage or sink failures stop mediation and surface `controller/audit_failed` through host diagnostics. Partial records or malformed audit transport also trigger failure. Inspect the error, preserve evidence and stop/start after fixing the cause; automatic recovery must not hide an audit failure.

Admission without an outcome means **execution unknown**, not success, failure or permission to replay: a crash may occur before forwarding or after the destination executes the request. A lost acknowledgement can also leave an admission that was never forwarded. Do not automatically replay. Terminal outcomes remain asynchronous and may be unavailable after delivery/disk failure or crash; those failures stop mediation. Durable admission relies on the host storage honoring sync semantics, not exactly-once execution or guaranteed final-outcome persistence. Later body validation can still reject an admitted request.

Guest-facing policy/credential denials now use HTTP 403 with a fixed empty body. Capacity, malformed/header-size and provider/upstream failures retain their distinct status codes, also with empty bodies; internal reason codes are host-only.

## Task 19 status

Passed: the bounded ten-minute mediator stability smoke completed with bounded RSS, FDs, processes, and threads. This smoke does not launch QEMU; TCG coverage comes from the separate single- and multi-VM harnesses.

Deferred, not passed: readiness p95, absolute throughput, sustained 128-active HTTP request performance, and repeat four-VM TCG performance. Current preference for the follow-up is a warm launcher/forkserver with separate per-VM children; do **not** collapse VMs into one shared mitmproxy process without a new design decision.

## SmolVM migration condition

`sbx` keeps its typed external-network facade until a released SmolVM version exposes the same QEMU Unix-stream backend API. Migrate only after the upstream release preserves the single-NIC/no-SLIRP invariant and the existing QEMU argv tests pass unchanged.

## Security notes

- The guest's effective DNS resolver must be the gateway at `192.0.2.1`. UDP DNS sent elsewhere has no forwarding path; TCP port 53 is denied unless an explicit TCP policy allows it. DNS-over-HTTPS and DNS-over-TLS are ordinary HTTPS/TLS traffic, so do not allow their providers unless intended. Addresses learned through another resolver do not grant egress by themselves.
- Unknown destinations, direct IP egress, alternate DNS, UDP/443/QUIC, identity mismatches, crashes, and unsupported protocols fail closed.
- Raw TCP/TLS forwarding denies non-public/special-use addresses by default, including `100.64.0.0/10` and Alibaba metadata `100.100.100.200`, reserved/documentation ranges and IPv6 transition/special-use space. Explicit `allow_ranges` exceptions remain available for those forwarding modes; IPv4-mapped addresses are checked as IPv4.
- Mediated HTTP resolves the admitted hostname once per new upstream connection, validates every answer as public unicast (including IPv4-mapped addresses), and pins a validated numeric destination. Loopback/private/link-local/metadata, reserved/non-public and multicast answers are denied, including mixed public/private results. TLS identity remains the logical hostname. HTTP mode does not allow private-range exceptions; local protocol tests use a test-only launcher, not a production option.
- Secrets stay host-side; the guest receives placeholders only, scoped by destination and approved fields.
- Linux uses rootless hardening where available. macOS is portable by design but unverified and less confined.
- Same-UID host processes remain a residual risk; use a separate OS account if that matters.

## Troubleshooting

- `mediator failed`: reinstall `sbx` with uv if its locked mediator dependencies are missing.
- `QEMU stream unsupported`: use QEMU 7.2+ and explicit TCG/KVM as appropriate.
- `gateway mode cannot fall back`: fix the gateway error; SLIRP is intentionally not used as a backup.
- `dead supervisor with orphaned children` or `stale QEMU`: stop the VM before gateway fallback cleanup; Linux verifies orphan identity through pidfds, while unverified macOS may require manual cleanup.
- `configuration: drift`: the running policy is unchanged; use `sbx stop NAME` and restart it to apply the requested configuration.

## Release checklist

1. Build Linux amd64 and Darwin amd64/arm64 workers.
2. Verify the root lock resolves wheel-only for supported Linux and Darwin targets and passes `uv audit`.
3. Run Section 22 functional/security acceptance under explicit TCG.
4. Run help/completion/doc example parse checks, tests, ruff, Go tests, audits, and `git diff --check`.
5. Record macOS as unverified unless it was actually run.

## Rollback checklist

1. Disable `[network]` gateway mode in affected configs.
2. Stop affected VMs: `sbx stop NAME`.
3. Remove stale gateway state under the user sbx state directory if diagnostics report it unsafe.
4. Reinstall the previous `sbx` package/tag.
5. Preserve `audit.log` and bounded mediator/worker logs for investigation; they should be secret-redacted. Treat `web-auth.json` as an authentication secret, not a shareable diagnostic.
