# Direct IPv4 gateway validation — 2026-11-19

## Delivered behavior

Explicit IPv4 literals in existing `hosts` lists now opt into raw TCP or credential-free mediated HTTP/HTTPS. No global bypass, new policy format, dependency, daemon, or runtime privilege was added. Hostname rules do not grant direct access to their resolved addresses.

- Python/Go validation shares address-policy vectors. Private TCP requires an explicit range exception; that exception does not authorize other addresses. Local/internal/synthetic/non-unicast direct destinations remain forbidden even with a broad exception. Mediated HTTP remains public-unicast-only.
- Each worker registers unique literal destinations before readiness; replacements reconstruct them without DNS allocations or journal mutation. `direct_ip_routes` readiness must match the expected unique count; stale workers fail clearly for literal policies.
- Literal TCP/HTTP connections bypass DNS resolution, not policy checks. Numeric DNS queries cannot allocate or restore synthetic identities.
- IP HTTPS permits absent SNI only for the admitted literal and retains exact authority, peer, capability and reuse checks. Pinned mitmproxy's existing IP verification produces/verifies real IP SANs and omits upstream wire SNI; no verification bypass or vendor TLS patch was used.
- Host credential bindings remain DNS-only, including every expanded host in mixed policies. IP-based TLS passthrough and IPv6 literals are rejected. Existing five-second durable HTTP admission, bounded transport, unknown outcomes and no-replay guarantees remain unchanged; raw TCP is outside the HTTP-admission guarantee.
- An independently observed startup race is also classified safely: an exited mediator closing the policy pipe now raises a gateway lifecycle error instead of leaking `BrokenPipeError`. It still fails closed.

