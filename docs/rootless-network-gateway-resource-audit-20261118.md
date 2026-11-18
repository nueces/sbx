# Resource and audit follow-up — 2026-11-18

**IMPLEMENTED; ACCEPTANCE FAILED — NOT MERGE READY.** Measurement and audit diagnostics are improved, and a new availability counterexample is captured. Worker-thread stability, audit availability and current-artifact readiness remain blockers. No commits, pushes or merges were made.

**Subsequent user decision:** the audit deadline was later increased to five seconds for early adoption; see [the decision and targeted validation](rootless-network-gateway-audit-five-seconds-20261118.md). This report and its sealed artifacts retain the earlier one-second policy and outcomes; they are not reclassified as passes.

## Identity and scope

- Baseline HEAD: **`b828b50`**. Final fourth wheel: **`48ed2c2cc09899c4f426fe2ed04af7e7bfc03e61a04ce79726a4f263daeb73d7`**.
- Private evidence: `local/gateway-validation/20261118T092234Z-resource-audit/`, including `results.json`, `case-ledger.json`, `captured-audit-counterexample.json`, executed harness copies and retained generations. Raw synthetic authentication/state/debug evidence is private, not release material.
- Clean native installation `/tmp/sbx-gwf/final-4` was verified against all **44** packaged assets and four mediator mirrors. All three worker hashes and production Go source are unchanged; no worker rebuild is claimed.
- Reference: unprivileged UID1000/capabilities0, hard CPUs0–3, 8GiB, swap0, q35/TCG `max`, no visible/inherited KVM, isolated HOME/state/project and closed synthetic-origin namespace. `cpu.max` is unavailable: **cpuset, not CPU quota**. No login, inference, real credentials or production private-destination exception.

## Changes versus policy calibration

The former **4MiB** allowance was a harness heuristic introduced in `9dd2007`, not the design's runtime memory ceiling. The user approved **16MiB one-time post-warm-up retained RSS growth per pair**, conditional on independent long-soak plateau checks. It is not 16MiB per interval, nor a change to the **256MiB idle target /512MiB ceiling**. Historical failures under the old rule remain historical failures.

The corrected harness uses the same **150-second representative warm-up** for initial/recovered pairs, two-second sampling and quiet checkpoints, PID/start-time/generation identity checks, per-component and separate supervisor counts, complete violations and missing-metric failures. Long soaks require the final six five-minute RSS medians to span at most **2MiB**, with ongoing mixed work. The workload floor, **23 iterations/window**, was frozen from calibration before holdouts; no threshold changed after failures. Smoke passes alone do not establish resource acceptance.

Threads retain zero-growth checks unless a finite pool has ownership-derived evidence. A separately labeled actual-VM counter profile established the existing mediator executor maximum **8**, plus main/monitor/broker-reader **3**, giving ceiling **11** for the tested policy without header hooks. Native and Python counts agreed. This is configured capacity, not an observed-maximum exemption. Worker ownership has no approved finite budget, so its zero-growth rule remains. Long-run thread high-water must also stop increasing.

The third-candidate profile is linked to the final fourth candidate by whole-module AST equality after normalizing raise causes: the sole mediator change suppresses raw exception chaining. Ownership code and resource criteria are unchanged. Short counter profiles show small sampled admissions/routes/TLS collections and native allocation growing more than traced Python allocation, but are **not retaining-owner or leak proofs**; sampling at fixed event cadence is not a population maximum. Go scheduler traces show an additional retained idle scheduler thread in a diagnostic pair, but the runtime's default10000-thread guard is not an approved worker budget. Full isolated-protocol attribution remains incomplete. No forced collection, allocator trimming, thread preallocation, restart-as-leak-fix or vendor mutation was used.

## Final-wheel resource results

