# Release action tasks

## Implementation worktrees

Use:

```bash
/home/nueces/code/sbx/feature/release-latest-version
/home/nueces/code/sbx/webpage/release-pr-check
```

## Tasks

1. [x] Add package version bump script
   - Create `.github/scripts/bump_version.py`.
   - Accept one argument: `x.y.z`.
   - Validate digits-only semantic version format.
   - Update `pyproject.toml`, `src/sbx/__init__.py`, and the `name = "sbx"` package block in `uv.lock`.

2. [x] Add webpage version bump script
   - Create `.github/scripts/bump_webpage_version.py`.
   - Accept version and page path.
   - Update exactly two `sbx-release-version` markers.

3. [x] Add prepare release workflow
   - Create `.github/workflows/prepare-release.yml`.
   - Use optional `workflow_dispatch` `version` input.
   - Blank input selects next patch from previous `vX.Y.Z` tag.
   - Create `release/vX.Y.Z` PR to `main`.
   - Create `webpage/release-vX.Y.Z` PR to `webpage/main`.
   - Use `RELEASE_PR_TOKEN` for PR creation.

4. [x] Add release PR validation
   - Create `.github/workflows/release-pr-checks.yml`.
   - Run on `release/v*` PRs to `main`.
   - Allow only `pyproject.toml`, `src/sbx/__init__.py`, and `uv.lock`.
   - Allow only the version line in `pyproject.toml`.
   - Allow only the `__version__` line in `src/sbx/__init__.py`.
   - Rerun `bump_version.py` and require no diff.

5. [x] Add publish release workflow
   - Create `.github/workflows/publish-release.yml`.
   - Trigger when PRs into `main` close.
   - Publish only merged `release/v*` PRs.
   - Repeat release validation before tagging.
   - Create and push tag `vX.Y.Z`.
   - Create GitHub release with generated notes.

6. [x] Add webpage release PR validation on `webpage/main`
   - Create `.github/workflows/webpage-release-pr-check.yml` on the `webpage/main` branch.
   - Run on `webpage/release-v*` PRs to `webpage/main`.
   - Allow only `index.html`.
   - Require exactly two matching release markers.
   - Require matching tag `vX.Y.Z` to exist before merge.

7. [x] Add focused tests
   - Add `tests/test_bump_version.py`.
   - Add `tests/test_bump_webpage_version.py`.

8. [x] Run checks

   ```bash
   cd /home/nueces/code/sbx/feature/release-latest-version
   UV_PROJECT_ENVIRONMENT=/home/agent/venv/release-action uv run --python 3.12 --extra dev pytest --no-cov tests/test_bump_version.py tests/test_bump_webpage_version.py
   UV_PROJECT_ENVIRONMENT=/home/agent/venv/release-action uv run --python 3.12 --extra dev ruff check .
   ```

## Done when

- Manual prepare action creates release and webpage PRs.
- No workflow pushes directly to protected `main` or `webpage/main`.
- Release PR cannot include unrelated files or non-version edits to package entry files.
- Publish workflow creates tag/release only after a valid `release/v*` PR merges.
- Webpage PR cannot merge until the matching release tag exists.
