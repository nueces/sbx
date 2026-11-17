# Local gateway blocker follow-up — 2026-11-17

**CAMPAIGN COMPLETE — NOT MERGE READY.** Concrete fixes and expanded validation are complete, but resource growth, the historical audit cause and remaining live-matrix gaps are unresolved. No commits, pushes or merges were made. Third-party dependency/distribution security, licensing and native macOS execution are not cleared here. A new long-lived launcher/forkserver requires separate approval; other work continues independently.

## Baseline and evidence

- Implementation baseline: `20e550f`, preserving unrelated untracked files and specification work.
- Private campaign: `local/gateway-validation/20261117T181507Z-local-blockers/`; raw synthetic authentication/debug/state evidence stays private.
- Historical application wheel: `bf0c5c8853b101efd5e65b9b38bfb34e2a7ea03d7b754c610d58a8e84e10514d`.
- First provisional wheel: `84fdf390763466d24dddbab72ad491f1979adc24689f3b04bd0e92af245f4b1d`, prefix `/tmp/sbx-gwe/candidate`; its results below remain artifact-specific.
- Latest wheel: `cbd34cfa9aabed6a2cc2aa80578ab346981a1478194f4c863d7f92be1b8873f0`, prefix `/tmp/sbx-gwe/candidate-binding`. It additionally rechecks the credential-bound context and header after an await; two new scope-mutation regressions fail before that guard. Its full performance and seven Linux integration reruns pass; both resource runs fail. Extended live cases are scoped below. The native prefix was removed only after its complete installation archive was verified.
- Reference constraints: CPUs 0–3, 8-GiB memory, zero swap, q35/TCG `max`, private no-KVM device view, UID/GID 1000 and zero effective capabilities. No CPU-quota claim. Affinity escape is denied; a separate 64-MiB subgroup produced its expected OOM kill, separate from measured workload counters.

The unchanged baseline wheel reproduces the performance failure: added readiness p95 **6.4130 seconds**, while HTTPS **249.61 Mbit/s**, **59.66%** of matched SLIRP and added established latency **7.3451 ms** pass their targets. The native application install experiment is a separate condition, not a retroactive pass for this workspace installation.

The unchanged real-VM baseline completes **604.14 seconds / 90 mixed iterations / 296 samples** and fails RSS growth. Original failures and complete regular state generations/guest logs are retained. One initial create failed before VM startup because the deliberately minimal host PATH omitted existing `e2fsck`/`resize2fs`; adding `/usr/sbin:/sbin` to the private runner PATH required no package installation. The empty VM inventory was checked before retrying.

## Reproduced findings and minimal changes

### Audit availability diagnostics

The historical approximately 90-second `audit_failed` counterexample remains unresolved unless its cause is captured. Its retained audit contains 247 records and is far below byte/rate limits; its final record is an admission, with no corresponding recorded outcome. That narrows investigation but does not establish the failure cause.

The controller now reports the failing audit I/O operation and errno without exception payloads. The mediator reports a bounded operation/category/errno/elapsed diagnostic without logging record contents. Existing `controller/audit_failed`, one-second transaction limit, append/flush/sync-before-ack and no-forward/no-replay guarantees remain unchanged. Regressions first fail without operation diagnostics, then pass with them. Diagnostic copies and heap/timing runs are explicitly non-acceptance evidence.

### Per-request event loops and blocking providers

The production `_load()` still applied mitmproxy's `concurrent` wrapper to an already-async `requestheaders`. The installed decorator creates a new event loop for every request. The diagnostic run exposes separate executor pools; a permanent loader regression fails because destination resolution runs on a different loop from the master.

Remove that obsolete wrapper so the async hook uses the native master loop. A separate reproduced regression shows that synchronous credential fetching would then block that loop. Offload only the blocking broker call, bound outstanding fetch tasks by the existing request budget, preserve their reservation until work really ends, and skip queued cancelled work. Revalidate logical authorization, credential-bound context and the expected header after awaiting and before injecting any secret. Tests cover loop responsiveness, cancellation capacity, disconnect/retarget/cancel without injection, and existing admission/error cleanup. Public interfaces, policy semantics, wire schemas and dependency versions are unchanged.

