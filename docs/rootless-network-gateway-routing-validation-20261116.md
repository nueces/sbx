# Pinned-IP routing fix: focused validation, 2026-11-16

**Status: focused routing fix validated; NOT MERGE READY.** This closes the reproduced HTTP connection-pool mismatch for the tested candidate, not the broader performance, preset, platform or final acceptance gates.

Implementation worktree: `feature/rootless-network-gateway`, HEAD `3477517a27a56d0479ca700194f4c02ab84c9e25` plus existing uncommitted work and this change. No commit, dependency-policy change or security exception was introduced.

## Behavior

The mediator now gives mitmproxy a numeric request-routing host that agrees with the pinned upstream connection address, while restoring the original validated logical HTTP Host/authority. Upstream SNI is explicitly the authenticated logical hostname, including when mitmproxy initially derives numeric SNI from a new routing target. TLS verification stays enabled.

Logical policy identity and numeric transport identity are separately bound to the authenticated client/admission:

- Request bindings are immutable, flow-scoped and indexed only while active. They preserve scheme and wire authority representations. The client retains one latest validated acquisition hint, not a historical DNS authorization cache.
- Original requests must pass logical destination, authority, policy, hook and credential checks before routing mutation. Routed requests must retain their issued numeric fields and logical authorities; shared checks also cover response hooks, streaming and WebSocket messages.
- Every new connection resolves the logical hostname again and rejects forbidden/mixed/empty answers or resolver failures. A valid current endpoint survives answer reordering; a changed valid answer can repin the new connection. A cached numeric hint alone does not authorize dialing it.
- During rotation, an already-issued acquisition hint can differ from the freshly validated connection endpoint. Both bindings must remain valid for the same client/admission; subsequent requests use the new hint. No open address is relabeled.
- Relative redirects and authority-less requests use a validated logical URL, including OPTIONS `*`. Original audit evidence remains logical and is not overwritten by repeated preparation.
- Capacity is reserved before asynchronous DNS; disconnect, cancellation, errors and terminal events release active bindings. Admission is rechecked after awaits, preventing disconnected/replaced clients from publishing new pins.

`requestheaders` is now asynchronous; its direct unit-test callers use the standard-library asyncio runner. No public CLI option, capability format, audit schema or runtime dependency changed. Mitmproxy remains **12.3.1**, with lazy connections, unchanged limits, keep-alive, hardening and no-replay behavior.

## Verification

The isolated proof preceded production integration. The baseline mediator run failed the retained sixteen-request HTTP/1 regression with a read timeout; the earlier technical campaign identified its sixth-request stall. The regression remains enabled, not xfailed.

| Check | Final result |
|---|---|
| Fast Python suite | **1,107 passed**, **92.20%** coverage; required threshold 90% |
| Source mediator suite | **146 passed + 8 subtests**, zero failures/errors/skips |
| Clean-installed mediator suite | **146 passed + 8 subtests**, zero failures/errors/skips |
| HTTPS keep-alive regression | Sixteen exactly-once origin requests over **one** upstream connection; logical Host and SNI |
| Cold plaintext HTTP | Sixteen exactly-once requests over **one** upstream connection |
| HTTP/2 | Sixteen multiplexed streams, exactly once, logical authority/SNI; one HTTP/2 upstream connection |
| HTTP/2 → HTTP/1 | Sixteen streams complete with logical authority/SNI, retaining mitmproxy's own conversion/connection-close behavior |
| Live DNS rotation | Same client survives silent upstream close, repins IPv4 → IPv6, then completes sixteen exactly-once requests over one replacement connection |
| Live private rebinding | New connection denied with 502; no requests reach the alternate origin |
| Certificate identity | Valid logical-name certificate succeeds; trusted-chain/wrong-name certificate is rejected before origin HTTP bytes |
| Identity/security regressions | Numeric guest targets even after cache warming, authority retargeting, physical HTTP/2 Host mismatch, cross-client/incarnation and peer/SNI binding mismatches denied |
| Lifecycle/limits | Concurrent preparations, DNS failures, disconnect/replacement during lookup, cancellation, audit/credential/hook failure and existing crash/no-fallback checks pass |
| Audit/UI | Logical hostname retained; private routing bindings, placeholders and synthetic injected credentials absent from snapshots |
| Source/package/wheel/install parity | All **44** files under `src/sbx` match wheel and clean install; all four source/packaged mediator modules match |
| Lint | Whole project plus explicit mediator directory pass |
| Changed-file formatting / whitespace | All eight changed Python paths pass formatting; `git diff --check` passes |

