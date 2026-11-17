# Final Linux gateway validation — 2026-11-16

**NOT MERGE READY.** This campaign exercises the remaining preset, reference-performance/resource and actual-VM validation workstreams. Passing subcases do not clear failed targets or unexecuted cases. No commits, pushes or merges were made.

## Candidate and evidence

- Wheel: `sbx-0.2.6.dev0-py3-none-any.whl`.
- SHA-256: **`fcd32b07caabccdb435abdfb301b8fe098e26995a8676d7e193ffada001cf90c`**.
- Feature worktree: `/home/nueces/code/sbx/feature/rootless-network-gateway`, HEAD `3477517a27a56d0479ca700194f4c02ab84c9e25`, plus preserved dirty work and this campaign's changes.
- Private evidence: `local/gateway-validation/20261116T173106Z-final-linux/`. This includes commands, bounded-stage exits, failed attempts, source baselines, installed environments, wheel, images/provenance, origin receipts and retained audit generations. Raw authentication/debug evidence is not publication material.
- All **44 packaged source/assets files** match source, wheel and clean installation; all four mediator mirrors match. The three bundled workers and production Go sources are unchanged. **No worker rebuild is claimed.** `candidate-integrity.json` checks 175 baseline files and identifies unrelated changes separately.
- Initial preset attempts used the older routing wheel `65d3a97f…`; only `preset-final-*` results belong to this wheel. Diagnostic instrumentation is explicitly separate from final-wheel acceptance.

## Reference setup and host dependencies

Debian 13.7, CPython 3.12.16, Go 1.26.5 and QEMU 10.0.13. The host exposes 12 CPUs and 33,660,510,208 bytes RAM. Workloads use the approved hard **four-CPU cpuset (0–3), 8-GiB memory ceiling and zero swap**. There is no `cpu.max` interface: this is not a quota/throttling claim.

The reference launcher verifies UID/GID 1000, zero effective capabilities, constrained descendants/affinity, and a private device view without `/dev/kvm`; workload launches use TCG. A separate 64-MiB probe produced the expected real OOM group kill. Its two killed processes are not gateway OOM failures. The measured `run` subgroup's OOM counters are checked separately. The count-only privileged observer is outside the measured cgroup and never reads descriptor targets or contents. Count/identity packets are retained for the later resource runs.

Installed for real storage-fault testing, with apt plan/install logs and before/after inventories:

| Role | Exact host package/version |
|---|---|
| Direct | `dmsetup=2:1.02.205-2` |
| Automatic dependency | `libdevmapper1.02.1:amd64=2:1.02.205-2` |

**Two additions, zero upgrades/removals; only `dmsetup` became manual.** No distro upgrade/autoremove. The earlier GCC/libc installation remains separately documented in [the race report](rootless-network-gateway-go-race-validation-20261116.md). Go builds use native `/tmp` temporaries because writable mmap in the workspace is unavailable.

## Automated verification

- **1,132 fast tests, 92.21% coverage**; **146 installed mediator tests + 8 subtests**, with twenty installed mediator launches and no failures/errors/skips. JUnit counts the mediator cases/subtests as 154 events, not 154 independent tests.
- Fresh ordinary and race-enabled Go runs both pass **all seven packages / 105 test-subtest-seed events** with no skips or race reports. The confinement child intentionally remains pure Go rather than race-instrumented. Fresh pure-Go Darwin amd64/arm64 temporary cross-compiles also pass; their outputs were removed, packaged workers were not rebuilt, and neither check is native execution.
- **All seven fuzz targets ran for at least 60 seconds**, with actual fuzz executions retained in `test-summary.json`; seed execution is not substituted for fuzzing.
- **Six Linux integration cases pass**, including explicitly configured four-VM TCG capacity/peer-crash isolation, direct gateway integration, nondumpable observer checks and both TLS-origin regressions. `integration-runtime-proof.json` verifies installed application imports and the packaged worker. Earlier two-VM runs remain separately labeled.
- Whole-project Ruff lint, changed-file formatting and whitespace checks pass. Whole-tree formatting still flags only the three unchanged baseline files: `.github/scripts/generate_complexity_report.py`, `tests/test_check_image_build_inputs.py`, `tests/test_project_shell.py`.

## Minimal changes

