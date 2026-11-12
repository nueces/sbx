# Rootless network gateway — implementation status

**DRAFT — not release-ready. Updated 2026-11-12.**

Worktree: `/home/nueces/code/sbx/feature/rootless-network-gateway/`

Branch: `feature/rootless-network-gateway`

Existing work was preserved. This records local implementation and validation, not release approval.

## Current status summary

Core gateway, secure opt-in mitmweb integration and durable HTTP admission logging are implemented. Release remains blocked by confirmed Tornado vulnerabilities, Darwin amd64 wheel-only dependencies, unavailable final TCG acceptance and unverified macOS runtime. Custom UI presentation changes are out of scope.

## Implemented

| Area | Current status |
|---|---|
| Dependencies | PyPI mitmproxy 12.3.1 and root lock. Direct Tornado version pin removed; mitmproxy still constrains resolution to 6.5.10. |
| Configuration | Strict `[network].mitmweb` boolean, default `false`; digest and supervisor transport; stop/start applies changes. |
| Secure UI integration | Authenticated OS-assigned host-loopback listener, no browser launch, read-only wrapper, mediator-owned lifecycle. |
| Credential protection | Metadata snapshots rather than live flows; no headers/bodies; mutation/import/replay and option changes blocked; placeholders and injected host credentials redacted. |
| Evidence | Full original URL/target/path/query/method/host/authority captured before hooks/substitution; canonical policy paths separate; no field truncation. |
| Durable admission | After final header authorization/preparation, append version-2 `admitted` record and await a matching durable-storage acknowledgement before forwarding HTTP headers/body. |
| Persistence | Supervisor flushes and syncs the audit file; first admission also syncs directory ancestors. Darwin requests `F_FULLFSYNC`; native execution remains unverified. |
| Correlation | `request_id`, generation and incarnation link admission, acknowledgement and terminal `outcome`. Denied requests have terminal outcomes only. |
| Transport/lifecycle | Existing serialized writer plus private per-incarnation reverse acknowledgement pipe; one outstanding admission; bounded frames/deadline; descriptors inherited and cleaned up with the mediator. |
| Failures | Admission delivery/storage/acknowledgement failure or timeout kills the affected flow and stops mediation. Supervisor failures surface `controller/audit_failed`. No fallback or replay. |

## Review findings — fixed

| Priority | Finding | Fix and regression evidence |
|---|---|---|
| P1 | Mediated DNS bypassed destination-address checks | Validate every resolved IPv4/IPv6 address; reject non-public, multicast and scoped addresses (including IPv4-mapped private addresses). Pin a vetted numeric target before connect while retaining TLS SNI; no hostname re-resolution by the connector. Reject mixed public/private answers and DNS failures. Production-launcher loopback denial, rebinding, malformed/failed resolution and existing protocol tests cover the boundary. Local fixture loopback access is test-only and never enabled by production launchers. |
| P1 | Normal shutdown deleted audit evidence | Normal close, authenticated shutdown and orphan fallback stop preserve generation logs/state; remove CA/auth files and runtime endpoints. Failed-start rollback also retains nonempty audit evidence in both supervisor and parent cleanup; only unused staging state can be deleted automatically. Restart retains previous audit generations. Explicit VM removal deletes retained generations under the ownership lock after validating that their processes are stopped. Tests cover direct/persistent stop, stop-on-exit lifecycle, restart, removal, lock contention and cross-VM isolation. |
| P2 | Idle ingress listeners consumed active-flow quotas | Accept first, then acquire ingress/shared capacity non-blockingly; close excess connections immediately and release partial acquisitions. Tests cover idle SSH/dev listeners with `tcp_flows=1`, outbound availability, exhausted ingress/shared capacity, quota release and shutdown hand-off. |
| P2 | Provider refresh could return expired credentials | Read the clock at cached fallback and before accepting/caching a fetched value, including expiry while the provider blocks. Only still-valid cached credentials can survive a refresh failure; otherwise propagate a contextual provider failure. Tests cover expiry before/at completion, valid fallback, and newly fetched expired values without cache poisoning. |

Expected-failure boundaries remain explicit: DNS errors/timeouts return fixed connection-denial codes; provider execution failures may use only an unexpired cache; explicit removal waits at most three seconds for a shutdown ownership-lock hand-off and otherwise errors without deleting evidence. Malformed VM-removal JSON now returns an error rather than silently assuming an empty failure list or deleting gateway evidence. No unrequested UI or dependency changes were made for the review.

## Worker/DNS review — fixed