The clean wheel installation ran from outside the worktree with isolated HOME/state, no editable project installation, installed module paths asserted, and **20 mediator launches** explicitly using installed launchers/addons. The existing standalone hardening subprocess probes still select their test-directory helper by cwd; its bytes are identical to the installed helper. Test-only fixture scripts are not packaged.

The live rebinding fixture uses a test-only DNS control file and upstream-disconnect acknowledgement, not timing assumptions or host DNS changes. It binds only IPv4/IPv6 loopback listeners. Production launchers never import that fixture or enable its DNS/loopback overrides; the production-path loopback-denial regression still passes.

### Formatting limitation

The whole-tree format check still reports these **unchanged baseline files**:

- `.github/scripts/generate_complexity_report.py`
- `tests/test_check_image_build_inputs.py`
- `tests/test_project_shell.py`

They were not reformatted as part of this fix. This is not described as an entirely green whole-tree formatting run.

## Artifact and reproducibility

Final wheel:

```text
sbx-0.2.6.dev0-py3-none-any.whl
SHA-256 65d3a97fb2b43bf63eb23f3be7724d48f034de4adebc55adcc214f04b06ffb20
```

Ignored evidence: `local/gateway-validation/20261116-routing-fix/`.

- `baseline.json`, `baseline.patch`, `baseline-mediator.log`, `baseline-fast.log` preserve the starting point.
- `proof/`, `proof-final.log` retain the isolated experiment, distinct from final production tests.
- `mediator-final.log`, `fast-final.log`, `installed-final.log` and JUnit XML retain final results. JUnit counts include the eight subtests, hence 154 cases for each mediator run.
- `installed-runtime-proof.json` records installed import paths and launch arguments; `candidate-final.json` records hashes, parity and baseline preservation.
- `dist/` and `installed-final/` contain the final candidate; `dist-initial/`, `installed/` and initial logs retain the earlier candidate rather than overwriting it.
- Build, locked export and installation logs plus `requirements.txt` and `dependencies.json` trace the isolated validation environment.

Use the existing explicit Python environment rather than the pre-existing looping `.venv/bin/python` symlink. For example, from the feature worktree:

```bash
PATH=/tmp/sbx-gwa/env/bin:$PATH PYTHONPATH=src \
  /tmp/sbx-gwa/env/bin/python -m pytest -o addopts='' --no-cov -q gateway/mediator

env -u PYTHONPATH \
  PATH="$PWD/local/gateway-validation/20261116-routing-fix/installed-final/bin:$PATH" \
  local/gateway-validation/20261116-routing-fix/installed-final/bin/python -I \
  local/gateway-validation/20261116-routing-fix/check_installed.py
```

## Dependencies and preserved scope

No apt package was installed. Fresh validation environments used the existing `uv.lock` with the dev extra; the lock and project dependency declarations are unchanged. Exact direct/transitive installed versions are in `dependencies.json`.

Direct runtime versions: cryptography **50.0.2**, h2 **4.4.1**, mitmproxy **12.3.1**, SmolVM **0.0.28**, Tornado **6.5.10**. Direct test tools: pytest **9.1.1**, pytest-cov **7.1.0**, pre-commit **4.6.0**, Ruff **0.15.21**. These are traceability records, not a renewed vulnerability clearance.

The finalizer verifies all **172** baseline paths, allowing only this task's declared edits. Unrelated dirty work, `uv.lock`, Go sources and all three packaged workers remain byte-unchanged from the starting snapshot. No worker rebuild is claimed. Tests clean up their processes/listeners; no VM, mount, cgroup or privileged observer was created for this slice.

## Remaining gates

No preset provisioning, reference throughput/latency/RSS/concurrency campaign, 600-second mixed stability run, native macOS validation, Go race run or fresh dependency-security audit was performed. Licensing remains separate. The previously documented mandatory gates stay open; this focused result is not a waiver or overall merge approval.