- The SDK's automatic x86 TCG microVM machine reproduced a kernel TSC-calibration hang. Local-image startup now chooses the supported **q35** machine on Linux x86 without accessible KVM, preserving other platform/KVM paths. Four parameterized CLI regressions cover these choices. Direct TCG integration/benchmark fixtures likewise use q35, PCI virtio devices and compatible boot arguments. No artificial clock scaling, CPU-feature change or KVM fallback was used.
- The TLS benchmark origin accepts HTTP/2 with one I/O owner per connection, not 128 blocked origin threads. Three bursts require **128 distinct origin arrivals and at least 64 seconds shared overlap**. Guest deadlines cover those deliberate holds; production limits, TLS checks and keep-alive remain unchanged.
- Readiness now records twenty matched actual guest-network boots against SLIRP, alternating order and retaining raw durations; listener/process startup alone is insufficient. Future attempts checkpoint partial measurements.
- `task19_stability.py` now drives a real existing VM through the installed application, including HTTPS, 1-MiB transfer, incremental SSE, WebSocket, HTTP/2, populated Git, package install/import, ingress and worker replacement. It requires 600 seconds, bounded sampling gaps and strict missing-metric failure. Regression tests cover SSH newline normalization, the real status/private-state split, per-pair growth and matched quiescent identities. A replacement cannot hide growth behind a different pair's larger baseline.

No production dependency, policy semantics, audit schema, resource ceiling or confinement exception was added.

## Final-wheel presets

Each attempt uses a fresh prepared-Debian VM, non-root `agent`, public installed CLI, the existing installation deadline and explicit registry-only GET/HEAD policy. No account login, paid inference, older vendor pin, allow-all rule, TLS bypass or manual installer repair was used. Version success, account/HOME, sudoers and resolver checks are required separately from installer completion.

| Preset | Final-wheel result |
|---|---|
| Pi | **PASS** create and non-root version/inventory inspection: SDK-selected `@mariozechner/pi-coding-agent` **0.73.1** |
| Codex | **PASS** create and non-root version/inventory inspection: `@openai/codex` **0.169.0** |
| Claude | **FAIL** installer completes; version command exits 132 (`Illegal instruction`) |
| OpenCode | **FAIL** public provisioning does not complete within the existing deadline; inspect the retained native-package installation errors |

`preset-final-*-result.json` and corresponding inspection logs record versions and exact outcomes. All four request-audit logs and associated state/worker/mediator logs were archived **before VM removal**. The passing installer/version cases are not a claim that every lifecycle/persistence/negative-policy case in the full preset matrix has been completed.

## Reference performance

`performance/performance-raw.json`, its forty boot transcripts, origin start/end events and the retained `gate-performance-q35-executed.py` identify the executed harness. The successful completion of measurement is **not** a passing performance gate: **9 predicates pass, 2 fail**.

| Check | Observed | Disposition |
|---|---:|---|
| Twenty paired added guest-readiness samples, gate p95 ≤1 s | **4.5753 s** | **FAIL** |
| Idle pair RSS p95 ≤256 MiB; idle samples ≤512 MiB | **81,055,744 bytes** p95 | PASS for sampled idle scope |
| 100-MiB HTTPS ≥100 Mbit/s | **97.5466 Mbit/s** | **FAIL** |
| HTTPS ≥50% matched SLIRP | **70.2236%**, SLIRP **138.9086 Mbit/s** | PASS |
| Added established-request latency p95 ≤10 ms | **5.1966 ms** | PASS |
| Three origin-proven 128-request overlaps ≥64 s | **64.5017 / 64.5989 / 64.5985 s**, 128 unique starts/ends each | PASS |
| Upload/download backpressure | 8 MiB in each direction | PASS |

Readiness is the gate's conservative order statistic of paired gateway-minus-SLIRP boot-to-network durations, including guest boot/DHCP variation; it is not an isolated diagnosis of process-start overhead. Raw samples are retained. No threshold was changed after measurement and the result was not rounded into a pass.

A post-load pair snapshot measured **90,456,064 bytes RSS, 21 FDs, 2 processes and 19 threads**. These snapshots and idle samples do not establish ten-minute growth or a ceiling throughout every unsampled instant. The separate **four-pair functional response/peer-crash isolation case passes**: each pair has 21 FDs/two processes, with RSS **80,355,328–80,506,880 bytes** and 9–10 threads. `four-pair-result.json` retains the four-VM markers and counts; this is not sustained four-VM throughput/leak acceptance.

## Actual storage faults and stability

**PASS — real storage faults at the installed controller/mediator boundary:**

- An owned filled tmpfs returned **ENOSPC (`write`, errno 28)**. The request record exceeds remaining allocated-page slack so that the test actually needs storage allocation.
- An owned loop-file/ext4/device-mapper target switched to `error` returned **EIO (`fsync`, errno 5)**. No host disk or unrelated mapper was targeted.
- In both cases the sink failed, the gateway stopped forwarding and the post-fault origin execution count remained **zero**. Audits and the EIO backing image were retained before restoring/unmounting/removing owned storage. Initial fixture-permission/CA/slack failures remain identifiable.

These are genuine kernel I/O faults, **not** monkeypatched exceptions, but they are component-boundary tests, not a substitute for the entire actual-VM mid-forward/outcome/storage/recovery matrix.

