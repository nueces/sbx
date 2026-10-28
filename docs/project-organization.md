# Project organization for sbx and Pi

This workflow uses a host-side working directory as the orchestration root. Configure SBX there, then invoke it from that directory or any nested worktree. Nearest-ancestor `.sbx.toml` discovery keeps those invocations associated with the same project.

## Directory model

Example:

```text
working-dir/
├── .pi/                         # Pi project-local packages/settings for this working area
├── main/                        # main/master clone of the project repo
├── specs/                       # optional specification worktree/area
└── nueces/
    └── 0000-feature-name/       # feature worktree created later by project tooling
```

Important: the feature worktree usually does **not** exist when `sbx run` starts. Pi starts in `working-dir`, loads project-local resources from `working-dir/.pi`, and optional tooling such as spec-kit can create the feature directory/worktree later.

## What belongs in the VM image

The base image should contain global runtime/tooling requirements that are useful before any specific feature worktree exists:

- Node.js/npm
- uv
- git/curl/ripgrep/sudo and similar base tools

These shared tools are installed in the image. Pi and Specify are optional runtime installations owned by the selected user; neither is baked into the reusable image.

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
runtime_install = ["pi", "specify"]
# run_user defaults to the current host username.
```

Then:

```bash
cd working-dir
sbx run
```

Pi starts in `working-dir`, discovers `working-dir/.pi`, and the optionally installed Specify CLI can create feature worktrees below that directory. From `working-dir/feature/example`, `sbx run` or `sbx shell` reuses the root config/mount and starts in that corresponding guest subdirectory. `project_path = "."` is relative to the config file, not the invoking subdirectory. User shell startup may change directory afterward.
