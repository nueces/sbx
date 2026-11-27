# Development and validation

## Prerequisites

Work from the sbx repository root, not a multi-worktree organization directory. Use Python 3.12, the root uv lock, Go 1.26.5 with `GOTOOLCHAIN=local`, and the host tools needed by the selected checks. End users do not need Go or a compiler to run a packaged gateway.

Use an ignored environment under `local/` to avoid changing an unrelated `.venv`:

```bash
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest --no-cov
```

Do not use real credentials in tests. Keep private state, CA keys, captures and archives outside guest shares and out of Git. A passed component test is not a native-platform, image-boot, long-soak or release acceptance claim.

## Fast checks

Run once before editing in a fresh environment, then run focused tests and the full fast suite after behavior changes:

```bash
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --extra dev pytest --no-cov tests/test_network_credentials_docs.py tests/test_documentation.py tests/test_bump_version.py
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --extra dev ruff check .
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --extra dev ruff format --check .
```

Keep fast tests directly under `tests/`, with `test_*.py` modules/functions and module-level imports. Use `tmp_path`, `monkeypatch` and fake external runners. Do not start QEMU, SSH, Docker or SmolVM in fast tests. Real system orchestration belongs in `tests/integration/`, excluded from default collection.

Report pre-existing failures separately instead of formatting unrelated files or describing a partial run as fully green. Use Ruff without `--check` only when intentionally formatting your changed files.

## Gateway validation

### Mediator and workers

From the repository root on the host:

```bash
GO=go gateway/scripts/build_release_artifacts.sh
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --extra dev pytest --no-cov gateway/mediator
GOTOOLCHAIN=local go -C gateway test ./...
```

The build script rebuilds all three workers and synchronizes four mediator assets. Production workers are pure Go (`CGO_ENABLED=0`) so all-thread confinement remains valid. Cross-building Darwin is not native macOS acceptance.

Race tests need a host C compiler and libc development headers. Use native temporary storage if the workspace/shared filesystem rejects writable mmap; never disable tests or hardening to bypass that limitation:

```bash
GO_TMP=$(mktemp -d /tmp/sbx-go.XXXXXX)
TMPDIR="$GO_TMP" GOTMPDIR="$GO_TMP" CGO_ENABLED=1 GOTOOLCHAIN=local go -C gateway test -race ./...
```

The hardening test's confinement child intentionally remains pure Go; a race-instrumented parent does not establish race instrumentation of that child. Retain failure output before removing the owned temporary directory.

### Linux integration

Prerequisites: non-root test user, QEMU/system image tools, a prepared test guest, OpenSSL, and working noninteractive sudo for the explicitly owned fixture operations. The direct-IP fixture uses `unshare`, `ip` and `setpriv`: privileged setup creates addresses only in a disposable network namespace, then drops UID/groups/capabilities **before** gateway/QEMU execution. No public-shaped test IP is assigned to a shared host interface.

The separate root-owned observer needs permission to count nondumpable processes. It verifies UID, PID start time, cgroup and process-tree identity, and reads counts—not descriptor contents/targets. Missing/incomplete observations fail; they are never reported as zero. Production gateway processes remain unprivileged and nondumpable.

From the repository root:

```bash
gateway/scripts/build_test_guest.sh
OBSERVER_DIR=$(mktemp -d /tmp/sbx-observer.XXXXXX)
sudo chown root:root "$OBSERVER_DIR"
sudo chmod 0755 "$OBSERVER_DIR"
sudo install -m 0444 gateway/scripts/resource_usage.py "$OBSERVER_DIR/observer.py"
export SBX_GATEWAY_RESOURCE_OBSERVER="$OBSERVER_DIR/observer.py"
export SBX_GATEWAY_VM_COUNT=4
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest --no-cov -s tests/integration
```

Explicitly selected integrations fail missing prerequisites rather than skip. Workload waits and resources must be bounded. Clean up owned processes/listeners/observers even after failure; preserve and verify required diagnostic streams, guest disks/logs, audit/DNS state and archive members before deleting evidence. Capture/archive failure must block removal. Do not replay unknown admitted transactions to recover evidence.

After tests and required evidence verification, remove only this observer staging:

```bash
sudo rm "$OBSERVER_DIR/observer.py"
sudo rmdir "$OBSERVER_DIR"
unset SBX_GATEWAY_RESOURCE_OBSERVER SBX_GATEWAY_VM_COUNT
```

Runtime gateway privilege requirements are separate from image building, namespace setup, observation and owned disposable storage-fault setup. No test may grant privileges to the gateway or disable nondumpability to make measurements work.

### Additional acceptance

- [Operator smoke](rootless-network-gateway-manual-validation.md): public CLI, isolated project/state, no real credentials.
- [Gateway-specific stability checks](../gateway/README.md#actual-vm-stability-validation): actual workload, frozen resource criteria, identity-checked samples and replacement.
- [Image Landlock boot gate](build-local-debian-pi-image.md#landlock): run per architecture. A skipped gate is not a pass.
- [Dependency/distribution checks](dependency-upgrade.md): locked wheel-only installation, audit and notices.

Resource snapshots do not establish an hour-long plateau. A short smoke does not clear full performance, overload/fault or native macOS gates. State the exact artifact, environment and unexecuted checks in validation results; keep raw reports outside maintained guides.

## Packaging and integration seams

`hatch_build.py` fails the build if a packaged worker is missing or not executable; it never changes file modes. Shared worktrees may not expose the executable bit correctly. Execute the clean-installed worker and mediator, verify asset parity, and do not infer installability from a source inspection.

The [fragile-glue ledger](fragile-glue.md) names private upstream APIs and exit conditions. Check changes to pinned SmolVM/Moby/kernel inputs and notices:

```bash
python .github/scripts/check_image_build_inputs.py
```

This read-only checker reports changed inputs; it never updates executable pins automatically.

## Documentation conventions

- Start procedural pages with purpose and prerequisites; follow with steps, expected results, failure/cleanup and related reference links.
- Label commands as host, guest-user or guest-root. Do not mix shell contexts without an explicit transition.
- Identify TOML as complete configuration or fragments; give fragments a concrete validation context. Plugin entry-point metadata is not sbx configuration.
- Put data-loss/security warnings **before** destructive commands. Prefer stop/reprovision to recreate; preserve private evidence before removal.
- Use portable paths and consistent example names. The default image is `sbx`; custom images must be labelled as custom.
- Keep one authoritative explanation per subject and link to it. Runtime, build, test and observation privileges are different subjects.
- Keep proposals, campaign histories, raw evidence and local backlog outside user instructions. Describe implemented behavior in present tense; distinguish tested, unverified and blocked.
- Extend the existing fast documentation tests for links/anchors, configuration fragments and literal CLI parsing. Never blindly execute shell blocks, network calls or cleanup examples.
- Preserve the release automation's single tagged README installation line. Documentation for the current checkout must not claim an unpublished feature is in an older tag.
