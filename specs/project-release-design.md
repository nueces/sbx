# Project release infrastructure

## Purpose

This document describes the repository automation used to release `sbx`. It is project-management infrastructure, not runtime `sbx` product behavior.

The release flow prepares version bump pull requests, publishes the GitHub release only after the main release PR merges, and updates the website through its own protected branch.

## Release branches

Release automation is split across protected branches:

- `main` contains the Python package and GitHub release workflows.
- `webpage/main` contains the static website and its own release PR guard.

The release branches are named:

```text
release/vX.Y.Z
webpage/release-vX.Y.Z
```

## Prepare release workflow

`prepare-release.yml` is a manual workflow. It resolves the requested version:

- explicit input `1.2.3` releases `v1.2.3`,
- explicit input `v1.2.3` is normalized to `v1.2.3`,
- blank input means next patch after the previous `vX.Y.Z` tag,
- no previous tag means `v0.1.0`.

It creates two PR branches:

- `release/vX.Y.Z` -> `main`, updating package version files,
- `webpage/release-vX.Y.Z` -> `webpage/main`, updating webpage release markers.

PR creation uses the `RELEASE_PR_TOKEN` secret because the default `GITHUB_TOKEN` cannot create pull requests in this repository. Branch protection still controls merges; the token must not bypass protected branch rules.

## Main release PR guard

`release-pr-checks.yml` validates PRs from `release/v*` to `main`.

It allows only these changed files:

```text
pyproject.toml
src/sbx/__init__.py
uv.lock
```

It also restricts:

- `pyproject.toml` to the `version = "X.Y.Z"` line,
- `src/sbx/__init__.py` to the `__version__ = "X.Y.Z"` line.

The workflow verifies both versions match the branch name, reruns `bump_version.py`, and requires no diff. This prevents release PRs from smuggling unrelated code or hidden package-entry changes.

## Publish release workflow

`publish-release.yml` runs when PRs to `main` close. It publishes only merged PRs whose source branch starts with `release/v`.

Before creating a tag, it repeats the release validation so a branch name alone cannot publish a release. If valid and the tag does not already exist, it creates and pushes tag `vX.Y.Z`, then creates the GitHub Release:

```bash
gh release create "$RELEASE_TAG" --title "$RELEASE_TAG" --generate-notes
```

## Webpage release PR guard

`webpage/main` owns `webpage-release-pr-check.yml`. It validates PRs from `webpage/release-v*` to `webpage/main`.

It allows only:

```text
index.html
```

It requires exactly two matching release markers:

```html
<!-- sbx-release-version -->vX.Y.Z<!-- /sbx-release-version -->
```

It also requires tag `vX.Y.Z` to already exist. This blocks the webpage PR until the package release has been published.
