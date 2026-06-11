# Project organization for sbx, Pi, and spec-kit

This workflow uses a host-side working directory as the orchestration root. `sbx` is started from that working directory, then Pi/spec-kit create feature worktrees under it.

## Directory model

Example:

```text
working-dir/
├── .pi/                         # Pi project-local packages/settings for this working area
├── main/                        # main/master clone of the project repo
├── specs/                       # optional specification worktree/area
└── nueces/
    └── 0000-feature-name/       # feature worktree created later by spec-kit
```

Important: the feature worktree usually does **not** exist when `sbx run` starts. Pi starts in `working-dir`, loads project-local resources from `working-dir/.pi`, and then spec-kit creates the feature directory/worktree as part of the workflow.

## What belongs in the VM image

The base image should contain global runtime/tooling requirements that are useful before any specific feature worktree exists:

- Node.js/npm
- Pi CLI
- uv
- spec-kit CLI (`specify-cli`)
- git/curl/ripgrep/sudo and similar base tools

These are installed in the image because they should survive VM destruction and be available immediately on VM startup.

## What belongs in `working-dir/.pi`

Pi packages/extensions/skills that are specific to this working area should be installed project-locally in:

```text
working-dir/.pi/
```

For example, install `pi-lens` from the working directory:

```bash
cd working-dir
pi install -l npm:pi-lens
```

The `-l` flag writes to project-local Pi settings/package directories under `.pi/`. Because `working-dir` is mounted from the host, this persists even if the VM is destroyed.

This keeps `pi-lens` scoped to the working area where Pi is launched, without installing it globally for every project and without requiring the future feature worktree to exist beforehand.

## What does not belong in `main/`

The main/master clone does not need Pi state or Pi package installs for this workflow. Avoid writing `.pi/` into `main/` unless that repository intentionally wants tracked Pi resources.

## Git tracking

If `working-dir` is not itself a git repository, `working-dir/.pi` cannot be accidentally committed to the project repo.

If `.pi/` is ever created inside a git worktree and should remain local-only, prefer that worktree's local exclude file instead of changing committed `.gitignore`:

```bash
echo ".pi/" >> .git/info/exclude
```

## sbx configuration

Run `sbx` from the working directory and mount that directory:

```toml
[sbx]
project_path = "."
run_user = "agent"
```

Then:

```bash
cd working-dir
sbx run
```

Pi starts in `working-dir`, discovers `working-dir/.pi`, and spec-kit can create feature worktrees below that directory.