These regressions establish concrete defects and fixes. They do **not** establish passing RSS/thread growth: both completed uninstrumented final-wheel runs still fail.

### Filesystem-dependent startup

Alternating executions of the identical mitmproxy distribution take approximately **3.0 seconds** from the workspace filesystem versus **0.32 seconds** from a native local installation after its first run (**0.92 seconds**). Import profiling identifies roughly **2.63 seconds** in the workspace import chain. Both sets of samples are retained; no old sample is discarded.

The final performance rerun uses a clean, hash-verified native installation, with its storage condition explicit. The full paired guest-readiness definition and one-second target remain unchanged. No warm service, clock/profile change, early readiness announcement or substituted listener-only metric is introduced.

## Acceptance disposition

| Workstream | Current disposition |
|---|---|
| Audit availability | Historical cause not captured in two completed diagnostic runs; remains unresolved, not cleared. |
| Resource growth | FAIL on both latest-wheel uninstrumented runs; valid loop/provider fixes do not clear RSS/thread growth. |
| Readiness | Latest candidate PASS on native local storage: added p95 **0.525 seconds**. Workspace-install failure remains separate, not a universal pass. |
| Missing actual-VM matrix | Latest-wheel large-transfer, policy, crash/recovery, provider and operator/private-bootstrap subprocess cases pass as scoped below. All four preset lifecycles and three actual-VM kernel storage-fault cases also pass; the broader live negative matrix remains incomplete, not waived. |

The latest candidate's performance gate passes all **11 predicates**: HTTPS **295.50 Mbit/s**, **89.03%** of matched SLIRP, added established latency p95 **6.3104 ms**, idle pair RSS p95 **87,736,320 bytes**, and three origin-proven 128-request overlaps of at least 64 seconds. The earlier candidate also passed, with its separate numbers retained. Seven installed-application Linux integrations, explicitly including four-VM isolation, also pass. These results do not clear resource growth or the unexplained audit failure.

First provisional-wheel (`84fdf390…`) uninstrumented run: **606.15 seconds / 91 mixed iterations / 298 samples**, real replacement and unchanged guest boot; **FAIL `post-warmup growth: rss_bytes`**. Recovered warm-to-final idle RSS grows **8,024,064 bytes** and two threads. Both worker and mediator show thread growth in count-only observations; no forced collection, thread preallocation, altered warmup or allowance change is introduced to conceal it. Removing the obsolete request-loop wrapper fixes a demonstrated defect, not the entire resource blocker.

Latest-wheel resource runs complete **602.70 / 603.95 seconds**, **90 / 90 mixed iterations**, **296 / 297 samples**, maximum gaps **2.063 / 2.368 seconds**, real replacement and unchanged guest boots, with no unavailable-metric/monitor failures. Both fail RSS growth: recovered-pair quarter-to-final increases **4,845,568 / 5,730,304 bytes** against 4 MiB, and first/recovered pairs also gain threads against zero. Recovered matched-idle RSS increases **6,836,224 / 8,114,176 bytes**. All sampled absolute ceilings pass (maximum **101,965,824 bytes RSS / 26 FDs / 2 processes / 15 threads**). These failed targets are not proof of an unbounded leak and are not waived.

Latest-wheel live checks now include hash-verified **100-MiB upload and download**, path/placeholder/authority/redirect denials, durable admission followed by kill before origin execution (zero executions; raw first-HTTP-byte boundary not captured), active POST worker/mediator/supervisor loss (exactly one original execution, no replay), authenticated pair recovery/DNS journal retention, and explicit manual stop/run after supervisor death. Provider error/hang cases return bounded failures while a different credential succeeds; killing a broker peer with a pending request causes zero origin executions and fresh-pair recovery. Three public configuration-rejection cases leave the running VM unchanged. Four malformed private-bootstrap cases use real installed supervisor subprocesses alongside that VM and fail before ownership; they are not described as mid-owned-VM bootstrap injection.

