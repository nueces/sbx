# Gateway development

Use Go 1.26.5 with `GOTOOLCHAIN=local` and uv 0.11.30 with CPython 3.12.13.
Linux is the tested runtime. Darwin amd64/arm64 are build and wheel-resolution checks only;
gateway runtime confinement on macOS is explicitly unverified and weaker than Linux.

Runtime tools resolve from explicit `SBX_GATEWAY_BIN`, `SBX_GATEWAY_MEDIATOR_PYTHON`,
`SBX_GATEWAY_MEDIATOR_LAUNCHER`, `SBX_QEMU_BIN`, and `SBX_UV_BIN` paths before portable
installation/PATH candidates. Overrides must be absolute, non-writable regular artifacts
(and executable when they name a runnable tool).
No gateway path uses sudo, capabilities, setuid helpers, TAP, or host firewall changes.

```sh
GOTOOLCHAIN=local go mod verify
GOTOOLCHAIN=local go test ./...
CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 GOTOOLCHAIN=local go build ./cmd/sbx-gateway
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 GOTOOLCHAIN=local go build ./cmd/sbx-gateway
uvx --from uv==0.11.30 uv sync --directory mediator --locked --no-build --python 3.12.13
uvx --from uv==0.11.30 uv audit --directory mediator --locked
uvx --from uv==0.11.30 uv run --directory mediator --locked python -c \
  'from importlib.metadata import distributions; print("\\n".join(sorted("{}=={}: {}".format(d.metadata["Name"], d.version, d.metadata.get("License-Expression") or d.metadata.get("License") or "UNKNOWN") for d in distributions())))'
```
