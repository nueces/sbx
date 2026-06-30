# README latest changes design

## Goal

Update `README.md` so it matches the current package behavior.

## Scope

- Install guidance should rely on `sbx`'s pinned SmolVM dependency; no separate manual SmolVM install command.
- Remove the obsolete warning that newer SmolVM is unsupported.
- Document mount behavior: bare host paths mount at the same absolute guest path; explicit `HOST:GUEST` stays explicit; `project_path` is first and starts the agent/shell there.
- Document the manual release workflow at a high level.

## Out of scope

- Full changelog.
- Duplicating GitHub Actions implementation details.