| Priority | Finding | Fix and regression evidence |
|---|---|---|
| P1 | Raw TCP could reach shared/special-use destinations | Default IP checks now reject CGNAT `100.64.0.0/10` (including Alibaba metadata `100.100.100.200`), additional IPv4 reserved ranges and IPv6 special-use/transition space. Explicit `allow_ranges` still override the default. Table tests include range boundaries, mapped IPv4, metadata, exceptions and ordinary public IPv4/IPv6. |
| P1 | Quiet VMs lost workers after ten minutes | Clear the between-frame read deadline and start the existing transfer deadline only after the first byte. Partial headers/payloads and blocked writes remain bounded. Real pipe tests cover repeated idle periods and traffic resumption; incomplete-frame EOF remains an error. Authenticated first-frame readiness/recovery deadlines are unchanged. |
| P1 | DNS eviction rebound guest-cached addresses | Refuse new mappings at capacity, rather than evict/reassign issued IPs. Existing answers remain bound for the generation, even beyond the advertised TTL. Regression verifies that resolving B at capacity cannot change cached A's reverse mapping. |
| P2 | Recovery forgot wildcard identities | Restore from a private append-only `dns-mappings.jsonl` via inherited descriptor. Validate VM/generation/policy digest, owner/mode/size and strict JSON. Append/sync before publishing each mapping; a worker-held file lock excludes overlaps. Startup uses `Restore` and registers every restored wildcard address via `Snapshot` before traffic; allocation skips restored IPs. Tests cover restored cached identities, a real worker-pair replacement using the journal, failures/corruption, capacity and descriptor cleanup. |

The DNS journal is separately bounded to 1 MiB and the configured mapping count. Full state refuses new names; persistence I/O failure is fatal to the worker (with DNS SERVFAIL), not a fallback to empty state. Startup file errors are propagated into lifecycle failure with context. Initialized state is never recreated if missing or empty, and corrupt or cross-generation records are never silently ignored. The working `local/completion-venv` and pinned Go 1.26.5 toolchain were used; no root virtual-environment links, dependencies or UI requirements were changed.

## Backpressure/policy review — fixed

| Priority | Finding | Fix and regression evidence |
|---|---|---|
| P1 | WebSocket history grew without bound | Clear completed-message history on every message hook while preserving the relay's independent current-message reference. Per-message authorization/size checks still drop/kill invalid messages. Tests cover 2,048 individually allowed messages with zero retained history, denial cleanup, and 128 real echo round trips. |
| P2 | Concurrent admission exceeded request capacity | Reserve the existing 128-request budget atomically before hooks, provider fetching or audit acknowledgement. Release reservations on rejection, preparation exceptions, response denial and completion; terminal audit errors also release body budgets. Synchronized tests reproduce the final-slot race at both credential and audit waits and verify failure/denial cleanup. |
| P2 | Replacement did not actually drain ingress backlog | Replace expired-deadline `Accept` with non-blocking kernel accepts through `SyscallConn`; close every queued socket until EAGAIN/EWOULDBLOCK. Unexpected errors propagate and the 4,096-attempt listener budget fails readiness rather than continuing with stale connections. Tests require EOF/reset (not timeout), cover closed/empty listeners, and verify fresh traffic after draining. |
| P2 | Different TCP modes could overlap | Reject overlapping host/port scopes across all supported public modes, which all use TCP. Tests cover every mode pair/order with wildcard/exact hosts, and retain valid disjoint ports/hosts. `[network]` default-deny semantics are unchanged. |

Admission exceptions are a fail-closed forwarding boundary: kill the flow and rethrow the original error; cleanup uses `finally`, not fallback parsing or successful forwarding. Ingress retries only interrupted/already-aborted accepts within its fixed budget; an empty backlog is recognized by errno, and other I/O failures reach the worker lifecycle with context. No UI changes or dependency changes were introduced for this review.

## Credential review — fixed

| Priority | Finding | Fix and regression evidence |
|---|---|---|
| P1 | A timed-out lookup's late reply permanently stopped credential delivery | Close a failed broker channel, mark it unavailable, fail waiting lookups and stop the mediator so existing supervision replaces the pair. Do not block shutdown on a reply already queued for another waiter. Tests cover late replies, closed transport, invalid JSON, immediate rejection of later lookups on the closed channel, and a healthy replacement channel. Source and packaged mediator stay identical. |
| P2 | Provider output was buffered without a memory bound | Reuse the command provider's bounded selector-based I/O for supervisor helpers. Enforce 64 KiB combined stdout/stderr while reading, multiplex input with output, and kill/reap failed helpers; the supervisor's existing finalizer kills the whole provider group. Tests overflow stdout, stderr and their combined budget, verify descendant termination and all provider slots returned, and transfer more than a pipe buffer in both directions without deadlock. |
| P2 | Bindings could not narrow unrestricted parent policies | Apply subset checks only when the parent restricts methods/paths. Tests cover omitted and empty parent lists, retained narrow bindings and finalization; existing rejection tests still prevent widening restricted policies. |

