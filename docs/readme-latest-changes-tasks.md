# README latest changes tasks

## Implementation worktree

Use:

```bash
/home/nueces/code/sbx/feature/readme-latest-changes
```

## Tasks

1. [x] Update install guidance
   - Remove the separate SmolVM install command; `uv tool install --editable .` installs `sbx` dependencies.
   - Delete the obsolete unsupported-newer-SmolVM warning.

2. [x] Update mount docs
   - Show bare extra mounts using same-path behavior.
   - Keep one explicit `HOST:GUEST` example.
   - Note that `project_path` is mounted first and sets the attached working directory.

3. [x] Add release workflow note
   - Briefly document the manual release action: input `x.y.z`, bumps version files, creates tag/release `vX.Y.Z`.

4. [x] Check docs
   - Run a grep for stale `0.0.19` references in `README.md`.

## Done when

- README matches current SmolVM version and mount behavior.
- README mentions the manual release workflow.
