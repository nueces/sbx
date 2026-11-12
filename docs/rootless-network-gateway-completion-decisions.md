# Rootless network gateway completion decisions

## Task 33

- **Supervisor executable:** launch `sys.executable -m sbx.gateway_supervisor` through
  `subprocess.Popen(start_new_session=True)`. Rationale: this is a fresh packaged subprocess and
  reuses the installed Python/runtime without `fork`-only setup. Consequence: the supervisor entry
  point and module are shipped in the wheel.
- **Ownership lock location:** keep `ownership.lock` at the per-VM directory level, outside any
  generation. Rationale: one lock serializes generation creation and survives generation-directory
  cleanup. Consequence: an empty per-VM directory and lock file may remain after normal shutdown.
- **Inherited startup payloads:** use anonymous `TemporaryFile` descriptors for non-secret metadata
  and the separate static mediator payload. Rationale: unlike a pipe, this cannot block before the
  child starts reading and remains bounded and absent from argv, environment, and named state.
  Consequence: Task 32 can remove only the static-secret descriptor without changing bootstrap.
- **Control and broker framing:** use a four-byte network-order length followed by compact JSON,
  capped at 64 KiB. Rationale: the standard library provides deterministic bounded framing and
  schema inspection. Consequence: protocol changes require a version increment; Task 33 implements
  only broker envelope/correlation validation, not credential retrieval.
- **Control concurrency:** process one request at a time in the per-VM supervisor. Rationale: this
  directly serializes mutating lifecycle operations and status requests are short. Consequence:
  provider concurrency belongs to Task 32 rather than the lifecycle control socket.
- **Static secret retention:** resolve current `env:`/`file:` references once in the initiating CLI
  and retain the resulting mediator payload only in supervisor memory. Rationale: replacement must
  work after the CLI exits without persisting values. Consequence: source changes are intentionally
  ignored until stop/start; Task 32 replaces this path atomically.
- **Lifetime handling:** pass one supervisor-owned pipe to each worker/mediator pair; both children
  exit when its write end closes. Rationale: descriptor EOF is portable and does not trust a parent
  PID. Consequence: pair replacement creates a fresh lifetime pipe and drops all old flows.
- **Linux pidfd compatibility:** prefer Python's pidfd APIs and fall back to the Linux
  `pidfd_open(2)`/`pidfd_send_signal(2)` syscall numbers when the interpreter omits wrappers.
  Rationale: supported Linux architectures use the generic syscall numbers and PID reuse must not
  reintroduce check-then-signal. Consequence: kernels returning `EINVAL`/`ENOSYS` refuse forced
  cleanup rather than signal unsafely.
- **Darwin orphan policy:** normal authenticated control/lifetime cleanup works, but dead-supervisor
  orphan signaling always refuses. Rationale: there is no `/proc` generation identity to validate.
  Consequence: the diagnostic requires manual cleanup on this explicitly unverified path.
- **Drift comparison:** compare the requested canonical digest using the live generation returned by
  the authenticated supervisor, excluding controller-owned ingress such as the reserved SSH route.
  Rationale: generation is part of the digest, while internal listeners are not user configuration.
  Consequence: status reports real drift without rejecting an unchanged configuration after startup.
- **Mediator executable lookup:** preserve the initiating CLI's `PATH` as the supervisor's only
  additional environment input and resolve `mitmdump` with normal `PATH` ordering. Rationale: Task
  28 explicitly permits users to select a compatible installation this way, while all secret and
  capability values remain descriptor-only. Consequence: mediator replacement keeps the original
  executable selection after the initiating CLI exits.
- **Reserved broker transport:** create a fresh private socket pair for each mediator incarnation,
  retain one endpoint in the supervisor, and inherit the other into the mediator. Rationale: Task 33
  establishes replacement-safe transport without implementing Task 32 providers. Consequence: pair
  replacement closes the old broker channel and Task 32 can attach its bounded protocol directly.
- **Supervisor identity after recovery:** include the persistent supervisor PID in every replacement
  state update, not only the initial commit. Rationale: authenticated recovery must not erase the PID
  needed by diagnostics and dead-supervisor pidfd cleanup. Consequence: status continues to report the
  supervisor and fallback cleanup remains possible after any number of pair replacements.

## Task 32

- **One schema cutover:** accept only `[[network.policy]]` and `[[network.credential]]`; do not translate unreleased HTTP/TLS/TCP/secret forms. Rationale: one authorization model avoids precedence and migration ambiguity.
- **Provider boundary:** run command and entry-point operations through `sbx.gateway_provider_helper` in a fresh process group with a supervisor-lifetime descriptor. Rationale: the supervisor can bound and terminate trusted same-UID provider trees without weakening mediator hardening.
- **Shipped method:** expose only `plugin:aws-codeartifact` from the sbx distribution. It describes Basic authorization metadata separately from fetching AWS CLI JSON containing token expiry.
- **Cache ownership:** keep per-credential locks, expiry-aware values, and the global provider limit solely in the persistent supervisor. A still-valid cached value survives transient refresh failure; expired values fail closed.
- **Provider bounds:** allow eight concurrent provider helpers and 128 concurrent broker requests,
  bound helper execution to 10 seconds and combined output/results to 64 KiB, remember the most
  recent 4,096 request IDs for replay rejection, and refresh expiring values 60 seconds early.
  Rationale: these fixed per-VM ceilings support unrelated concurrency without unbounded memory or
  process growth. Consequence: capacity exhaustion and values with no valid fallback fail closed.
