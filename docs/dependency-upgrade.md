# Dependency upgrades

Rootless gateway runtime pins are release inputs, not loose ranges:

- Go worker: Go 1.26.5 with `GOTOOLCHAIN=local` and the committed `gateway/go.mod`/`go.sum`.
- Mediator: root `pyproject.toml` pins the PyPI release mitmproxy 12.3.1, cryptography 50.0.2 and h2 4.4.1. Tornado has no direct version pin; `uv.lock` still resolves 6.5.10 under mitmproxy's upper bound. This replaces the temporary Git dependency. CPython 3.12 is the supported CI baseline; newer CPython versions are best effort and depend on available dependency wheels.
- **Release blocker (reverified 2026-11-11):** mitmproxy 12.3.1 requires Tornado <=6.5.10, but the audit reports GHSA-pw9p-v7r6-gx4p, GHSA-r4j5-9xpp-3985, and GHSA-pcg2-g6cj-q92c, fixed in Tornado 6.6. Do not suppress these findings, weaken CI, or force conflicting package metadata. Release requires an upstream-compatible fixed dependency or a separately reviewed mitigation. GitHub affected ranges and [PyPI's 6.5.10 metadata](https://pypi.org/pypi/tornado/6.5.10/json) list all versions `<6.6`; these are not findings already fixed in 6.5.10. The current locked audit confirms all three and no additional known vulnerabilities.
- **Darwin amd64 wheel gate blocked:** cryptography 50.0.2 publishes Darwin arm64 wheels but no Darwin amd64 wheel. Wheel-only resolution succeeds for Darwin arm64 and Linux amd64, but not Darwin amd64. Published stable 49.x/50.x releases lack the amd64 wheel; 48.0.1 has universal2 wheels but known security findings, so no vulnerable downgrade was introduced. Building the Go artifact does not establish Python runtime installability; do not claim that platform passed or silently allow source builds.

Upgrade checklist:

1. Update the source locks (`uv.lock`, `gateway/go.mod`, and `gateway/go.sum`).
2. Rebuild `src/sbx/gateway_runtime/bin/sbx-gateway-{linux-amd64,darwin-amd64,darwin-arm64}` and copy the mediator launcher/addon files.
3. Run root wheel-only resolution/audit, mediator tests, Go tests, Python tests, explicit-TCG functional/security acceptance, license audit, and `git diff --check`.
4. Keep macOS marked unverified unless the gateway was actually executed there.

Do not add a compiler, source build, privileged helper, unpinned runtime download, or shared single mitmproxy data plane as part of a dependency bump.
