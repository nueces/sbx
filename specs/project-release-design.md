# Project release infrastructure

## Purpose

This document defines the repository automation used to release `sbx`. It is project-management infrastructure, not runtime `sbx` product behavior.

The flow opens a package release PR, publishes only after that PR merges, then opens separate website and next-development-version PRs. No workflow pushes directly to protected `main` or `webpage/main`.

## Version states

Package development versions use:

```text
X.Y.Z.devN
```

Published versions and tags use:

```text
X.Y.Z
vX.Y.Z
```

A release changes the package from an existing `X.Y.Z` or `X.Y.Z.devN` version to the final `X.Y.Z` named by the release branch. After publishing `vX.Y.Z`, the automation proposes `X.Y.(Z+1).dev0` for the next development cycle.

Development suffixes are valid package states but must never appear in a release branch name, published tag, README install tag, or website release marker.

## Branches

Release automation uses these branches:

```text
release/vX.Y.Z
webpage/release-vX.Y.Z
post-release/vX.Y.Z-dev
```

- `main` contains the Python package and GitHub release workflows.
- `webpage/main` contains the static website and its release PR guard.
- `release/vX.Y.Z` targets `main` with the final package version.
- `webpage/release-vX.Y.Z` targets `webpage/main` after the tag exists.
- `post-release/vX.Y.Z-dev` targets `main` with the next `.dev0` version.

## Version bump scripts

`.github/scripts/bump_version.py` accepts only `X.Y.Z` or `X.Y.Z.devN` and updates exactly one package version occurrence in each of:

```text
pyproject.toml
src/sbx/__init__.py
uv.lock
```

For final `X.Y.Z` versions it also updates the README install URL to tag `vX.Y.Z`. Development bumps do not change the README because it must continue to identify the latest published release.

`.github/scripts/bump_webpage_version.py` accepts only final `X.Y.Z` versions and updates exactly two marked release-version occurrences in the website.

Both scripts fail if their expected version occurrences are missing or ambiguous.

## Start release workflow

`.github/workflows/start-release.yml` is the manual entry point. Its optional input accepts `X.Y.Z` or `vX.Y.Z`; a leading `v` is removed. Blank input selects the next patch after the latest `vX.Y.Z` tag, or `0.1.0` when no release tag exists.

The workflow rejects an existing target tag or release branch, creates `release/vX.Y.Z`, runs `bump_version.py X.Y.Z`, and opens a PR to `main` using `RELEASE_PR_TOKEN`.

The release PR changes exactly four lines in four allowed files:

```text
README.md
pyproject.toml
src/sbx/__init__.py
uv.lock
```

## Main release PR guard

`.github/workflows/release-pr-checks.yml` validates only `release/v*` PRs to `main`.

It allows changes only in the four release files, requires four insertions and four deletions in total, and rejects every other changed file. For package version lines, the deleted/source value may be either `X.Y.Z` or `X.Y.Z.devN`; the inserted/result value must be the final `X.Y.Z` from the branch name. This distinction is required because normal development state uses `.devN` while releases do not.

The README may change only its tagged install URL between final `vX.Y.Z` values. The workflow verifies the checked-out `pyproject.toml` and `src/sbx/__init__.py` versions exactly match the branch version, reruns `bump_version.py` with that version, and requires no remaining diff. This also verifies the `uv.lock` and README results without duplicating their update logic.

## Publish release workflow

`.github/workflows/publish-release.yml` runs when PRs to `main` close and proceeds only for merged `release/v*` PRs.

Before publishing, it repeats the complete release PR validation, including acceptance of a deleted/source `.devN` package version and the requirement that the resulting version is final and matches the branch. It rejects an existing tag, creates and pushes `vX.Y.Z`, and creates the GitHub release with generated notes.

After the tag exists, it opens two independent PRs:

1. `webpage/release-vX.Y.Z` updates the website release markers and targets `webpage/main`.
2. `post-release/vX.Y.Z-dev` changes `pyproject.toml`, `src/sbx/__init__.py`, and `uv.lock` to `X.Y.(Z+1).dev0` and targets `main`.

The development PR must contain exactly three insertions and three deletions. It does not update the README.

If either generated branch already exists, that PR step exits without replacing it.

## Website release PR guard

`webpage/main` owns `.github/workflows/webpage-release-pr-check.yml`. It validates only `webpage/release-v*` PRs to `webpage/main`, allows only `index.html`, and requires exactly two matching markers:

```html
<!-- sbx-release-version -->vX.Y.Z<!-- /sbx-release-version -->
```

The matching `vX.Y.Z` tag must already exist. Therefore the website cannot advertise a release before the package release is published.

## Security and protection

PR creation uses `RELEASE_PR_TOKEN` because the default `GITHUB_TOKEN` cannot create pull requests in this repository. The token needs only repository contents and pull-request access and must not bypass protected branch rules.

Release and website changes always land through PRs. Publishing is gated by revalidation of the merged release change rather than trusting a branch name alone.
