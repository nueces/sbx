# Release action design

## Goal

Add a manual GitHub Actions release workflow. A maintainer enters a version like `x.y.z`; the workflow updates project version files, commits the bump, creates tag `vX.Y.Z`, and creates GitHub release `vX.Y.Z`.

## Scope

In scope:

- Manual `workflow_dispatch` input named `version`.
- Strict version format: `MAJOR.MINOR.PATCH` digits only.
- Update `pyproject.toml`, `src/sbx/__init__.py`, and the local `sbx` package entry in `uv.lock`.
- Commit the version bump if files changed.
- Create and push tag `vX.Y.Z`.
- Create GitHub release `vX.Y.Z`.

Out of scope:

- Publishing to PyPI.
- Changelog generation.
- Pre-release/build metadata.

## Design

Keep version editing in one small GitHub-action-local Python script using only the standard library. The GitHub workflow calls it, commits changed files, pushes the branch and tag, then uses `gh release create`.

The version files use the raw input version. The tag and release name prefix it with `v`, for example input `1.2.3` creates tag/release `v1.2.3`.

## Verification

Add one focused test for the version bump script. Run:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_bump_version.py
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
