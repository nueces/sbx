# Claude/OpenCode provisioning compatibility — 2026-11-17

**Overall NOT MERGE READY.** This follow-up fixes and validates agent provisioning; it does not clear unrelated gateway/security/distribution gates. Final live verification and cleanup are recorded below. No commits, pushes or merges were made.

## Candidate and private evidence

- Final wheel: `sbx-0.2.6.dev0-py3-none-any.whl`, SHA-256 **`bf0c5c8853b101efd5e65b9b38bfb34e2a7ea03d7b754c610d58a8e84e10514d`**.
- Feature worktree: `/home/nueces/code/sbx/feature/rootless-network-gateway`; baseline HEAD `3477517a27a56d0479ca700194f4c02ab84c9e25` plus pre-existing dirty work.
- Private evidence: `local/gateway-validation/20261117T105341Z-agent-provisioning/`. Keep raw synthetic authentication/npm/debug data private.
- The initial diagnostic used the prior `fcd32b07…` wheel. Provisional `3702c1bf…` added the CPU/version fix, then exposed the automatic-machine restart problem. Provisional `c8cc2339…` fixed machine selection, then reproduced the ingress TIME_WAIT restart problem. Only `installed-verified/`, `dist-verified/`, `agent-verified-*` and the final campaign checks establish evidence for the final hash above.
- Forty-four source/assets files match source/wheel/clean installation. Four mediator mirrors match. Production Go, lockfiles and all three packaged worker hashes are unchanged; no packaged-worker rebuild is claimed. `candidate-integrity.json` protects the dirty baseline.

## Reproduced cause: our emulated CPU profile

Both failures were reproducible with official native packages inside the guest:

| Package | Same native binary SHA-256 under both profiles | `qemu64` | TCG `max` |
|---|---|---|---|
| Claude Code **2.1.326** | `b6b3647d981130dc44fc670846ae97b6cde531ffb667cf9c4d17cfcafb38cb37` | SIGILL | `2.1.326 (Claude Code)`, exit 0 |
| OpenCode **1.19.6**, glibc/baseline candidate | `aab0a58cd7fc5a1f17495b76c08e79989d331653d8b63156aca2f6e9e36d1173` | SIGILL | `1.19.6`, exit 0 |

The observed OpenCode baseline and ordinary glibc package binaries have identical bytes in this release; both were checked. Node **22.23.5**, npm **10.9.9**, glibc **2.41**, guest user, binary bytes and resource/security settings were unchanged in the A/B comparison. Native package code was never executed on the host. Registry metadata/tarball integrity checks and source receipts are retained.

`qemu64` did not expose SSE4.1/SSE4.2/AVX/AVX2; `max` does. This proves profile incompatibility and a working supported replacement, not the precise minimum faulting instruction. Attempts to fetch the vendor setup/CPU documentation returned HTTP 403; the report does not invent a documented minimum CPU requirement.

**OpenCode's musl error was secondary.** Its official postinstall first copies and executes glibc candidates with `--version`; only after those fail does it try musl candidates. A diagnostic-only copy logged both initial `SIGILL` results before `EBADPLATFORM`. Under `max`, the first glibc probe succeeds. Postinstall source is byte-identical between **1.19.5 and 1.19.6**. No vendor patch, special native-package pin, forced platform flag, unofficial binary or broadened policy is necessary.

## Changes

1. The shared Linux/no-KVM gateway fallback now selects QEMU TCG **`max`** instead of `qemu64`. ARM already used `max`; KVM/HVF host behavior and non-gateway argv remain unchanged. No KVM, artificial clock scaling or new resource/security exception.
2. Restarting a provisionally successful preset reproduced a microVM kernel TSC-calibration hang. For automatic x86 machine selection in the same no-KVM backend, use supported **q35** before SDK argv construction. This also works for existing stopped VMs whose saved config says `auto`, without rewriting that config. Explicit machine selection is preserved. Real recovery of both provisional VMs and new unit coverage verify the path.
3. Claude/OpenCode tools provisioning now requires a **15-second `--version` check as the runtime user**, for fresh installs and existing commands. Nonzero/SIGILL/timeout fails provisioning; errors are redacted. A broken existing command is not silently overwritten or upgraded. The installation deadline remains 240 seconds in live cases.
4. Claude retains SDK preset installation/configuration/auth-copy handling, followed by the shared installed-tool check. OpenCode retains the existing official npm installer and user prefix. Pi/Codex/specify behavior is not changed.
5. A further immediate-restart failure reproduced even with serialized VMs: ingress TCP connections in host TIME_WAIT prevented rebinding. Set **SO_REUSEADDR**, not SO_REUSEPORT, before the exact-address bind. A real TCP regression fails before the fix, then verifies immediate rebind **and rejection of a second live listener**. Address/ownership checks and admission remain unchanged.
6. Direct TCG integration/performance fixtures use the same new profile; performance metadata records `guest_cpu_model=max`. Old `qemu64` results are historical, not acceptance for this profile.

No public CLI/config option, application dependency or Go production change was added.

## Verification completed

- Baseline fast suite: **1,132 passed** before changes.
- Final fast suite: **1,164 passed, 92.21% coverage**.
- Final installed mediator suite: **146 cases plus 8 subtests**, no failures/errors/skips; actual installed launch/import proof retained.
- **Seven Linux integration cases pass**, including the real TCP TIME_WAIT/live-listener regression, explicitly configured four-VM TCG capacity/isolation, direct integration, observer and TLS-origin checks. Installed module/worker identity proof and four-pair markers/counts are retained.
- Regressions cover fresh/existing commands, stdout/stderr version output, SIGILL/nonzero exit, timeout, redaction, user/HOME/lease/deadline preservation, no silent reinstall, SDK ordering/cleanup/config-copy behavior, fresh/stopped automatic machine resolution, and unchanged KVM/ARM/Darwin assembly behavior. Platform assembly tests are not native macOS execution.
- Whole-project Ruff lint, formatting of 13 touched Python files and `git diff --check` pass. One post-run integration-test import-spacing correction is AST-identical to its executed snapshot and its focused real-TCP regression was rerun successfully; the installed application bytes did not change.

