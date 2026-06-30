# SmolVM package upgrade design

## Goal

Update `sbx` to use the latest published `smolvm` package and keep existing `sbx` commands working with the new SmolVM CLI.

Latest package found on PyPI: `smolvm 0.0.24.post2`.
Current package in `main/pyproject.toml`: `smolvm==0.0.19`.

## Problem

`sbx` still calls several old top-level SmolVM commands and assumes the `smolvm` executable is on `PATH`. New SmolVM uses noun-verb commands under `smolvm sandbox ...`, and `sbx` already depends on the Python package, so requiring a separate console script on `PATH` is unnecessary.

Failing old command shape:

```text
smolvm info NAME --json
smolvm list [--all]
smolvm start NAME --boot-timeout N
smolvm stop NAME
smolvm delete NAME --json
smolvm ssh NAME
```

Expected new command shape:

```text
smolvm sandbox info NAME --json
smolvm sandbox list [--all]
smolvm sandbox start NAME --boot-timeout N
smolvm sandbox stop NAME
smolvm sandbox delete NAME --json
smolvm sandbox ssh NAME
```

Preset start commands stay unchanged:

```text
smolvm pi start ...
smolvm claude start ...
smolvm codex start ...
```

## Scope

In scope:

- Update the `smolvm` dependency to `0.0.24.post2`.
- Allow stable post-release patches such as `.post1` / `.post2`.
- Reject alpha, beta, release-candidate, dev, and other pre-release/non-stable versions.
- Regenerate the lockfile from that dependency change.
- Replace old sandbox lifecycle command invocations with `smolvm sandbox ...`.
- Add an internal SmolVM command runner that falls back to the installed Python package when the `smolvm` executable is not on `PATH`.
- Update tests that assert command argv and missing-`PATH` behavior.
- Keep user-facing `sbx` commands unchanged.

Out of scope:

- Renaming `sbx` commands.
- Adding compatibility code for older SmolVM versions.
- Rewriting command calls to the full SmolVM Python API.
- Using `uv tool` to run SmolVM.
- Changing sandbox behavior beyond command compatibility.

## Design

Use the smallest compatibility change: centralize SmolVM subprocess invocation, then update command arrays where `sbx` shells out to SmolVM.

Add a tiny internal runner:

```python
def _smolvm_argv(args: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from smolvm.cli.main import main; raise SystemExit(main())",
        *args,
    ]


def _run_smolvm(args: Sequence[str], **kwargs: Any) -> int:
    return _run(_smolvm_argv(args), **kwargs)


def _run_smolvm_capture(
    args: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str] | None:
    return _run_capture(_smolvm_argv(args), **kwargs)
```

This keeps stdout, stderr, exit codes, and JSON behavior unchanged while removing the `PATH` requirement. Always use `sys.executable -c ...` so `sbx` uses the same `smolvm` package installed in its environment, not an older global `smolvm` executable.

No direct `["smolvm", ...]` argv should remain in `src/sbx/cli.py` after the migration; every SmolVM subprocess call should route through `_run_smolvm(...)` or `_run_smolvm_capture(...)`.

Remove `_require("smolvm", ...)` checks. Do not add a separate SmolVM import preflight; the `python -c ...` subprocess already fails if SmolVM is missing, and that keeps the runner small. The separate `smolvm` console script should no longer be required on `PATH`.

Command mapping:

| Current `sbx` call | New `sbx` call |
| --- | --- |
| `smolvm info NAME --json` | `smolvm sandbox info NAME --json` |
| `smolvm list --all` | `smolvm sandbox list --all` |
| `smolvm start NAME --boot-timeout N` | `smolvm sandbox start NAME --boot-timeout N` |
| `smolvm stop NAME` | `smolvm sandbox stop NAME` |
| `smolvm delete NAME --json` | `smolvm sandbox delete NAME --json` |
| `smolvm ssh NAME` | `smolvm sandbox ssh NAME` |

Do not add a larger command builder abstraction unless the replacement list grows. `_run_smolvm(...)` plus direct argv lists is enough for this migration.

## Mount path behavior

Bare `mount` entries should match the documented `--project-path` convenience: resolve the host path and mount it at the same absolute guest path. Keep explicit `HOST_PATH:GUEST_PATH` entries unchanged. This wires the existing `_same_path_mount(...)` helper into normal mount handling instead of leaving SmolVM to default bare mounts to `/workspace-N`.

When `project_path` is set, add its same-path mount before any extra `mount` entries so it is always the first workspace mount passed to SmolVM.

## Test strategy

Keep tests focused on the behavior under test. For command-shape tests, monkeypatch `_run_smolvm(...)` / `_run_smolvm_capture(...)` directly instead of creating fake `smolvm` executables and changing `PATH`. For missing-`PATH` coverage, assert `_smolvm_argv(...)` builds the `sys.executable -c ...` argv directly; no subprocess fake or `monkeypatch.undo()` is needed. Do not patch `_smolvm_argv(...)` in an autouse fixture; patch it only in tests that need fake `smolvm` shell-out behavior.

## Version policy

Use the latest stable SmolVM release available. PEP 440 post releases are stable and allowed, so `0.0.24.post2` is valid. Pre-releases and development releases are not allowed for this upgrade, including versions containing `a`, `alpha`, `b`, `beta`, `rc`, `pre`, `preview`, or `dev`.

## Files expected to change during implementation

- `pyproject.toml`
- `uv.lock`
- `src/sbx/cli.py`
- `tests/test_cli.py`
- `tests/test_cli_extra.py`

## Verification plan

Run the existing project checks from the implementation worktree:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```

Also run focused tests first while changing command argv:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
```

## Open questions

None for the minimal migration.
