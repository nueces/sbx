# Dependency upgrades

Rootless gateway runtime pins are release inputs, not loose ranges:

- Go worker: Go 1.26.5 with `GOTOOLCHAIN=local` and the committed `gateway/go.mod`/`go.sum`.
- Mediator: uv 0.11.30, CPython 3.12.13, mitmproxy 12.2.3, and committed `gateway_runtime/mediator/uv.lock` / `gateway/mediator/uv.lock`.
- Temporary security overrides remain `msgpack==1.2.1` and `tornado==6.5.7` until a supported mitmproxy release accepts them directly.

Upgrade checklist:

1. Update the source locks (`gateway/go.mod`, `gateway/go.sum`, `gateway/mediator/uv.lock`).
2. Rebuild `src/sbx/gateway_runtime/bin/sbx-gateway-{linux-amd64,darwin-amd64,darwin-arm64}`.
3. Copy the mediator lock and launcher files into `src/sbx/gateway_runtime/mediator/`.
4. Regenerate `src/sbx/gateway_runtime/SHA256SUMS`.
5. Run wheel-only mediator sync/audit, Go tests, Python tests, explicit-TCG functional/security acceptance, license audit, and `git diff --check`.
6. Keep macOS marked unverified unless the gateway was actually executed there.

Do not add a compiler, source build, privileged helper, unpinned runtime download, or shared single mitmproxy data plane as part of a dependency bump.