Examples, limits and guest behavior are documented in [the credential/network guide](network-credentials.md#direct-ipv4-policies), with links from README, the main gateway guide and operator runbook.

## Candidate and provenance

Implementation baseline: `7fabdda133aaae713ee691a725e9d58401368641`; this report concerns the subsequent uncommitted direct-IP changes, not that baseline commit alone.

- Wheel: `sbx-0.2.6.dev0-py3-none-any.whl`.
- SHA-256: **`1d09eafcadd6f5283ed188c9c997ae9a6572d0bd89eff39245bea3eb4cdac88a`**.
- Current locked dependencies retained; no lock or Go module dependency changes.
- Linux amd64, Darwin amd64 and Darwin arm64 workers rebuilt using the pinned Go toolchain. All four mediator mirrors match. Wheel/source/clean-installed package files and worker executable modes were compared; exact file hashes are in `artifact-identity.json`.
- Fresh native-local installation used locked binary wheels, including mitmproxy 12.3.1, rather than importing the editable source package. `sbx --help` and `sbx network --help` succeeded there.

Evidence root: `local/direct-ip-validation/` in the implementation worktree. Results are local artifacts, not automatic telemetry or an external release.

## Results

| Check | Result |
| --- | --- |
| Baseline fast suite | PASS before implementation. |
| Before-change regressions | Expected Python/Go failures retained: invalid literal configurations accepted, direct addresses not registered, literal mediation taking DNS/no-SNI paths. |
| Final fast Python suite | **1,296 passed, 1 skipped; 92.22% coverage**, above the unchanged 90% gate. |
| Mediator suite | **193 passed + 8 subtests**. Includes real IP TLS for HTTP/1.1 and HTTP/2, wrong IP/DNS-typed numeric SAN/CN-only/expired/untrusted certificate negatives, no upstream wire SNI, durable admission gating, chunked streaming, WebSocket echo, redirects and authority reuse rejection. |
| Go ordinary / race suites | **All seven packages PASS**, including packet-level literal admission, shared policy vectors, numeric DNS refusal, restored state and authenticated SOCKS behavior. |
| Targeted fuzzing | Four bounded runs PASS: config parsing, ClientHello SNI, SOCKS replies and QEMU frame parsing; fuzz seed regressions also run with the ordinary suites. |
| Existing Linux integration suite | **8 passed**, with explicit four-VM TCG isolation and count-only resource observation. |
| New source-runtime direct-IP VM test | **PASS**: controlled origins, production address checks and real QEMU. |
| Clean-installed wheel direct-IP VM test | **PASS**, including the final fixture with isolated host HOME. |
| Ruff / formatting / diff checks | PASS. |

The fast-suite skip is the existing manual built-image Landlock gate, not direct-IP acceptance. It is not counted as a successful image boot check. Native Darwin execution was not performed.

### Actual-VM coverage

Only owned disposable **network namespaces** received the globally classified/public and private fixture addresses. No illustrative public IP was assigned to a physical/shared-host interface; no third-party service was contacted for positive acceptance. Fixture setup dropped UID/groups/capabilities before any gateway or QEMU process started. Worker/mediator status confirmed zero effective capabilities and `NoNewPrivs`.

The guest exercised public and explicitly permitted private TCP, plain HTTP and correctly verified IP HTTPS. Wrong certificate, forged DNS Host, a known DNS credential placeholder, disallowed method/path, wrong port, unlisted public address and unlisted neighbor inside an allowed private CIDR did not obtain authorized HTTP execution/access. Origin-side records confirm only the expected requests. The workload disabled its ordinary DNS resolver after an initial controlled lookup; IP operations continued working. A direct gateway DNS query separately checked that the old synthetic mapping survived replacement.

Both worker and mediator were replaced while QEMU stayed alive. The next pair reported two literal routes, successful IP operations resumed, and the DNS journal remained byte-identical. Existing four-VM integration separately retained cross-VM isolation coverage. This is not a claim of native macOS or long-duration reliability acceptance.

## One-time configuration cost, not a soak

The worker-policy JSON limit remains 1 MiB. For the measured generated rule shape:

| Registered addresses | Serialized worker policy | Pair RSS snapshot | Pair FDs |
| --- | --- | --- | --- |
| 2 | Small representative policy | 82,350,080 bytes | 19 |
| 8,237 | 1,048,559 bytes | 93,286,400 bytes | 19 |

The next rule produced 1,048,686 bytes and was rejected instead of launching an oversized worker policy. This is the maximum for this worker-policy encoding, **not** a promise that the persistent supervisor's separate bootstrap metadata encoding supports the same number of rules. Identity-checked root-owned count-only observation was used. These short startup snapshots stay under the 256 MiB idle target and 512 MiB pair ceiling, but do not establish an hour-long RSS/thread plateau or repair/relabel historical resource failures. No GC/allocator trimming or thread preallocation was added.

## Archival and cleanup

Guest console/network-state evidence, regular runtime/audit/DNS files and stopped disposable guest overlays were archived and every regular member verified before removal. Startup failures and superseded fixture runs remain distinguishable in five copied evidence sets. A test-image archive contains the backing rootfs, kernel and manifest so the retained overlays do not depend solely on a disposable `/tmp` base image.

- Clean native installation: **4,356 regular files**, archive SHA-256 **`987d831025488d0821535647b9446514994179d06ba201be15e59a249d4df4df`**. Original installation prefix removed only after member verification.
- Test-image archive SHA-256: **`1a6f115b158dd3166ce892c3fa091a20c305d6b66c750e1086509a98e798062b`**. Original shared test image retained.
- Owned namespace fixtures, QEMU/gateway processes, temporary observers and the retained pre-guest failed staging directory were cleaned up. Historical `/tmp/sbx-gwb/images/sbx` was not touched.

The initial wheel command encountered the pre-existing `.venv` symlink loop; explicit interpreter/environment selection resolved it without changing `.venv`. An installation-command flag conflict and early test-fixture socket-mode/trust setup failures are retained separately; they are not hidden production passes.

## Limits and release posture

This completes the scoped Linux implementation/functional validation, not external publication or whole-project release clearance. Native macOS, a fresh hour-long resource/performance campaign, dependency-advisory re-triage, licensing/distribution decisions and the manual image-Landlock gate remain outside these results. No dependency/security exception was granted, no authentication/signing feature was added, and no commit, push, merge or release was performed.

Evidence archives contain private state/test CA keys and must not be posted as adopter reports. Share only redacted diagnostics and artifact identities after review.