Broker framing/decoding/I/O failures are a fatal transport boundary: current lookups receive a fixed provider-unavailable error and the mediator shuts down rather than silently continuing without a reader. Provider I/O/limit failures rethrow contextual lifecycle errors without including output; the existing explicitly allowed unexpired-cache boundary remains unchanged. JSON decoding never falls back to a successful result, and no HTTP request is automatically replayed. No dependencies or worker code changed for this review.

## Latest recovery/state review — fixed

| Priority | Finding | Fix and regression evidence |
|---|---|---|
| P1 | Pending broker work accumulated across replacements | Share the 128-request budget across the supervisor's broker connections. Saturation returns a correlated error without spawning handlers or resetting a healthy channel. Disconnect/shutdown cancels lock/slot waiters, with 100 ms cancellation polling and bounded capacity waits. Running helpers may finish/cache under existing limits. Tests cover three connections sharing a small budget, overload then recovery, disconnect cancellation, independent credentials, deadlines and provider/send/thread-start failures with reservation release. |
| P2 | Clock rollback selected old retained state | Add a private, atomic, synced `current.json` reference and select exclusively from it. Reuse the ownership lock for publication/cleanup, including the inherited supervisor descriptor. Stop preserves the reference; unused rollback clears it before deletion; explicit removal works with a null reference and retains the ownership-lock file. Tests cover backward/equal clocks through status/diagnostics/drift/drain/shutdown, invalid/unsafe/pointerless references, publication/clear/sync failures, null-reference rollback/removal, live-history protection and startup/stop contention. |

The feature is unreleased, so no legacy compatibility or automatic migration was added. Pointerless nonempty state is rejected and evidence remains untouched. Reference I/O failures propagate; a selected directory cannot be deleted until its reference is durably cleared. Broker protocol/transport failures close and cancel only their channel; provider/capacity failures translate to the existing secret-free RPC error at that explicit boundary. Existing unexpired-cache behavior remains unchanged, and requests are never automatically replayed.

The root filesystem now has sufficient space and the full lifecycle suite was rerun, rather than carrying forward the prior environment-induced failures. No Go source, worker artifacts, dependency pins or mediator copies changed in this review.

## Audit guarantee and limits

Admission without an outcome means **execution unknown**: a crash may occur before forwarding or after execution. A persisted admission with a lost acknowledgement also does not prove forwarding. Never infer success/failure or automatically replay. Terminal outcomes remain asynchronous and can be lost on failure; this is not exactly-once execution or lossless final-outcome logging.

The guarantee covers mediated HTTP requests, not TLS passthrough, non-HTTP traffic, DNS or connection establishment. It relies on the host storage honoring sync semantics. Subsequent request-body validation may still deny an admitted request.

Limits remain 1 MiB per serialized event, a one-second end-to-end admission deadline including lock/transport/acknowledgement, a 4 KiB acknowledgement cap, 100 MiB aggregate per-generation logs, and 1,000 records/second with a 2,000-record burst. Admission and outcome each consume byte/rate budgets; these are failure boundaries, not truncation limits. Durable syncs are serialized without batching or new configuration.

## UI integration scope

Authentication, loopback binding, credential isolation, read-only enforcement, resource bounds and lifecycle are our responsibility; upstream UI design/presentation is not. Custom warnings, counters and redesign are not requirements or blockers. The existing 256-event / 8 MiB display window evicts whole oldest events; persistent audit logging is independent and UI snapshots remain outcome-only.

## Remaining release blockers

### 1. Tornado security — verified, not just historical

On 2026-11-11, GitHub advisory ranges, PyPI's version-specific metadata and `uv audit --locked` all confirmed Tornado **6.5.10 is affected** by:

- [GHSA-pw9p-v7r6-gx4p](https://github.com/advisories/GHSA-pw9p-v7r6-gx4p): open redirect.
- [GHSA-r4j5-9xpp-3985](https://github.com/advisories/GHSA-r4j5-9xpp-3985): chunked-body resource exhaustion.
- [GHSA-pcg2-g6cj-q92c](https://github.com/advisories/GHSA-pcg2-g6cj-q92c): folded-header quadratic CPU use.

All list affected versions `<6.6`, first fixed in `6.6`, and are not withdrawn. These are not issues already fixed by upgrading from 6.5.5 to 6.5.10. The full audit found these three vulnerabilities and no other reported vulnerabilities in the current 79-package environment. This is known-advisory evidence, not proof of absence of other flaws.

[PyPI mitmproxy metadata](https://pypi.org/pypi/mitmproxy/json) still reports latest stable 12.3.1 with Tornado `>=6.5.0,<=6.5.10`; [Tornado 6.5.10 metadata](https://pypi.org/pypi/tornado/6.5.10/json) reports the same findings. No compatible fixed release was available at inspection. Do not suppress findings or force conflicting metadata; require upstream support or a separately approved patch/mitigation.

### 2. Darwin amd64 wheel-only dependencies

Cryptography 50.0.2 has no Darwin amd64 wheel; the locked CPython 3.12 wheel-only resolution fails. Linux amd64 installation and Darwin arm64 wheel-only resolution pass.

[PyPI Cryptography metadata](https://pypi.org/pypi/cryptography/json) shows that 49.0.0 and every published stable 50.x release lack Darwin amd64 wheels. 48.0.1 provides universal2 wheels but has known vulnerabilities, including fixes requiring 49.0.0 and 50.0.0. A vulnerable downgrade, source-build fallback or platform removal was not introduced. No compatible secure wheel-only solution was identified.

### 3. Final integration and runtime evidence

The test guest is unavailable. Both the standard build and a workspace-backed cache retry failed with Docker/helper `No space left on device` while populating/building guest artifacts. Explicitly selected single-/two-VM TCG tests fail their missing-image prerequisite; neither is passed or skipped. Restore build capacity and rebuild the guest before rerunning.

Native macOS runtime, final stability/performance acceptance and additional license/fuzz checks remain unverified in this completion run. Existing four-VM performance deferral remains explicit, not passed or newly extended to other gates.

## Validation evidence — current sources

| Check | Result |
|---|---|
| Baseline before changes | 426 fast tests; 33 mediator tests plus 8 subtests passed. |
| Final fast suite | 510 passed, **90.79% coverage** on CPython 3.12.15. |
| Final mediator suite | 88 passed plus 8 subtests; two dependency deprecation warnings. |
| Admission regressions | GET/streaming POST and concurrent HTTP/2 blocked before acknowledgement; concurrent request IDs; sync failures, timeouts, invalid/oversized/stale/closed acknowledgements; crash before acknowledgement and after forwarding; replacement preserves unfinished admissions; duplicate-hook suppression; storage/rate/identity/pipe-creation failures; mocked Darwin full-sync requirement. |
| Ruff / source-package parity | Passed. |
| Prior Go tests / worker builds | `go test ./...` and Linux amd64 / Darwin amd64 / Darwin arm64 artifact builds passed with Go 1.26.5 after the address/framing/DNS recovery and ingress-drain fixes. Race instrumentation could not run: cgo is disabled by default, and enabling it found no C compiler. |
| This review: Go verification | Not rerun: the pinned Go toolchain is absent in this VM. No Go source changed and workers were not rebuilt. |
| Clean wheel installation | Built wheel, installed hash-locked dependencies wheel-only in a fresh environment, and installed wheel without dependency re-resolution. |
| Installed runtime smoke | Installed worker executed; packaged mediator passed test-fixture admission/outcome and authenticated read-only UI smoke; production launcher independently denied loopback without the test-only exception. Fixture launcher is not packaged. |
| Platform wheel-only checks | Linux amd64 and Darwin arm64 passed; Darwin amd64 failed for Cryptography 50.0.2. |
| Dependency audit | Failed with the three confirmed Tornado advisories. |
| Single-/two-VM explicit-TCG | Blocked: guest build failed with disk-space errors; both selected tests fail missing-image prerequisite. |
| macOS runtime | Unverified; cross-build and mocked sync tests are not native validation. |
| Four-VM performance | Existing deferral retained; not run or passed. |

Earlier local loopback smoke measurement (before the review fixes; 30 requests, `/tmp` storage, not final performance acceptance or a TCG benchmark): durable sync median **2.684 ms**, p95 **3.132 ms**, maximum **5.257 ms**; HTTP request median **5.016 ms**, p95 **5.923 ms**. These measurements do not establish production throughput or clear deferred performance gates.

## Next actions

1. Resolve the verified dependency security and Darwin amd64 wheel blockers without bypassing gates.
2. Restore guest-build capacity and run single-/two-VM explicit-TCG acceptance, including audit failure/recovery isolation.
3. Complete native macOS, remaining stability/performance and license/fuzz acceptance on final sources/dependencies; record the existing performance waiver accurately.
4. Repeat affected tests/packaging/audit after any dependency changes before release approval.

## Sensitive evidence warning

Private logs and authenticated UI exports intentionally contain guest-supplied URLs/query strings, potentially including tokens or personal information. Preserve owner-only permissions and review evidence before sharing it. Known placeholders and injected host-provider credentials remain redacted.
