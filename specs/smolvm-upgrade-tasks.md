# SmolVM package upgrade tasks

## Implementation worktree

Use:

```bash
/home/nueces/code/sbx/feature/smolvm-upgrade
```

Do not edit `main/` directly.

## Tasks

1. Update the SmolVM dependency
   - Change `pyproject.toml` from `smolvm==0.0.19` to `smolvm==0.0.24.post2`.
   - Confirm the selected version is stable: PEP 440 `.postN` releases are allowed; alpha, beta, release-candidate, dev, and other pre-release/non-stable versions are not.
   - Regenerate `uv.lock` with the project-standard uv command.

2. Add the internal SmolVM runner
   - In `src/sbx/cli.py`, add `_smolvm_argv(args)`.
   - Add `_run_smolvm(args, **kwargs)`.
   - Add `_run_smolvm_capture(args, **kwargs)`.
   - Runner must use:

     ```python
     sys.executable, "-c", "from smolvm.cli.main import main; raise SystemExit(main())"
     ```

   - Do not use `uv tool`.
   - Do not call the full SmolVM Python API for these shell-out paths.

3. Route every SmolVM subprocess through the runner
   - Replace every direct `['smolvm', ...]` / `["smolvm", ...]` subprocess argv in `src/sbx/cli.py` with `_run_smolvm(...)` or `_run_smolvm_capture(...)`.
   - Verify with:

     ```bash
     rg '\["smolvm"|\['"'"'smolvm'"'"'' src/sbx/cli.py
     ```

   - Expected result: no direct SmolVM subprocess argv remains.

4. Update SmolVM lifecycle command shapes
   - Replace old lifecycle args with new sandbox args:

     | Old args passed to runner | New args passed to runner |
     | --- | --- |
     | `info NAME --json` | `sandbox info NAME --json` |
     | `list --all` | `sandbox list --all` |
     | `start NAME --boot-timeout N` | `sandbox start NAME --boot-timeout N` |
     | `stop NAME` | `sandbox stop NAME` |
     | `delete NAME --json` | `sandbox delete NAME --json` |
     | `ssh NAME` | `sandbox ssh NAME` |

   - Keep preset starts as:

     ```text
     pi start ...
     claude start ...
     codex start ...
     ```

5. Remove executable PATH checks
   - Remove `_require("smolvm", ...)` checks before SmolVM commands.
   - Do not add a separate SmolVM import preflight; let the `python -c ...` subprocess fail if the dependency is missing.
   - The `smolvm` console script must not be required on `PATH`.

6. Keep the runner lean
   - Do not add `importlib.util` just to check whether `smolvm.cli.main` exists.
   - Do not add a `SMOLVM_IMPORT_HINT` constant or `_smolvm_cli_available()` helper.
   - `_run_smolvm(...)` should be one line around `_run(_smolvm_argv(args), **kwargs)`.
   - `_run_smolvm_capture(...)` should be one line around `_run_capture(_smolvm_argv(args), **kwargs)`.

7. [x] Update tests for command argv
   - Update expected argv in `tests/test_cli.py` and `tests/test_cli_extra.py`.
   - For tests that only care about SmolVM args, monkeypatch `_run_smolvm(...)` / `_run_smolvm_capture(...)` directly and capture the args passed to those helpers.
   - Do not create fake `smolvm` executables or edit `PATH` for command-shape tests.
   - Do not patch `_smolvm_argv(...)` in an autouse fixture; patch it only in tests that still need fake `smolvm` shell-out behavior.

8. [x] Add missing-PATH coverage
   - Add one focused test proving the runner no longer depends on the `smolvm` console script.
   - Keep it tiny: assert `_smolvm_argv(["doctor"])` starts with `sys.executable, "-c", "from smolvm.cli.main import main; raise SystemExit(main())"` and appends `"doctor"`.
   - Do not mock subprocess execution, change `PATH`, or call `monkeypatch.undo()` for this test.

9. [x] Run focused checks

   ```bash
   cd /home/nueces/code/sbx/feature/smolvm-upgrade
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
   ```

10. [x] Run final checks

   ```bash
   cd /home/nueces/code/sbx/feature/smolvm-upgrade
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
   ```

## Phase 2: fix bare mount same-path bug

11. [x] Normalize and order mount entries
   - In `src/sbx/cli.py`, convert `[sbx].mount` / `--mount` entries without an explicit guest path through `_same_path_mount(...)` before passing them to SmolVM.
   - Leave explicit `HOST_PATH:GUEST_PATH` entries unchanged.
   - When `project_path` is set, add its same-path mount before any extra `mount` entries; project mount must always be first.
   - Reuse the existing `_same_path_mount(...)` helper; do not add a new mount abstraction.

12. [x] Add mount regression coverage
   - Add or update one test proving `mount = ["/host/path"]` becomes `--mount /host/path:/host/path`.
   - Keep existing explicit-path behavior covered: `mount = ["/host/path:/guest/path"]` stays unchanged.
   - Add or update one test proving `project_path` mount appears before extra mounts.

13. [x] Run focused checks

   ```bash
   cd /home/nueces/code/sbx/feature/smolvm-upgrade
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_cli_extra.py
   ```

14. [x] Run final checks

   ```bash
   cd /home/nueces/code/sbx/feature/smolvm-upgrade
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
   ```

## Done when

- `pyproject.toml` pins stable `smolvm==0.0.24.post2`.
- No alpha, beta, release-candidate, dev, or other pre-release SmolVM version is selected.
- `uv.lock` is regenerated.
- No direct SmolVM subprocess argv remains in `src/sbx/cli.py`.
- Old lifecycle commands are updated to `smolvm sandbox ...` args.
- `sbx` no longer requires `smolvm` on `PATH`.
- Focused and final checks pass.
