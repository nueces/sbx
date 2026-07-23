# sbx CLI UX simplification implementation plan

## Source of truth

Implement the decisions in:

```text
specification/cli-ux-simplification/specs/cli-ux-simplification-design.md
```

Create implementation work in:

```text
/home/nueces/code/sbx/feature/cli-ux-simplification
branch: feature/cli-ux-simplification
```

If the website needs changes, use a matching website feature branch/worktree rather than editing `webpage/main` directly.

Do not edit or commit directly to `main`, `specification/main`, or `webpage/main`. Do not commit unless explicitly requested.

## Implementation approach

Keep the current `argparse` command hierarchy and command handlers. This feature does not need a parser framework, command registry, compatibility layer, or new command namespace.

The work has six seams:

1. remove rejected flags and reorganize help;
2. make JSON output deterministic;
3. change listing and forwarding semantics;
4. improve initial config output and credential-copy warnings;
5. expose consistent `list`/`ls` aliases for VM and image inventories; and
6. update completions and replace the exhaustive README with a curated workflow guide.

## CLI option changes

For `run`, `create`, and `recreate`:

- remove `--attach` but retain `--no-attach`;
- remove `--name`; use positional `NAME` and `[sbx].name`;
- remove `--no-write-config` but retain `--write-config`;
- remove `--os` but retain `[sbx].os`;
- remove all auth-port flags while retaining auth config keys;
- remove credential-copy flags while retaining `[sbx].copy_host_credentials`;
- remove Git-config flags while retaining `[sbx].git_config`; and
- retain `--json`, with `run --json` requiring `--no-attach`.

Also remove Git-config flags from `shell` so Git forwarding is configuration-only.

Do not remove unrelated `--name` options, such as `image build --name` or the accepted `network forward --name` selector.

Build help groups with standard `argparse` argument groups. Keep one shared start-option helper; do not create separate command classes or schemas.

## Effective start-option groups

```text
Session:
  --agent
  --run-user
  --env
  --no-attach
  --stop-on-exit / --keep-running

Workspace:
  --project-path
  --mount
  --writable-mounts

VM resources:
  --memory
  --cpus
  --disk-size
  --image
  --boot-timeout
  --install-timeout

Configuration and output:
  --write-config
  --json
```

The positional `NAME` remains outside these option groups.

## Name resolution

Use positional `NAME` directly for `run`, `create`, and `recreate`. If absent, continue resolving `[sbx].name`; if neither exists, preserve current generated-name behavior where supported or the current clear missing-name error where required.

Remove the old `name_arg`/`--name` precedence path rather than retaining a hidden alias.

`network forward` is the intentional exception:

```bash
sbx network forward SPEC...
sbx network forward --name OTHER_VM SPEC...
```

Its `--name` selects another existing VM and removes the current positional guessing heuristic.

## Curated-image defaults

Add `"run_user": "agent"` under `sbx` in manifests produced by `sbx image build`. When a VM is configured with a local image, resolve `run_user` in this order:

1. `--run-user`;
2. `[sbx].run_user`; then
3. manifest `sbx.run_user`.

Validate a manifest-provided user with the existing guest-user validation and copy the effective value into generated `.sbx.toml`. Reuse the manifest default for an existing VM when the manifest remains readable; existing-VM reuse must not fail only because an old image path or manifest is unavailable. A manifest without `sbx.run_user` keeps the existing root behavior, so generic presets and custom images remain unchanged.

Do not infer `project_path = "."` from the image or current directory. A project mount grants writable host access and remains explicit on first creation through `--project-path`; automatic config creation persists it for later runs.

## JSON contract

Errors remain human-readable on stderr with nonzero exit status; this feature does not add a JSON error schema.

Successful `run --no-attach --json`, `create --json`, and `recreate --json` emit exactly one object on stdout on both new and existing VM paths:

```json
{"vm":{"name":"project-sbx","status":"running"}}
```

Successful `sbx ls --json` emits a top-level array, matching `image list --json` / `image ls --json` style:

```json
[
  {
    "name": "project-sbx",
    "status": "stopped",
    "project": "/path/to/project",
    "image": "debian-sbx",
    "ssh_port": 2204
  }
]
```

Use `null`, not `"-"`, for unavailable JSON fields. Keep `"-"` only in the human table.

Successful `sbx network status --json` emits one object:

```json
{
  "name": "project-sbx",
  "status": "running",
  "backend": "qemu",
  "guest_ip": "10.0.2.15",
  "ssh_port": 2204,
  "port_forwards": [],
  "auth_callback": {
    "status": "inactive",
    "detail": null
  }
}
```

In JSON mode:

- reject `run --json` unless `--no-attach` is present;
- keep stdout free of human lifecycle, deletion, config-bootstrap, and next-step messages;
- send warnings and diagnostics to stderr; and
- ensure underlying start/delete calls do not leak successful human output to stdout.

Use one small result-rendering helper where it removes duplicated branches; do not introduce a generic output framework.

## Listing behavior

Change both `ls` and `list` to:

```bash
sbx ls             # all VMs
sbx ls --running   # running VMs only
sbx ls --json      # all VMs as JSON
```

Remove `-a` and `--all` immediately. Keep both `ls` and `list` visible and functional.

Build one structured row per VM, then render that same data as either a table or JSON to prevent format drift.

Keep `sbx image ls` and add `sbx image list` as an identical visible alias. Both forms support the existing human and `--json` output.

## Configuration and first-run behavior

Preserve these rules:

- creating a new VM with no `.sbx.toml` writes a minimal config automatically;
- an existing VM with no project config writes only with `--write-config`;
- an existing config is changed only with `--write-config`;
- config updates add missing values and never overwrite existing values.

When human-mode initial creation writes `.sbx.toml`, print concise next steps once:

```text
Created sandbox 'project-sbx'.
Wrote .sbx.toml.

Run agent:  sbx run
Open shell: sbx shell
Stop:       sbx stop
Remove:     sbx rm
```

Avoid printing the guidance when reusing a VM, updating an existing config, or producing JSON.

When `[sbx].copy_host_credentials = true` will be used while provisioning a preset-backed VM, print a clear warning to stderr. Do not warn for an existing VM or a local-image path where credential copying is not performed.

## Completion updates

Update the existing static bash, zsh, and fish generators; do not replace them in this feature.

- remove every deleted start/shell option;
- retain `--no-attach` and `--write-config`;
- replace `ls --all`/`-a` with `--running` and add `--json`;
- add `network forward --name` and `network status --json`;
- expose both `image list` and `image ls` with the same options;
- keep all accepted long and short command aliases visible; and
- keep path and enum completion behavior unchanged where still applicable.

## Documentation and website

Update current user-facing material, not historical implementation plans unless they are presented as current guidance.

At minimum inspect and update:

- `README.md`;
- `docs/ergonomics.md`;
- `docs/network-command-roadmap.md`;
- `docs/git-config-forwarding.md`;
- `docs/environment-forwarding.md` if examples or option tables are affected;
- shell-completion examples/tests; and
- `webpage/main/index.html` or its matching website feature worktree if it mentions changed behavior.

Make the README a friendly guide rather than a complete reference. Lead with installation, `sbx image build`, first project creation with the curated image, subsequent short commands, adding mounts through `.sbx.toml` plus `sbx stop`/`sbx run`, and installing extra tools through `sbx shell`. The curated first-run command omits `--run-user agent` because the image manifest supplies it, but keeps `--project-path .` explicit. Explain that VM-disk changes survive stop/start but not `recreate` or `rm`; note that the curated image includes Pi, OpenCode launches from a shell, and npm agent commands should follow vendor-supported installation flags. Link focused docs and `sbx --help` for complete details.

Document security policy as reproducible `.sbx.toml` configuration. Remove examples for deleted flags and show the new list, forwarding, JSON, and first-run behavior.

## Validation strategy

Use existing pytest fixtures and fake SmolVM/SSH runners. Tests must not start QEMU, SSH, Docker, or real tunnels.

Keep tests for the supported CLI contract and meaningful behavior changes. Do not retain transition-only tests that enumerate removed options or completion entries: generic argparse rejection already covers unknown syntax, and those tests would freeze deleted history. Help and completion tests should positively assert the current supported surface rather than maintain a blacklist of former flags.

Run focused tests first, then the full suite and lint using an external temporary environment:

```bash
cd /home/nueces/code/sbx/feature/cli-ux-simplification
UV_PROJECT_ENVIRONMENT=/tmp/sbx-cli-ux-venv \
  uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_cli.py tests/test_completion.py

UV_PROJECT_ENVIRONMENT=/tmp/sbx-cli-ux-venv \
  uv run --python /usr/bin/python3 --extra dev pytest --no-cov

UV_PROJECT_ENVIRONMENT=/tmp/sbx-cli-ux-venv \
  uv run --python /usr/bin/python3 --extra dev ruff check .

UV_PROJECT_ENVIRONMENT=/tmp/sbx-cli-ux-venv \
  uv run --python /usr/bin/python3 --extra dev ruff format --check .
```

## Non-goals

- no `start` command;
- no config wizard or `init` command;
- no new CLI framework or completion dependency;
- no hidden compatibility aliases or deprecation warnings;
- no JSON error schema;
- no changes to the underlying VM lifecycle model; and
- no unrelated config, image-build internals, networking, or website redesign.
