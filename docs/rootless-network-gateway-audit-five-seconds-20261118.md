# Five-second audit deadline — early-adoption decision

The user approved increasing the audit transaction deadline **from one to five seconds** on 2026-11-18, prioritizing practical use and early-adopter reports. This supersedes the earlier campaign's no-timeout-increase constraint, not its recorded results.

## What failed and what changes

An allowed HTTP request must have a durable admission record **before its headers/body reach the destination**. The supervisor appends, flushes and syncs that record, then acknowledges it to the mediator. An asynchronous outcome record follows completion; missing outcomes remain execution-unknown, never permission to replay.

The captured failure spent **2.626 seconds in the sync stage**, beyond the former one-second budget. The mediator stopped; the controller later encountered a broken ACK pipe. EPIPE was a consequence, not evidence of the original disk error. Timing includes possible scheduling delays; the underlying source of the pause and its relation to the older failure remain unproven.

The same 2.63-second pause now fits the **five-second end-to-end audit budget**, including lock wait, pipe delivery and durable acknowledgement. It is not five seconds per stage. The controller's bounded ACK-write wait is also five seconds. Normal fast requests do not acquire an artificial five-second delay.

Durability is unchanged: no early ACK, audit dropping, batching, new daemon, unlimited wait or replay. Storage/transport errors still fail closed immediately; a transaction exceeding five seconds still stops mediation. Audit events remain capped at1MiB, ACKs at4KiB and existing queue/log budgets remain. A slow transaction can pause other mediated traffic while waiting; the larger budget trades a longer bounded pause for fewer unnecessary shutdowns. It does not make unreliable storage reliable.

## Validation and artifact

- New real mediator/controller/TLS-origin regression holds admission sync for **2.65 seconds**: no HTTP reaches the origin during the pause, then HTTP200/exactly one origin execution. It fails under the old deadline and passes under five seconds.
- ACKs at2.626s/4.999s succeed; an ACK becoming readable at5.001s still fails. Existing real timeout, disk-error, malformed-ACK, streaming, HTTP/2 and no-replay tests pass.
- **1191 fast tests /92.19%**, source and clean-installed mediator **162+8 subtests**, Ruff/affected formatting and whitespace checks pass.
- New wheel SHA256 **`6b389e564953c5654632be0c0a288db47c73efb01bf45e6004e7235465260e62`**. All44source/wheel/installed assets and four mediator mirrors match; all three Go workers remain byte-identical. No new dependencies, OS packages or privileged setup.
- Private evidence: `local/gateway-validation/20261118T145304Z-audit-five-seconds/`. Clean native installation's4377regular files were archived/member-hash verified before owned temporary-prefix removal; original-prefix restoration or locked reinstall is required for entrypoints.

This is targeted installed component/protocol validation, **not a fresh actual-VM long soak, performance or native-macOS result**. The earlier wheel's thread/readiness/audit results remain historical; neither full release acceptance nor underlying slow-sync repair is claimed. No commits/pushes/merges.

## Useful early-adopter reports

Use `sbx network diagnostics NAME` and include the sbx version/wheel hash, OS, state-storage/filesystem type, affected protocol/workload/concurrency, observed pause/error and a small reproducer. For audit failures include the redacted operation/reason/errno/stage timings, and whether unrelated streams also paused. Prioritize actual outages, wrong policy behavior, sustained/unbounded growth and reproducible slow paths over treating a small stable thread increase or a missed startup target as broken functionality.

Do not post raw audit logs, state directories, URLs containing tokens, credentials or private CA keys without private review. No automatic telemetry or new reporting service was added.
