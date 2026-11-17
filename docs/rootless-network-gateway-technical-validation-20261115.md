# Technical validation follow-up — 2026-11-15

**NOT MERGE READY.** Scope: #4 performance, #5 prepared-base presets, #6 FD measurements and #8 final-artifact acceptance. Licensing (#7) is handled on another branch; dependency/macOS gates are not waived.

The campaign stopped at the approved architecture-decision boundary: a reproduced HTTP connection-pooling defect cannot be safely repaired by simply restoring an open connection's address. Independent checks completed, but the full acceptance plan did not.

## Results by gate

| Gate | Disposition | Explanation |
|---|---|---|
| #4 Performance | **FAIL** | Reference isolation works and four pairs pass capacity/isolation checks, but established HTTP/1 reuse stalls on request six. The complete throughput/latency/concurrency benchmark does not finish. |
| #5 Prepared-base presets | **FAIL / partial prerequisites verified** | The real curated image builds, creates the non-root account and supplies valid sudoers/visudo. Node CA trust was repaired and verified live. Pi installation subsequently still times out; the other presets were not live-accepted. |
| #6 FD measurements | **Collector verified; acceptance incomplete** | A separate privileged observer obtains real counts without changing gateway protections. Six real-pair snapshots and descendant/identity tests pass. Complete matched quiescent/load/replacement and long-run growth evidence is still missing. |
| #8 Final artifact | **PARTIAL, not accepted** | Linux wheel installation/parity, fast tests, ordinary Go and live integration pass. The new reuse regression fails; race testing lacks a compiler; the exhaustive operator/fault/protocol and ten-minute campaign was not completed. Required native macOS results also remain external. |

## What was established here

- Current host: 12 logical CPUs and approximately 31.3 GiB RAM; supported observation is not based on the older host's configuration.
- Temporary cgroup reference: CPUs **0–3**, `memory.max=8589934592`, `memory.swap.max=0`. An eight-process CPU probe could not escape the four-CPU cpuset; a 64-MiB subgroup killed its deliberately over-limit allocation. No gateway privilege was added.
- **Deviation recorded:** this kernel has no `cpu.max` file, so CPU quota/throttling could not be configured. The actual CPU bound is the hard four-CPU cpuset, not a claimed quota. The initial quota setup failure is retained.
- A private mount/device namespace hides KVM; the workload runs as UID 1000 with zero effective capabilities. Real guest commands use explicit TCG and default clocks. The observer moves only itself outside the measured cgroup.
- The public image-build CLI completed in **259.42 seconds**, using the curated Debian base and an explicit **8192-MiB rootfs size**. This was not a manually finalized minimal test image or a claim about the default 20480-MiB size.
- Live prepared-base inspection found user `agent` UID/GID 1000, `/usr/sbin/visudo`, a successfully parsed guest sudoers rule and only `192.0.2.1` in the resolver configuration.
- Four installed-artifact gateway pairs were responsive and passed the existing peer-crash isolation check in the constrained TCG environment. Their measured pair counts were **21 FDs, two processes and nine threads each**; RSS ranged from **86,638,592 to 87,175,168 bytes**. These are capacity snapshots, not a ten-minute leak result or readiness/RSS percentile acceptance.

## Reproduced blockers and bounded fixes

### HTTP/1 pool starvation — unresolved

The new `test_http1_reuse_exceeds_upstream_connection_limit` sends 16 requests on one TLS client connection. It reproducibly stalls at **index 5**, the sixth request, rather than reaching the origin again.

The mediator changes `Server.address` from logical hostname to validated numeric IP before connection establishment. In the pinned mitmproxy implementation:

1. HTTP connection reuse compares the request's logical `(host, port)` against `Server.address`.
2. The numeric address does not match, so otherwise idle upstream connections are not reused.
3. The connector has a per-client, per-endpoint semaphore of **five**, held for each upstream connection's lifetime.
4. After five idle connections, another connect waits for a slot. Diagnostic traces show `server_connect` without `server_connected`, while the event loop remains idle and the origin receives no next request.

A restore-after-connect experiment did not solve this: mitmproxy's `Server.__setattr__` explicitly rejects changing the address of an OPEN connection. That attempted hook and its inadequate mock-based test were removed. **No private-field bypass, connection-limit increase, keep-alive disabling or request replay was retained.**

The next decision is how to separate validated numeric dialing from logical HTTP pool identity through a supported, auditable routing/connector boundary. DNS revalidation on reconnect, logical TLS/SNI identity and per-request authority/policy checks must remain intact. This exceeds the approved minimal-fix boundary; the new failing regression is deliberately not hidden with an xfail.

### Benchmark TLS accept loop — fixed, insufficient by itself

The origin fixture performed TLS handshakes in its accept loop. A raw idle TCP peer blocked a second TLS client; closing that peer allowed the request. The fixture now defers the handshake to the request thread and bounds accepted-socket I/O. A real TLS/keep-alive regression passes. The full gateway benchmark still fails because of the independent pooling issue above.