Automated checks: **1,166 fast tests / 92.26% coverage**, **156 installed mediator cases plus 8 subtests**, seven Linux integration cases including four-VM isolation, and ordinary/race Go **7 packages / 105 identical passing events**, zero skips/races. Three packaged workers, production Go and dependencies are unchanged; no worker rebuild is claimed.

## Presets and kernel storage faults

Fresh public creates, runtime-user versions, stop/run persistence, reprovisioning, restrictive-policy denial and archive/removal pass for **Claude 2.1.326, OpenCode 1.19.6, Pi 0.73.1 and Codex 0.169.0** on this wheel. Create times are **80.289 / 117.295 / 160.873 / 88.043 seconds**, respectively. Claude/OpenCode additionally reject a broken existing command without overwriting it. All versions run UID 1000; no login, inference or real credentials are used. Sixteen regular state generations and their sixteen DNS journals are retained across these four lifecycle cases.

Three actual-VM tests use real owned kernel fault storage with unmodified installed application code:

- **ENOSPC before admission:** filled private tmpfs, write errno 28, zero origin executions, fail closed.
- **EIO before admission:** owned loop/ext4/device-mapper error target, zero origin executions, fail closed. An independent fsync of the actual audit inode returns errno 5; this is not mislabeled as a captured controller errno.
- **ENOSPC at outcome:** exactly one original origin execution, no replay after durable outcome storage becomes unavailable, explicit unknown outcome/fail-closed evidence.

Application workloads remain unprivileged; root is limited to owned fault setup/restoration/observation/archive. Full active/restored state and guest logs are retained before removal. Each owned volume, loop and mapper is removed after archival.

## Remaining scope and cleanup

`case-ledger.json` and `results.json` retain PASS/FAIL/UNRESOLVED/NOT_RUN boundaries. In particular, actual-VM broker overload, mid-owned-VM malformed bootstrap/transport injection, the raw post-ack/pre-first-HTTP-byte boundary, adversarial DNS-journal recovery and the remaining full live protocol/identity negative matrix are **not** replaced by automated/component coverage. This follow-up therefore expands rather than declares the complete acceptance matrix closed.

A final **non-acceptance 180-second** heap/allocator diagnostic finds native allocator growth substantially exceeding traced live Python allocation growth (allocator in-use +7,648,048 bytes versus traced +990,220 bytes between sampled endpoints). That narrows further investigation but does not prove the retaining owner, unbounded leakage or an acceptable plateau. No forced GC/trimming, enlarged warmup or thread allowance is used.

Source, wheel and installed/archive parity checks cover all **44 assets**, four mediator mirrors and unchanged Go/three-worker identities. The exact native installation is preserved as `installed-native-final.tar.gz`, SHA-256 **`8dcee08cad87e3553896dc8505bf9000bd346579d87b103e0c60d3c233bfa734`**, with **4,467 regular files** verified against its live prefix before removal. Restoring its original prefix or cleanly reinstalling the wheel/locked dependencies is required to use its entrypoints. All retained guest gzip/tar contents verify; ten early archives received their complete gzip/member checksum verification after VM removal, a verification-timing limitation rather than missing retained bytes. Later exports gate removal on that verification.

Final VM inventory is empty. Owned fixture services/namespace keepers, cgroup, staging, scratch directories, loops and mappers are gone; controllers are restored. **2,267 count-only observer packets** are retained. Workload OOM counters are zero; the separate expected probe accounts for the hierarchy's one OOM event/two kills. Hierarchy peak memory is **4,432,416,768 bytes**. OS package/manual selections are unchanged; prior prepared image and archived test image remain untouched. No missing measurement or later successful run erases an earlier unexplained failure.