## Live preset acceptance

Final cases use fresh public CLI provisioning, a prepared Debian image, UID 1000 `agent`, isolated host state and registry-only GET/HEAD policy with normal TLS verification. No real-account login or paid inference. Every final VM removal is gated on successful guest-log and complete regular state-generation archiving, including DNS journals; state is copied again after writers stop.

**PASS — Claude Code 2.1.326 and OpenCode 1.19.6**, on the final wheel above. Fresh public creates completed in **111.17 / 129.03 seconds**, respectively, with independent non-root version/inventory inspection. Both also pass stop/run persistence, idempotent `--runtime-install` reprovisioning, existing broken-command rejection (exit 42, no overwrite), restore/version checks, and registry denial under an explicit default-deny policy. The denied uncached npm request fails at gateway-controlled resolution, without a direct fallback. Guest npm logs and complete regular state generations, including DNS journals, were archived before removal. Pi **0.73.1** and Codex **0.169.0** also pass fresh create/non-root version/inventory smoke checks. `agent-summary.json` verifies four final-wheel PASS outcomes, ten archived state generations/request-audit logs/DNS journals, and complete guest npm log archives before removal.

The first immediate-restart error was initially suspected to be parallel SSH-port contention. It recurred with a single serialized VM; a standalone TCP regression established host TIME_WAIT as the cause. The fix above changes only closed-connection address reuse, not port allocation or live-listener sharing. Failed provisional attempts remain in the ledger.

## Reference performance and actual-VM stability

The user approved the verified CPU-profile change. Reference performance and resource/stability must therefore be rerun against this final wheel with matched gateway/SLIRP CPU profiles, unchanged thresholds, a four-CPU cpuset, 8-GiB/no-swap ceiling and no visible/inherited KVM. The separate count-only observer remains privileged; gateway/workload processes remain UID 1000 with zero effective capabilities. The independent 64-MiB OOM probe and affinity-escape rejection verify enforcement; probe kills are not gateway OOM failures.

**Performance rerun: FAIL overall, with 10 predicates passing and only readiness failing.** On matched `max` gateway/SLIRP guests: HTTPS **240.02 Mbit/s** versus SLIRP **388.51 Mbit/s** (**61.78%**), added established p95 latency **6.6403 ms**, idle pair RSS p95 **80,359,424 bytes**, and three origin-proven 128-request overlaps of at least 64 seconds pass their unchanged targets. Paired added readiness p95 **4.2894 seconds** still fails the ≤1-second target. As before, that paired measure includes guest boot/DHCP variation and does not isolate gateway startup overhead.

The absolute-throughput failure is superseded for this new profile, not reclassified as a pass on the older `qemu64` wheel. The increase follows a CPU-profile change, not solely a gateway software optimization. Raw data and executed harness identity are retained in `performance/`. No target was weakened and unrelated redesign remains out of scope.

**Actual-VM rerun: FAIL — resource growth**, not a shortened run. The unchanged installed application completed **605.17 seconds / 88 mixed iterations**, with genuine worker replacement, unchanged guest boot identity, **295 samples**, a **4.3817-second maximum sample gap** and no missing-metric/monitor failures. HTTPS, incremental SSE, WebSocket, HTTP/2, populated Git/package and ingress work all completed. Sampled peaks were **94,224,384 bytes RSS / 28 FDs / 2 processes / 14 threads**, below absolute ceilings.

Per-pair post-warmup RSS growth was **5,042,176 / 5,046,272 bytes**, exceeding the unchanged 4-MiB allowance; thread counts grew by one/two against an allowance of zero. The recovered pair's matched warm-to-final quiescent RSS also grew **6,942,720 bytes** and three threads. These are failed resource targets, not a diagnosis of an unbounded leak. Complete state, DNS journals, guest logs and **330 count/identity observer packets** were retained; the earlier audit counterexample was not reproduced and is not thereby cleared.

## Evidence limitations and remaining gates

- The initial A/B diagnostic VM archive helper had a syntax error and removal continued. Host A/B outputs, native hashes and official package sources survive, but that diagnostic VM's complete guest npm logs/state were not retained. This limitation is explicit; helper compilation and successful full archiving gate all final acceptance VM removals.
- Final live presets and seven Linux integration cases pass. Readiness/resource growth, the prior unexplained audit failure and remaining broader acceptance scenarios are still open.
- Tornado/mitmproxy compatibility, secure Intel Cryptography wheel distribution and separate licensing review are not cleared by provisioning success. Native macOS execution remains user-deferred, not passed.

## Cleanup and integrity

All owned VMs are removed and the isolated VM inventory is empty. Synthetic origin/network holder, private device/mount views, reference cgroup and root-owned observer staging are gone; host controllers are restored. `/tmp/sbx-gwd` is removed after fixture/state retention. The prior prepared image and the verified archived test image are untouched. No OS packages or manual-package flags changed. The measured subgroup has zero OOM events/kills; the separate probe accounts for the hierarchy's intentional OOM event/two kills. Recorded hierarchy memory peak was **4,695,511,040 bytes**.

`cleanup-host.json`, `cleanup-scratch.json`, `os-package-delta.json`, `agent-summary.json`, `candidate-integrity.json` and the command ledger retain these facts. Source/wheel/install parity, worker hashes and unrelated dirty-file preservation are rechecked at completion. Full raw authentication evidence remains private; no overall merge acceptance is claimed.