### Node CA trust — fixed and verified

The first live npm attempt failed with `UNABLE_TO_VERIFY_LEAF_SIGNATURE`: Node did not use the OS trust store updated by guest CA installation. CA installation now atomically creates a login profile defaulting `NODE_EXTRA_CA_CERTS` to the **public** gateway certificate. Explicit existing values are preserved; certificate verification is not disabled.

Tests exercise idempotent installation and profile selection. A real guest Node HTTPS request to npm returned **200** with the expected supplemental CA path. This does not establish successful complete agent provisioning: the later Pi installation exited **124** at its existing 240-second installation bound. Its remaining timeout is consistent with the pooling defect, but every contributing cause has not been independently established.

### OpenCode SDK mismatch — fixed, live acceptance pending

The CLI exposes OpenCode, but SmolVM 0.0.28 has no `opencode` preset. That lookup was reproduced. The explicit OpenCode path now uses the existing sbx runtime installer, retaining the resource defaults and provisioning order. A regression verifies no unsupported SDK lookup occurs. It has not been live-accepted.

### FD observer — implemented and exercised

`gateway/scripts/resource_usage.py` is validation-only. Its explicit root-owned observer reads FD counts, not descriptor contents or targets. It checks UID, cgroup, PID start time and process-tree stability. Missing/incomplete measurements raise an error instead of returning zero. CI and the development runbook stage the observer separately; shipped gateway processes remain unprivileged and nondumpable.

This kernel also lacks `/proc/PID/task/TID/children`. Descendants are therefore discovered through PPid/start-time metadata with a before/after tree check, rather than silently omitted. Tests cover known-count nondumpable processes, descendants, denied observation, stale identity, process exit, invalid results and observer failure/timeout. Retained observer packets confirm that the collector was outside the reference cgroup.

**Separate limitation:** the pre-existing production `gateway._process_tree_usage` still suppresses inaccessible `/proc` data; its FD count is not acceptance evidence. Existing per-process kernel limits were not weakened. This campaign did not redesign production resource telemetry or grant it observation privileges.

## Final candidate and checks

Wheel: `sbx-0.2.6.dev0-py3-none-any.whl`\
SHA-256: `fc81714b1748aba61f7c07512d427981eabc4dbe99a5accad3635497e4b7594a`

**44 packaged files** compare byte-for-byte between source, wheel and installed environment. Source/package mediator copies also match. No Go production source changed in this follow-up; the three existing release workers were retained, not claimed as newly rebuilt/native-macOS-tested artifacts.

| Check | Result |
|---|---|
| Fast suite | **1107 passed**, **92.24%** coverage |
| Mediator suite | **95 passed + 8 subtests; one new reuse regression FAILED** |
| Explicitly staged observer tests | **17 passed**: 15 fast cases and two real nondumpability/descendant cases |
| Installed Linux integration suite | **5 passed**, repeated with raw output/resource packets retained |
| Four-VM installed TCG check | **1 passed**, four responsive pairs and peer-crash isolation |
| Fresh ordinary Go tests | PASS, `-count=1` |
| Go vulnerability check | Zero reachable findings; two imported-package and 22 required-module findings reported as not called |
| Fresh race suite | **BLOCKED:** enabling cgo exposed a missing host C compiler; no passing race claim |
| Final fuzz/ten-minute/full operator-fault matrix | **Not completed after the architecture stop** |

Functional fixture results do not replace missing production-path or exhaustive evidence. In particular, no new accepted throughput/latency/SLIRP ratio, sustained 128-request overlap, populated-Git matrix, real storage-fault matrix or final-wheel 600-second mixed run is claimed.

## Evidence, cleanup and limits

Private campaign directory: `local/gateway-validation/20261115-technical-acceptance/`.

- `baseline.json`, `cases.json`, `stages.jsonl`, `results.json` record scope, raw commands/outcomes and case dispositions.
- `reference-enforcement-cpuset.json` and namespace probes establish the actual resource boundary; the unsuccessful quota probe is separate.
- `candidate-final.json` records wheel, worker and real curated image hashes.
- `resource-packets.jsonl`, recorded integration logs and the isolated observer tests contain actual FD evidence.
- `origin-idle-tls-before.json` / `origin-idle-tls-after.json` establish the fixture correction.
- `http1-reuse-before.log`, `http1-reuse-after.log`, the mediator self-stack/trace snapshots and origin receipts support the unresolved reuse diagnosis. The failed restore experiment is not part of the final code.
- npm/CLI and `prepared-base-node-ca-live.log` retain the certificate failure/fix and remaining provisioning failure separately.

The public removal path cleaned the failed Pi VMs. **Full failed-preset audit generations were not archived before those removals**; retained CLI/npm/Node diagnostics are partial evidence, not a complete audit-retention or live-fault acceptance result. Historical acceptance-campaign evidence was not overwritten.

Cleanup and unrelated-file integrity are recorded by the campaign finalizer. No commit, push, merge, dependency-lock update, licensing change or production security-policy exception is part of this follow-up.
