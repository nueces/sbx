# Release action design

## Goal

Provide a protected release flow that prepares version bump pull requests, publishes the GitHub release only after the main release PR merges, and updates the website through its own protected branch.

## Scope

In scope:

- Manual `Prepare release` workflow with optional `version` input.
- Blank version means next patch after the previous `vX.Y.Z` tag; no previous tag means `0.1.0`.
- Version format: `MAJOR.MINOR.PATCH` digits only. A leading `v` in manual input is stripped.
- Main release branch named `release/vX.Y.Z`.
- Webpage release branch named `webpage/release-vX.Y.Z`.
- Main release PR updates only:
  - `pyproject.toml`
  - `src/sbx/__init__.py`
  - `uv.lock`
- Webpage release PR updates only `index.html` release markers.
- Publish release workflow creates tag/release `vX.Y.Z` after the `release/v*` PR merges into `main`.
- Webpage release PR is blocked until matching tag `vX.Y.Z` exists.

Out of scope:

- Publishing to PyPI.
- Pre-release/build metadata.
- Direct pushes to protected `main` or `webpage/main`.

## Design

Keep version editing in small GitHub-action-local Python scripts using only the standard library:

- `.github/scripts/bump_version.py` updates package version files.
- `.github/scripts/bump_webpage_version.py` updates the two `<!-- sbx-release-version -->...<!-- /sbx-release-version -->` markers in `index.html`.

`prepare-release.yml` runs manually. It fetches tags, resolves the requested version, creates a `release/vX.Y.Z` branch, runs the package bump, pushes the branch, and opens a PR to `main`. It also clones `webpage/main`, creates `webpage/release-vX.Y.Z`, runs the webpage bump, pushes the branch, and opens a PR to `webpage/main`.

PR creation uses the `RELEASE_PR_TOKEN` secret because the default `GITHUB_TOKEN` is not permitted to create pull requests in this repository. The token should only need contents read/write for release branches and pull-request read/write; it must not bypass protected branch rules.

`release-pr-checks.yml` validates PRs from `release/v*` to `main`. It rejects any changed file except `pyproject.toml`, `src/sbx/__init__.py`, and `uv.lock`. It also restricts `pyproject.toml` to the `version = "X.Y.Z"` line and `src/sbx/__init__.py` to the `__version__ = "X.Y.Z"` line, verifies both match the branch version, reruns `bump_version.py`, and requires no diff.

`publish-release.yml` triggers when a PR into `main` closes. It publishes only merged PRs whose source branch starts with `release/v`. Before tagging, it repeats the release validation so a branch name alone cannot publish a release. If valid and the tag does not already exist, it creates and pushes tag `vX.Y.Z`, then runs `gh release create "$RELEASE_TAG" --title "$RELEASE_TAG" --generate-notes`.

`webpage/main` owns `webpage-release-pr-check.yml`. It validates PRs from `webpage/release-v*` to `webpage/main`, rejects any changed file except `index.html`, requires exactly two release markers with the matching tag, and fails until tag `vX.Y.Z` exists.

## Verification

Run the focused script tests and workflow linters through the normal CI/pre-commit path:

```bash
UV_PROJECT_ENVIRONMENT=/home/agent/venv/release-action uv run --python 3.12 --extra dev pytest --no-cov tests/test_bump_version.py tests/test_bump_webpage_version.py
UV_PROJECT_ENVIRONMENT=/home/agent/venv/release-action uv run --python 3.12 --extra dev ruff check .
```