**Actual-VM counterexample:** `closed-stability/` completed ten mixed iterations, including incremental SSE and populated Git/package work, then failed after approximately 90 seconds with controller `audit_failed`. The origin history, request-audit logs and associated state/worker/mediator logs were retained. No replay/direct fallback was introduced. The underlying audit failure has not yet been reproduced with a captured cause; it remains an availability investigation, not a cleared gate.

The public quick tunnel buffered SSE even on a direct host request outside the gateway. A closed network namespace therefore hosts a synthetic TLS origin on a public-shaped address: only loopback is usable, there is no external route, and the fixture CA is trusted only in the private mount view. TLS/hostname verification and normal production destination validation remain enabled. This is a synthetic-origin fixture, not a claim of public Internet reachability. The unmodified VM run uses the final installed application; test instrumentation runs are separately identified.

The explicitly non-acceptance audit-instrumented run completed **600.54 seconds / 75 mixed iterations**, with worker replacement, but failed its original thread-growth check. Both process pairs show count growth in retained samples; this is not a passing resource result. It did not reproduce the initial audit error. Its patched controller/mediator identities are recorded in `audit-diagnostic-identity.json` and are **not the final installed artifact**.

**Final uninstrumented run: FAIL — resource growth.** `closed-final-stability/` used the unchanged installed application for **605.96 seconds / 92 complete mixed iterations**, with genuine worker replacement, unchanged guest boot identity and **296 resource samples**. Maximum sample gap was **3.1761 seconds**, with no missing-metric/monitor failures. Every sampled pair remained below the ceilings: peak **95,154,176 bytes RSS / 28 FDs / 2 processes / 19 threads**.

The gate's per-pair post-warmup comparisons nevertheless found **5,611,520 / 5,320,704 bytes RSS growth** (allowance 4 MiB); the first pair also gained six threads (allowance zero). These counts require investigation; they are not by themselves a diagnosis of an unbounded leak. The run failed before the final quiescent comparison, so matched quiescent resource acceptance remains incomplete. The harness now retains that final checkpoint before evaluating growth on future attempts; the exact executed snapshot/hash is preserved as `stability-final-executed.py`. No threshold, production resource limit or source artifact was changed to manufacture a pass.

The two long runs did not reproduce the initial audit failure; that earlier counterexample remains open rather than being erased by later functional success.

**Archive limitation:** selected request-audit/state/log files were retained, not complete state-directory snapshots. DNS journals/runtime confdirs were not included by the original archive filter and cannot now be claimed as preserved. The private preset helper was corrected for future attempts. This does not discard the retained request-audit records, but limits DNS/recovery forensics and is another reason not to claim the whole final fault/recovery matrix complete.

## Remaining dispositions

- Readiness and absolute HTTPS throughput remain failed; do not reinstate historical performance waivers or weaken isolation/thresholds.
- Claude/OpenCode and the remaining complete preset matrix remain unaccepted.
- Final-wheel resource-growth acceptance fails and the earlier audit counterexample remains unexplained. Unexecuted final operator/protocol/fault/storage/recovery cases remain **BLOCKED — no final-wheel actual-VM evidence**; unit/component tests do not substitute for them. In particular, the complete preset lifecycle/negative-policy matrix, hash-verified 100-MiB transfer and actual-VM audit/bootstrap/broker/supervisor/storage-fault windows were not completed.
- The recorded Tornado compatibility/advisory conflict and secure wheel-only Intel Cryptography distribution blocker remain open; this campaign does not freshly clear dependency security.
- Native Darwin amd64/arm64 execution is **DEFERRED by the user to another validator after merge, not PASS**. The handoff must use an exact wheel hash, locked wheel-only dependencies, packaged worker hashes and native lifecycle/confinement/protocol/recovery/resource tests. The Intel wheel/security blockers are not waived by that timing decision.
- Licensing (#7) is excluded and handled separately, not resolved. Default guest-kernel Landlock remains a separate image issue.

## Cleanup and verification

Owned VMs are removed and the isolated public VM inventory is empty. Synthetic origins/tunnel/namespace holder, storage mounts/mapper/loop resources, reference cgroup and root-owned observer staging are gone; owned cgroup controllers are restored. The measured `run` subgroup has **zero OOM events/kills**; the separate probe accounts for the hierarchy's intentional OOM counters. Host setup records, **614 count-only observer packets**, synthetic fixture material and a sparse test-image archive were retained before deleting `/tmp/sbx-gwc`. The prior `/tmp/sbx-gwb/images/sbx` image and unrelated resources were not removed. The two documented OS packages remain installed.

`cleanup-host.json`, `cleanup-scratch.json`, `candidate-integrity.json`, install/package inventories and the command ledger record these checks. No commits, pushes or merges occurred; no passing overall acceptance claim is made.