- **Mediator transport:** use the Task 33 private per-incarnation socket with version, unique request ID, incarnation, and exact response correlation. Provider requests execute independently in broker threads.
- **Opaque guest identity:** generate `secrets.token_urlsafe(32)` once per bound credential and gateway generation. Persist only the non-secret guest placeholder and a guest-managed name manifest; remove stale names on the next start.
- **Header-only substitution:** resolve plugin defaults before data-plane readiness and send only descriptors/placeholders to the mediator. Literal templates and Basic passwords require one exact header instance; values elsewhere are not searched or rewritten.
- **PATH compatibility:** preserve the initiating CLI PATH both for Task 28 mediator selection and provider executable lookup; no other host values are inherited unless explicitly named.
- **Invocation-time environment collisions:** validate resolved `--env` values against credential
  guest-environment names in `cmd_start`, in addition to validating configured `[sbx].env` during
  schema parsing. Rationale: CLI precedence must not let a real forwarded value replace an opaque
  placeholder. Consequence: a collision fails before VM mutation.
- **Mixed placeholder ownership:** authorize every known placeholder occurrence by its exact matching
  header and binding, even when another credential legitimately matches the same request. Rationale:
  the presence of one authorized credential must not let a sibling placeholder escape through another
  inspected header. Consequence: any unmatched known placeholder denies the complete request.
- **Provider process-group completion:** after every helper result, kill any remaining process-group
  members before releasing broker capacity, including on apparent success. Rationale: a trusted helper
  may accidentally leave descendants behind. Consequence: provider execution has no background
  lifetime beyond one bounded describe/fetch operation.

## Mitmweb integration scope

- **Secure integration, not UI ownership:** use the upstream mitmweb UI without requiring custom presentation changes. Authentication, loopback-only binding, sanitized metadata, read-only enforcement, bounded resource use and mediator lifecycle are our responsibility. Custom retention warnings, eviction counters and UI redesign are out of scope and are not completion blockers.
- **Display history is not the audit log:** the current 256-event / 8 MiB snapshot window is a resource bound, not complete session retention. Persistent audit logging remains separate and follows the admission guarantee below.

## Audit admission guarantee — implemented; release validation still blocked

- **Durable admission before forwarding:** after policy evaluation and request preparation, write an `admitted` record with a unique request ID and wait for durable-storage acknowledgement before forwarding a mediated HTTP request. Supervisor receipt or a buffered write is insufficient; admission transport, storage or acknowledgement failure/timeout blocks forwarding and stops mediation.
- **Correlated outcome:** record the terminal outcome with the same request ID. Admission without an outcome means execution is unknown, not success, failure or permission to replay. This is not exactly-once execution or guaranteed final-outcome logging; TLS passthrough and non-HTTP traffic remain outside this HTTP audit guarantee.
- **Acceptance:** test forwarding order, durable acknowledgement, concurrent correlation, transport/disk failures, acknowledgement timeouts and crash/restart interpretation. Review byte/rate budgets for both records and measure persistence latency. The implementation now uses a private reverse acknowledgement pipe, serialized one-second admission transactions, schema-v2 admission/outcome records and supervisor-owned durable sync before acknowledgement. First-admission directory ancestry is synced; Darwin also requests full sync. Regression tests cover ordering, streaming, HTTP/2, concurrency, transport/sync failures, malformed acknowledgements and crash semantics. Final TCG/native-macOS and performance acceptance remains incomplete; see the status draft.

## Operator completion

- **Rootless SSH key location:** pass the actual user's `~/.smolvm/keys` directory explicitly to
  SmolVM key provisioning and remove stale `SUDO_*` identity metadata from non-root preset
  environments. Rationale: inherited `SUDO_USER` metadata must not redirect a rootless invocation to
  `/root/.smolvm`. Consequence: clean-installed local-image and preset startup work without trusting
  unrelated sudo environment variables.
- **Unix socket runtime layout:** keep persistent gateway state under the user state directory and put
  `qemu-net.sock` and `control.sock` in a short owner-only per-generation runtime directory. Validate
  the selected endpoint paths against the shared portable 103-byte limit without coupling them to the
  VM name or persistent state depth.
- **Multi-VM required gate:** run two isolated VMs by default and retain four VMs behind
  `SBX_GATEWAY_VM_COUNT=4`. Rationale: the design's recorded four-VM TCG performance waiver remains in
  force, while two VMs exercise process, socket, CA, ingress, and failure isolation reliably.
  Consequence: CI keeps a mandatory multi-VM gate without representing four-VM capacity as passed.
