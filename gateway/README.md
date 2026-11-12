# Gateway development

Use Go 1.26.5 with `GOTOOLCHAIN=local`; mediator dependencies come from the root uv lock.
Linux is the tested runtime. Darwin amd64/arm64 are build and wheel-resolution checks only;
gateway runtime confinement on macOS is explicitly unverified and weaker than Linux.

The worker and QEMU resolve from explicit `SBX_GATEWAY_BIN` and `SBX_QEMU_BIN`
paths before portable installation/PATH candidates. The mediator uses the installed
`sbx` interpreter and packaged launcher; `mitmdump` resolves from the initiating CLI's
normal `PATH` ordering, which the supervisor retains without other environment values.
No gateway path uses sudo, capabilities, setuid helpers, TAP, or host firewall changes.

The worker requires an inherited `--dns-state-fd` for a private append-only generation
journal. The controller binds its header to VM/generation/policy digest; workers restore
cached identities and register wildcard addresses before reading guest traffic. Mapping
publication follows append/sync, and file locking excludes overlapping workers. Tests
must pass the journal descriptor too; there is no empty-state recovery fallback. The
configured DNS ceiling refuses new mappings rather than recycling guest-cached IPs.

QEMU framing deadlines bound partial headers/payloads and blocked writes, not ordinary
between-frame inactivity. Fast framing tests use short transfer deadlines rather than
waiting ten minutes to exercise this behavior.

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
