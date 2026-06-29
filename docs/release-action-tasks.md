# Release action tasks

## Implementation worktree

Use:

```bash
/home/nueces/code/sbx/feature/release-action
```

## Tasks

1. [x] Add version bump script
   - Create `.github/scripts/bump_version.py`.
   - Accept one argument: `x.y.z`.
   - Validate digits-only semantic version format.
   - Update `pyproject.toml`, `src/sbx/__init__.py`, and the `name = "sbx"` package block in `uv.lock`.

2. [x] Add GitHub release workflow
   - Create `.github/workflows/release.yml`.
   - Use `workflow_dispatch` with required `version` input.
   - Run the bump script.
   - Commit changed version files.
   - Create and push tag `vX.Y.Z`.
   - Create GitHub release `vX.Y.Z`.

3. [x] Add focused test
   - Add `tests/test_bump_version.py` for valid bump and invalid input.

4. [x] Run checks

   ```bash
   cd /home/nueces/code/sbx/feature/release-action
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_bump_version.py
   UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
   ```

## Done when

- Manual action can bump to `x.y.z`.
- Tag name is exactly `vX.Y.Z`.
- Release name is exactly `vX.Y.Z`.
- Focused test and ruff pass.