| Run | Work completed | Result |
| --- | --- | --- |
| Holdout1, no replacement | **3602.177s /531 iterations** | **FAIL**: worker8→9 late; worker tail/quiescent growth |
| Holdout2, no replacement | **297 iterations; last sample2072.314s /992 samples** | **FAIL/incomplete**: audit deadline failure; missing process correctly fails measurement |
| Replacement1 | **605.689s**, genuine replacement | **FAIL**: worker thread/quiescent growth |
| Replacement2 | **601.371s**, genuine replacement | **PASS** under frozen smoke criteria |

Holdout1 warmed-to-final pair RSS increased **15,908,864 bytes**, below16MiB; final-six RSS median span **368,640 bytes**, below2MiB; idle FD count remained23. Its RSS/plateau checks pass, but its worker-thread checks do not. Holdout2 did not complete: **conditional RSS acceptance across both long soaks is not established**. There is no final-resource pass.

The earlier unmodified-application calibration ran **3605.708s**, with warmed-to-final RSS growth **16,015,360 bytes** and median span **933,888 bytes**; it failed thread criteria. That is calibration, not final-wheel acceptance. The interrupted second-candidate provisional holdout and diagnostic runs are retained separately, not relabeled passes.

## Audit: implemented repairs and captured availability failure

Implemented:

- A bounded **2KiB** validated first-failure cache and private atomic `audit-failure.json` sidecar, with owner/mode/no-symlink/generation/incarnation checks. Existing state/admission/outcome schemas remain unchanged.
- Optional redacted details through existing authenticated diagnostics. Cached status/diagnostics do not read the failed primary filesystem. Fresh clients still need trusted endpoint/generation metadata; no discovery/authentication bypass.
- Fixed operation/reason/errno/timing/internal-ID fields; no credentials, bodies, URLs or raw exception payloads. Correct rate-versus-storage classification, and preservation of an early failed generation even before admission.
- Final monotonic deadline validation: a deterministic regression showed `select` readiness arriving after the transaction deadline could previously accept a late ACK. The deadline is still **one second**, ACK cap **4KiB**, sync before ACK/forwarding.
- UTF8-only state parsing retained, documented decoder failures handled, and raw exception chaining suppressed. Decoder exhaustion is explicitly simulated in its regression: deeply nested input alone did not exhaust the pinned parser. Superseded wheels, failed initial test assumptions and before/after evidence remain separate.

**A new spontaneous counterexample was captured during holdout2**, at about13:01:49UTC:

- Mediator: acknowledgement deadline at **1,001,140us**, followed by fail-close.
- Controller: total **2,626,595us**, including measured **sync2,626,329us**; subsequent ACK write failed with **EPIPE32**.
- Matching retained event: admitted `/cmd/ws`, no outcome,7135audit records. The controller cache records its first observed failure (secondary EPIPE); the mediator log identifies the earlier deadline. Correlate both, rather than calling EPIPE the original storage error.

This captures a **slow-sync-stage/deadline mechanism**, not proof of physical disk latency: elapsed time can include scheduling. The new permanent event-controlled slow-sync/closed-ACK regression verifies no early ACK, retained timing and fail-closed state; it locks down safety, **not an availability repair**. No compliant availability fix has been demonstrated. Longer deadlines, early ACKs, dropped audits, replay and silent recovery of admitted unknown transactions were not substituted.

The historical247-record failure also ended at admitted `/cmd/ws`, but has no causal timing/errno. That is consistent with, **not proof of**, the same mechanism. Audit record count alone cannot exclude shared-log contention. **Historical attribution remains UNRESOLVED; current availability remains FAIL.** Next investigation needs matched sync/scheduling/I/O attribution and a repair within the unchanged durable one-second contract, then fresh final-artifact holdouts—not repeated attempts until one passes.

## Regression, storage and performance gates

