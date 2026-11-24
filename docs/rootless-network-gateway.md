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

See the [component diagram](rootless-network-gateway-components.mmd), [manual validation runbook](rootless-network-gateway-manual-validation.md), and [credential guide](network-credentials.md). The credential guide includes an inline request-flow diagram, a supported/unsupported authentication summary, and copyable GitHub/CodeArtifact examples showing how policy IDs, credentials, bindings, fetch timing, and guest headers fit together.

## Support and prerequisites

This guide describes the current development checkout. Gateway support is absent from the older `v0.2.5` release; use an installation containing the feature, as explained under [installation](../README.md#install). Start with a prepared image and working QEMU 7.2+ host setup from the [image guide](build-local-debian-pi-image.md).

| Area | Status / boundary |
| --- | --- |
| Linux amd64 | Functional gateway and isolated TCG tests have passed. This is not blanket release or long-duration acceptance. |
| macOS amd64 / arm64 | Workers cross-build; native runtime and weaker Darwin confinement remain unverified. |
| Dependency security | Tornado advisories remain unfixed. Four CI-reported IDs have a user-approved development-only exception; stable versions retain the full audit gate. See [exception scope and upgrade gates](dependency-upgrade.md#experimental-audit-exception). |
| Intel macOS installation | Last checked 2026-11-11: the locked secure Cryptography release lacked a wheel. Cross-building Go does not clear this gate. |
| Reliability / performance | Fresh complete final-artifact long-soak and reference-performance acceptance remain open. A functional smoke or bounded RSS snapshot is not full acceptance; a small stable thread increase is not itself proof of a leak. |
| Distribution / images | Licensing/notices decisions and manual booted-image Landlock verification remain separate gates. |

Only the documented development-only Tornado exception is approved; no other dependency, platform, distribution or performance gate is waived. This is not stable-release clearance. Runtime gateway processes need no sudo or network capabilities; image building and isolated integration-test setup have different prerequisites. [Contributor validation](development.md) documents those separately.

## Runtime provisioning

The package installation provides mitmproxy and the mediator dependencies from the root lock. Gateway startup uses the installed `sbx` interpreter; it does not invoke uv, download Python or packages, or create a per-VM virtual environment. CPython 3.12 is the supported baseline; newer versions are best effort. Mitmproxy is pinned to the numbered PyPI release 12.3.1. Known Tornado vulnerabilities still block stable release; the documented development-only exception accepts their risk without fixing them. This is not a fresh security clearance. See [dependency upgrades](dependency-upgrade.md).

Installed-file and dependency integrity are left to the package installer and OS. Gateway startup still probes required QEMU capabilities. No source build, compiler, runtime download, TAP/TUN device, sudo, capability, setuid helper, or host firewall rule is part of runtime setup. `SBX_GATEWAY_BIN` remains available for selecting a separately built worker. The mediator always uses the installed `sbx` interpreter and packaged launcher; `mitmdump` resolves through the initiating CLI's normal `PATH` ordering, which the persistent supervisor retains without inheriting any other environment values.

Guest CA installation also writes `/etc/profile.d/sbx-gateway-ca.sh`, defaulting
`NODE_EXTRA_CA_CERTS` to the public gateway certificate for Node/npm login-shell
workloads. An explicit existing value is preserved. This supplements Node's bundled
roots without disabling certificate verification or exporting the CA private key.

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

All supported policy modes use TCP. Overlapping host/port scopes are rejected even when their mode names differ: for example, HTTP mediation for `*.example.com:443` cannot coexist with raw TCP for `db.example.com:443`. Distinct ports or disjoint host scopes remain valid. Merely adding `[network]` still enables default-deny outbound networking; see [default-allow egress](#default-allow-egress) for the opt-in alternative.

## Network policies

Policy examples below are network fragments: merge them into your `.sbx.toml` rather than repeat existing tables. `[network]` enables default-deny networking unless `default = "allow"` is set; without it the normal backend uses SLIRP. All destinations use the same policy table with an explicit mode:

```toml
[[network.policy]]
id = "example-api"
mode = "http-mediated"
hosts = ["api.example.com"]
ports = [443]
methods = ["GET", "POST"]
path_prefixes = ["/v1/"]
```

- `http-mediated`: inspect HTTP/HTTPS; permits `methods`, `path_prefixes` and an ordered unique `hooks` list containing only approved built-in names. Empty methods or paths allow all within the destination policy.
- `tls-passthrough`: preserve end-to-end TLS for any TLS-wrapped application protocol, authorized through exact DNS SNI. No HTTP inspection or host credential injection.
- `tcp-forward`: forward opaque TCP without inspecting HTTP or TLS.

For example:

```toml
[[network.policy]]
id = "private-ldaps"
mode = "tls-passthrough"
hosts = ["ldap.example.com"]
ports = [636]

[[network.policy]]
id = "postgres"
mode = "tcp-forward"
hosts = ["db.example.com"]
ports = [5432]
```

`hosts` is always a list. One-label wildcards such as `*.example.com` match neither the apex nor `a.b.example.com`; only credential-free HTTP and raw TCP permit them. TLS passthrough and credential-bound policies require exact DNS hosts. Share a policy only when every host has identical authorization. Different permissions need separate policies. Expanded internal rules retain the public policy ID used by bindings and diagnostics.

`allow_ranges` is valid only for TLS passthrough and raw TCP. Each normalized CIDR explicitly permits an otherwise forbidden resolved address; it does not authorize additional hosts. Mediated HTTP rejects this option and remains public-unicast-only. Literal TCP additionally follows the hard exclusions below.

Methods normalize to uppercase. Paths repeatedly percent-decode within a fixed bound, reject invalid encoding, control characters, backslashes and dot segments, then match the whole prefix or a slash-delimited descendant. Credential bindings may narrow but never widen their parent policies; see [bindings](network-credentials.md#bindings).

## Default-allow egress

`default = "allow"` keeps every explicit policy exactly as written and forwards all other guest TCP (ports 1–65535) raw, without the mediator, credential retrieval, token injection or TLS interception. Explicit policies then mainly decide where sbx may inject host-held credentials. Complete configuration:

```toml
[network]
default = "allow"

[[network.policy]]
id = "service-api"
mode = "http-mediated"
hosts = ["api.example.com"]
ports = [443]
methods = ["GET", "POST"]
path_prefixes = ["/v1/"]
```

- A connection matching a policy host and port is enforced by that policy only. Denied methods/paths, SNI mismatches, missing mediation, authentication, DNS or upstream failures are rejected; they never retry token-free through the default route. `api.example.com:80` and every other destination are unmatched and use the default route.
- Unmatched traffic may reach public unicast addresses and IPv4 private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Loopback, link-local (including the AWS/GCP/Azure/OCI `169.254.169.254` metadata address), `0.0.0.0/8`, CGNAT `100.64.0.0/10` (including Alibaba `100.100.100.200`), Azure `168.63.129.16`, the guest/gateway subnet, the synthetic pool, documentation, reserved, broadcast and multicast space are blocked. IPv6 is limited to global `2000::/3` minus transition/documentation ranges; ULA (including AWS `fd00:ec2::254`) and NAT64 are blocked. Every literal and every resolved address is checked, and only the checked numeric address is dialed. Other provider-specific metadata services cannot be guaranteed.
- Names matched by a policy keep synthetic identities. Other names receive their real host-resolved IPv4 answers (up to 8, TTL 30), filtered by the same address rules: NXDOMAIN for missing names, an empty answer when only blocked addresses resolve, SERVFAIL for other resolver failures. These answers never consume synthetic mappings or the DNS journal.
- Explicit `allow_ranges` and direct-IP rules are unchanged and are not widened; default allow does not add exceptions to explicit policies.
- Changing `default` changes the configuration identity; stop/start to apply it. Old workers reject allow-mode configs, and readiness reports `egress_default`, so a mismatched worker fails closed.
- On Linux, allow mode keeps Landlock filesystem confinement but no longer restricts the worker's outbound TCP connect ports; the address rules above are the enforcement.

**Security boundaries:**

- **Token scoping is not service-policy enforcement.** The guest can reach a policy's service token-free through a literal IP, an alias, another port, or by resolving names itself over DNS-over-HTTPS, DNS-over-TLS or TCP/53. Hostname attribution is unavailable for unmatched names because the guest connects to real addresses.
- **Private access includes the host.** Services bound to the host's LAN or VPN addresses are reachable; host firewalls and service authentication still matter.
- **No payload filtering on the default route.** Host-held credentials are never fetched or injected there, and guest-supplied data is forwarded opaquely.
- **TCP only.** UDP other than gateway DNS, QUIC/HTTP/3 and guest IPv6 are not forwarded. ICMP echo may appear to succeed for arbitrary destinations; it does not indicate reachability.

Status counters `default_forwarded`, `default_denied` and `dns_upstream_answers` record default-route activity without payloads.

## Direct IPv4 policies

An IPv4 literal in `hosts` is the opt-in; there is no global bypass. Only listed addresses and ports become reachable. A DNS policy does not grant direct access to its resolved IPs, and `allow_ranges` does not grant access to neighboring addresses in its CIDR.

Private TCP needs an exact IP plus an explicit safety exception (network fragment):

```toml
[[network.policy]]
id = "database-ip"
mode = "tcp-forward"
hosts = ["10.20.30.40"]
ports = [5432]
allow_ranges = ["10.20.30.40/32"]
```

Raw TCP may carry TLS, but the guest application must verify its service certificate. The gateway does not inspect it, inject host credentials, or provide the durable HTTP-admission guarantee.

For HTTP inspection use credential-free mediation. Replace this illustrative address with the public IPv4 of a service you own (network fragment):

```toml
[[network.policy]]
id = "public-ip-api"
mode = "http-mediated"
hosts = ["93.184.216.34"]
ports = [443]
methods = ["GET", "HEAD"]
path_prefixes = ["/v1/"]
```

Mediated HTTP/HTTPS remains public-unicast-only, without private-range exceptions. For HTTPS, the trusted upstream certificate must contain the exact IP as an **IP-address SAN**, not a DNS SAN containing numeric text or only a matching Common Name. The existing per-VM CA supplies an IP SAN on the guest-facing leaf. Ordinary IP clients omit SNI; nonempty SNI on an IP admission is rejected, while DNS HTTPS still requires matching SNI. `Host`/HTTP2 authority must match the IP and effective port. Never disable verification or use a DNS Host override to select a virtual host through an IP policy.

```text
DNS policy: guest -> synthetic address -> DNS identity checks -> validated upstream IP
IP policy:  guest -> configured IPv4  -> exact IP/port checks -> that same upstream IP
```

Both mediated paths require durable admission before forwarding HTTP. Literal routes do not perform DNS or consume synthetic mappings; replacement restores exact registrations. They cannot select DNS-bound host credentials or widen redirects. IPv6 literals, numeric aliases (integer/hex/short IPv4), CIDR hosts and IP TLS passthrough are unsupported. Unconfigured IPs/ports are denied.

Direct destinations in `0.0.0.0/8`, loopback, link-local, the guest/gateway subnet, synthetic pool, multicast and reserved non-unicast space remain forbidden even with broad range exceptions. Upgrade and stop/start to activate matching worker artifacts; an old worker without the required direct-IP readiness count fails clearly. Native macOS remains unverified.

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

Set this in your existing `[network]` table (fragment; do not create a duplicate table):

```toml
[network]
mitmweb = true
```

The boolean defaults to `false` (`mitmdump`); `true` runs a hardened mitmweb wrapper using the installed package interpreter. Changing it requires stop/start, just like a policy change. No browser opens automatically. Each mediator owns a distinct OS-assigned `127.0.0.1` port and retains mitmweb authentication. The UI is stopped with the mediator, including on supervisor death; recovery creates a new port and authentication token.

After changing the setting, run on the host from your configured project:

```bash
sbx stop
sbx create
sbx network ui
```

`sbx network ui NAME` selects an explicit sandbox. Success prints exactly the current authenticated login URL; open it manually in your host browser. **The URL is a credential:** never share it, capture it in shared logs, copy it into project config, or expose/forward the UI to the guest or a public interface. Printing is an explicit disclosure, including when stdout is redirected. Rerun the command after recovery to obtain the new URL; a URL can become stale immediately after lookup.

The command requires a live ready supervisor, validates private UI metadata and refuses detected replacement during lookup. It never starts/restarts anything, changes policy, launches a browser or probes the HTTP endpoint. If the UI is disabled or its files are missing, check `mitmweb = true` and apply stop/start; if already enabled, use `sbx network diagnostics NAME` for troubleshooting. A recovering gateway must become ready before lookup succeeds. Operational failures leave stdout empty and exit 1; a missing name or invalid configuration exits 2.

`web-auth.json` remains an internal owner-only authentication artifact, not the normal user interface. Status/diagnostics may show the non-secret address and authentication-file path, but never read or print its token. The URL is not passed to another process.

The UI shows completed **metadata snapshots** with full request URLs and targets, including query strings, plus the separate canonical policy path, method, destination, status and outcome. It retains at most 256 whole events within an 8 MiB serialized-event budget, evicting oldest whole snapshots rather than trimming fields; the persistent audit log is independent of that display window. It never retains live proxy flows, request/response headers, bodies, WebSocket messages, provider values, or guest placeholders. Flow export contains the same sanitized snapshots. Editing, importing, replay, arbitrary commands and option mutations return HTTP 403; this is an observer, not an interactive proxy debugger. TLS passthrough and traffic denied before HTTP mediation do not produce HTTP snapshots.

## Host audit log and guest denials

The supervisor owns one `audit.log` JSON Lines file per VM generation, under the private persistent state directory. `sbx network diagnostics NAME` prints its host-only path. It is mode `0600` below `0700` directories and remains the same across worker/mediator replacement. Normal stop and stop-on-exit preserve it with the generation's diagnostics, including across the next start. Runtime sockets, CA keys and UI authentication files are removed on stop. Explicit VM removal deletes the VM's retained generations under its ownership lock; a failed startup before mediation may discard its unused staging state.

```bash
# Substitute the audit path printed by host diagnostics.
tail -f /path/from/diagnostics/audit.log
```

Before forwarding an allowed mediated HTTP request's headers or body, the mediator emits an `admitted` record and waits for the supervisor to confirm durable storage. The supervisor appends the complete record, flushes and syncs the file, and persists new directory entries before acknowledgement; Darwin additionally requires `F_FULLFSYNC`. Receipt or a buffered write is insufficient. Admission failure or a five-second end-to-end timeout prevents forwarding and stops mediation. DNS/connection establishment, non-HTTP traffic and TLS passthrough are outside this HTTP guarantee.

Records use schema `version: 2`, `event: admitted|outcome` and `request_id`, scoped by generation/incarnation. Admission records have `outcome: admitted` and status 0. Each completed mediated request produces a terminal `outcome` record with the same request ID (denied requests have no admission). Records include UTC `timestamp`, `generation`, `incarnation`, original `url`, full `request_target`, original `path`, separate `canonical_path`, original `method`, `host`, `scheme`, `authority`, `port`, admitted public `policy` (or null), HTTP `status` (0 on transport failure), `outcome`, and stable internal `reason`. Original request details are captured before hooks and credential injection. URL, path, hostname and method fields are **not truncated or normalized away**: malformed/encoded targets and query strings remain evidence even when policy canonicalization rejects them. Any fragment actually sent in a request target is retained; ordinary clients do not transmit URL fragments. Headers, bodies, provider output and raw exceptions are not recorded. Known placeholders and cached host-provider values remain redacted. JSON encoding escapes control characters without losing them or allowing log-line injection.

**Sensitive evidence:** guest-supplied tokens, personal information and other secrets in URLs/query strings are intentionally visible in these private logs and the authenticated UI. Do not publish logs or flow exports without reviewing them.

Audit records use a dedicated serialized pipe writer and routing thread, with a private per-incarnation reverse acknowledgement pipe capped at 4 KiB; admission transactions are serialized without batching. Both admission and outcome records consume the log byte/rate budgets. UI snapshots remain terminal-outcome-only. Audit records larger than a pipe buffer or ordinary 64 KiB diagnostic event are transferred in full, not fragmented into truncated log lines. Named safety limits remain: 1 MiB per serialized audit event, a five-second end-to-end admission deadline (lock, transport and durable acknowledgement), the 100 MiB aggregate per-generation log budget, and 1,000 events/second (2,000-event burst). These limits do **not** slice URL fields or silently drop records. Delivery/record-size, rate, storage or sink failures stop mediation and surface `controller/audit_failed` through host diagnostics. Partial records or malformed audit transport also trigger failure. Inspect the error, preserve evidence and stop/start after fixing the cause; automatic recovery must not hide an audit failure.

The five-second budget is an availability policy approved for early adoption, not a relaxation of sync-before-forwarding. A captured 2.63-second sync-stage pause exceeded the former one-second budget; the same pause can now complete without shutting down the gateway. Healthy requests do not acquire a five-second delay. A slow audit transaction can stall other mediated traffic until it completes; exceeding five seconds or an actual storage/transport error still stops mediation. This does not make failing storage reliable.

For early-adopter reports, include the sbx version/wheel hash, OS, state-filesystem type, workload/protocol and concurrency, and the redacted output of `sbx network diagnostics NAME`. Include operation/reason/errno/stage timings when present, and whether unrelated requests also stalled. Do not attach raw audit logs, state directories, credentials or CA keys without private review. This is manual reporting, not automatic telemetry.

Admission without an outcome means **execution unknown**, not success, failure or permission to replay: a crash may occur before forwarding or after the destination executes the request. A lost acknowledgement can also leave an admission that was never forwarded. Do not automatically replay. Terminal outcomes remain asynchronous and may be unavailable after delivery/disk failure or crash; those failures stop mediation. Durable admission relies on the host storage honoring sync semantics, not exactly-once execution or guaranteed final-outcome persistence. Later body validation can still reject an admitted request.

Guest-facing policy/credential denials now use HTTP 403 with a fixed empty body. Capacity, malformed/header-size and provider/upstream failures retain their distinct status codes, also with empty bodies; internal reason codes are host-only.

## SmolVM migration condition

`sbx` keeps its typed external-network facade until a released SmolVM version exposes the same QEMU Unix-stream backend API. Migrate only after the upstream release preserves the single-NIC/no-SLIRP invariant and the existing QEMU argv tests pass unchanged.

## Security notes

- The guest's effective DNS resolver must be the gateway at `192.0.2.1`. UDP DNS sent elsewhere has no forwarding path; TCP port 53 is denied unless an explicit TCP policy allows it. DNS-over-HTTPS and DNS-over-TLS are ordinary HTTPS/TLS traffic, so do not allow their providers unless intended. Addresses learned through another resolver do not grant egress by themselves.
- In default-deny mode, unknown destinations, unconfigured direct IP egress, alternate DNS, UDP/443/QUIC, identity mismatches, crashes, and unsupported protocols fail closed. In [default-allow mode](#default-allow-egress), unmatched TCP to permitted addresses is forwarded raw, including alternate DNS over TCP and DNS-over-HTTPS/TLS; UDP/QUIC, identity mismatches, crashes and explicit-policy failures still fail closed.
- Raw TCP/TLS forwarding denies non-public/special-use addresses by default, including `100.64.0.0/10` and Alibaba metadata `100.100.100.200`, reserved/documentation ranges and IPv6 transition/special-use space. Explicit `allow_ranges` exceptions remain available for those forwarding modes; IPv4-mapped addresses are checked as IPv4.
- DNS-named mediated HTTP resolves the admitted hostname once per new upstream connection, validates every answer as public unicast (including IPv4-mapped addresses), and pins a validated numeric destination. Loopback/private/link-local/metadata, reserved/non-public and multicast answers are denied, including mixed public/private results. TLS identity remains the logical hostname. HTTP mode does not allow private-range exceptions; local protocol tests use a test-only launcher, not a production option.
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
2. Verify the root lock resolves wheel-only for supported Linux and Darwin targets. Apply the [documented audit policy](dependency-upgrade.md#experimental-audit-exception); stable candidates must pass `uv audit --locked` without exceptions.
3. Run the fast, mediator, Go/race and explicit-TCG integration checks in [development](development.md), then the [operator smoke](rootless-network-gateway-manual-validation.md). Record long-duration/performance and native-platform results separately; missing required results remain open.
4. Run help/completion/doc example parse checks, tests, ruff, Go tests, audits, and `git diff --check`.
5. Record macOS as unverified unless it was actually run.

## Rollback checklist

1. Stop affected VMs with their existing configuration: `sbx stop NAME`. Do not restart failed admitted transactions merely to collect evidence.
2. Preserve the private audit, DNS journal, state, diagnostic logs and needed guest-only data before any removal/reset or downgrade. Include runtime evidence before stop where safely obtainable; stop removes ephemeral CA/auth files and live endpoints. If export fails, retain the stopped VM and disk.
3. Verify the archive contents and hashes. An `admitted` request without an outcome remains unknown; never replay it automatically.
4. Only after preservation, explicitly reset/remove owned state when required by the selected version's format. Do not delete a directory just because its metadata is corrupt or unsafe. VM removal deletes retained evidence and guest-only data.
5. Reinstall the selected previous package/tag and review its configuration compatibility before restarting. Disabling `[network]` switches back to unrestricted-by-this-gateway SLIRP; that is an explicit security-policy change, never an automatic recovery step.

Audit URLs/query strings can contain secrets even though known provider values and placeholders are redacted. Keep archives private and never publish raw state, logs, CA keys or UI login URLs without review.
