# Gateway development

Use Go 1.26.5 with `GOTOOLCHAIN=local`; mediator dependencies come from the root uv lock.
Linux is the tested runtime. Darwin amd64/arm64 are build and wheel-resolution checks only;
gateway runtime confinement on macOS is explicitly unverified and weaker than Linux.

The worker and QEMU resolve from explicit `SBX_GATEWAY_BIN` and `SBX_QEMU_BIN`
paths before portable installation/PATH candidates. The mediator uses the installed
`sbx` interpreter and packaged launcher; `mitmdump` resolves from the initiating CLI's
normal `PATH` ordering, which the supervisor retains without other environment values.
No gateway path uses sudo, capabilities, setuid helpers, TAP, or host firewall changes.

On Linux without accessible KVM, gateway-backed x86 guests use QEMU's supported
TCG `max` CPU, not legacy `qemu64`: current Claude/OpenCode native binaries can
otherwise fail with SIGILL. Automatic x86 machine selection uses q35 so stopped
VMs do not intermittently lose timer calibration on restart; saved configurations
are not rewritten. Explicit machine selection is preserved; ARM, KVM and
non-gateway argv are otherwise unchanged. Record the CPU profile when comparing
performance. Record the installed runtime's filesystem too: the validation
workspace's shared filesystem adds seconds of Python import latency. The strict
readiness gate is measured with a clean wheel installation on native local storage,
not by hiding import time or using a warm process. Workspace-install failures remain
separate evidence; this is not a universal filesystem-independent startup guarantee.

Claude/OpenCode provisioning also requires a successful, bounded 15-second
`--version` check as the configured guest user, including for an existing command.
A broken existing binary fails clearly rather than being silently reinstalled.
The SDK's Claude preset/configuration handling and official npm installers remain
in use. No login or inference is part of this check.

The worker requires an inherited `--dns-state-fd` for a private append-only generation
journal. The controller binds its header to VM/generation/policy digest; workers restore
cached identities and register wildcard addresses before reading guest traffic. Mapping
publication follows append/sync, and file locking excludes overlapping workers. Tests
must pass the journal descriptor too; there is no empty-state recovery fallback. The
configured DNS ceiling refuses new mappings rather than recycling guest-cached IPs.

QEMU framing deadlines bound partial headers/payloads and blocked writes, not ordinary
between-frame inactivity. Fast framing tests use short transfer deadlines rather than
waiting ten minutes to exercise this behavior.

Linux workers must be built with `CGO_ENABLED=0`: `no_new_privs` and Landlock are
applied using Go's all-thread syscall barrier, covering existing OS threads and
future threads through inheritance. A cgo-linked worker fails startup instead of
claiming process-wide confinement. Kernels without Landlock still report it unavailable.
After QEMU connects, the worker probes the guest with ARP so idle guests can complete
replacement readiness without relaxing the authenticated-frame requirement.

The mediator bounds glibc arena reservations before exec to preserve thread capacity
under its unchanged 512 MiB address-space limit. Its 300-second TCP inactivity timeout
matches the worker's HTTP idle limit; it is not a connection-establishment timeout.

SOCKS capability version 2 places the logical hostname in the username and signed
flow/rule/time/nonce/port metadata in the password. Both fields fit RFC 1929's
255-byte limit, including the largest accepted DNS name and rule ID. VM and worker
identities remain bound into the signature without being repeated on the wire.

Preset creation captures the original host provider environment before sanitizing
guest provisioning. Only baseline HOME/PATH/TZ and explicitly inherited variables
reach the supervisor through its private provider descriptor; guest sanitization stays unchanged.

## Actual-VM stability validation

From the implementation root, use the installed wheel's interpreter and an already
running TCG VM with the prepared Debian tools and non-root `agent` account:

```sh
"$INSTALLED_PYTHON" gateway/scripts/task19_stability.py \
  --vm "$VM_NAME" --origin "$SYNTHETIC_HTTPS_ORIGIN" \
  --git-commit "$FIXTURE_COMMIT" --gateway-root "$PRIVATE_GATEWAY_STATE" \
  --ingress-port "$HOST_PORT_FOR_GUEST_3000" --artifacts "$FRESH_PRIVATE_DIRECTORY"
```

The synthetic origin must implement the endpoints and fake command/CodeArtifact
credentials in `GUEST_WORKLOAD`; the guest receives only `GWC_TOKEN`/`GWC_AWS`
opaque placeholders. Use a populated Git fixture, an installable `gwv-package==0.1`,
incremental SSE and WebSocket echo, never real accounts. Quick public tunnels may
buffer SSE: verify origin pacing independently before attributing it to the gateway.
Keep TLS verification enabled and isolate any test CA trust configuration.

The harness requires 600 real seconds, samples at most ten seconds apart, real guest
work, host ingress and one worker replacement. It compares warm/quiescent usage
within each process pair; replacement must not hide a leak. Nondumpable processes
require the separately staged count-only observer described in `docs/development.md`
(from the repository root). Missing metrics, failed work or short runs fail acceptance.

## Toolchain checks

```sh
GOTOOLCHAIN=local go mod verify
GOTOOLCHAIN=local go test ./...
CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 GOTOOLCHAIN=local go build ./cmd/sbx-gateway
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 GOTOOLCHAIN=local go build ./cmd/sbx-gateway
uv sync --directory .. --locked --no-build --python 3.12
uv audit --directory .. --locked
uv run --directory .. --locked python -c \
  'from importlib.metadata import distributions; print("\\n".join(sorted("{}=={}: {}".format(d.metadata["Name"], d.version, d.metadata.get("License-Expression") or d.metadata.get("License") or "UNKNOWN") for d in distributions())))'
```