- **1191 fast tests /92.19% coverage**, source and clean-installed mediator **160+8 subtests**, and **8 Linux integration cases**, including four-VM isolation, pass. The new real supervisor-protocol subprocess test covers discarded stderr/unavailable state reads; it is not itself a QEMU test.
- Ordinary/race Go suites pass. Go/three workers are unchanged; earlier fuzz/crossbuild evidence is not presented as a fresh run. Whole-project Ruff, formatting of ten affected Python files, diff checks and source/wheel/retained-installation parity pass.
- Three final-wheel **actual-VM kernel storage cases PASS**: ENOSPC before admission0origin, EIO before admission0origin, ENOSPC outcome1origin/no replay/unknown outcome. Authenticated cached diagnostics remain queryable while storage is broken. ENOSPC captures controller append errno28; this campaign also captures **controller sync errno5** for EIO, in addition to the independent audit-inode probe. All owned storage is restored/removed after verified archival.
- Full reference performance: **10/11 predicates PASS; overall FAIL**. Added readiness p95 **1.253340s** fails the unchanged1s target. HTTPS **288.474Mbit/s**, matched-SLIRP ratio **0.760639**, added established p95 latency **9.750730ms**, idle RSS p95 **87,474,176 bytes**, three sustained128-request overlaps, backpressure and log bounds pass. The previous wheel's native-storage readiness pass remains historical, not clearance for this candidate. No startup marker, profile, clock, target or warm launcher changed to hide the failure.

## Evidence retention and cleanup

Audit fail-close made SSH archival unavailable. The archive guard correctly blocked removal. After a graceful ACPI request did not stop the guest, public CLI stop retained its disk. A byte-identical **8GiB raw-disk archive** was streaming-hash verified; guest logs were then recovered from a **disposable copy** after journal recovery, mounted read-only. Original disk hash remained unchanged. Guest members/hashes, stopped generation/runtime files and console logs were verified **before** public VM removal. No failed transaction was replayed or failed guest restarted.

All normal/fault generation regular files, DNS journals, runtime configuration and guest logs are retained with verification. Earlier historical missing DNS/runtime files are not reconstructed. Current helper snapshots are not claimed identical to every earlier private-helper revision; executed stability harnesses and wheel identities are retained per phase. Failed helper attempts remain visible. No independent observer-packet spool was deployed in this campaign: **6156 validated parsed stability sample rows**, performance data and exact root-staged count-only observer code are retained; no raw-packet count is invented.

The final native installation's **4467 regular files** were archive-member/hash verified before scratch deletion: `installed-native-final.tar.gz`, SHA **`d67c4921b78f008f9733848d79639f8232eca7d7040564758662193eeebd8a6f`**. Restore its original prefix or clean-reinstall locked wheel/dependencies for valid entrypoints. Empty VM inventory, stopped fixtures, removed owned loops/mappers/cgroup/staging/scratch, restored controllers and untouched historical images are verified. Workload OOM0; hierarchy OOM1/kills2 is the independent deliberate64MiB probe. Peak reference memory **3,004,764,160 bytes**.

**Do not claim OS packages unchanged.** This campaign requested no packages, but a separate UID1000 `apt install -y htop ncdu vim` transaction at11:40:07UTC added seven packages during the first holdout: direct `ncdu1.22-1`, `vim2:9.1.1230-2+deb13u2`; dependencies `libgpm2 1.20.7-11+b2`, `libsodium23 1.0.18-1+deb13u1`, and `vim-common`, `vim-runtime`, `xxd` at `2:9.1.1230-2+deb13u2`. No removals/upgrades; manual additions ncdu/vim. Inventories and apt history are retained; those host changes were not reverted or attributed to the later13:01 failure without evidence.

## Remaining blockers

Worker-thread boundedness/tail acceptance, slow-sync/deadline availability, current-candidate readiness and fresh complete final-artifact holdouts remain open. Broader live negative-matrix gaps, recorded third-party security/Intel macOS distribution conflicts, licensing and default guest-kernel Landlock remain separate. Native macOS execution is user-deferred, **not PASS**. None is cleared by these regression counts or the revised RSS policy.
