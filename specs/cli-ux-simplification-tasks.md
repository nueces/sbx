# sbx CLI UX simplification implementation tasks

## Implementation status

Implemented in `feature/cli-ux-simplification`; curated-image default-user follow-up pending commit.

Latest validation:

- full suite: 207 passed;
- coverage: 91.64% (90% required);
- Ruff check and `git diff --check`: passed;
- full format check: only pre-existing `main` drift remains in `.github/scripts/generate_complexity_report.py`, `tests/test_build_debian_image.py`, `tests/test_check_image_build_inputs.py`, and `tests/test_smolvm_preset.py`; and
- website review: no changed flag or behavior is described there, so no website branch/diff was needed.

## Source of truth

Implement against:

```text
specification/cli-ux-simplification/specs/cli-ux-simplification-design.md
specification/cli-ux-simplification/specs/cli-ux-simplification-plan.md
```

Implementation belongs in a matching implementation worktree:

```text
/home/nueces/code/sbx/feature/cli-ux-simplification
branch: feature/cli-ux-simplification
```

Website changes, if required, belong in a matching website feature branch/worktree. Specification changes remain in `specification/cli-ux-simplification`.

Do not edit or commit directly to `main`, `specification/main`, or `webpage/main`. Do not commit unless explicitly requested.

## Phase 0 — prepare and baseline

### T001 — Create and verify the implementation worktree

- [x] Create `feature/cli-ux-simplification` from current `main` at `feature/cli-ux-simplification/` if it does not exist.
- [x] Confirm the path mirrors branch `feature/cli-ux-simplification`.
- [x] Read the workspace and implementation-worktree `AGENTS.md` files.
- [x] Confirm the implementation worktree is clean before editing.
- [x] Do not mix specification files into the implementation branch.

### T002 — Run the pre-change baseline

- [x] Run the full test suite with the external `/tmp/sbx-cli-ux-venv` environment documented in the plan.
- [x] Run Ruff check and format check.
- [x] Record pre-existing failures rather than fixing unrelated code.

### T003 — Inventory every affected reference

- [x] Search source, tests, completions, README, current docs, and website for:
  - `--attach` and `--no-attach`;
  - start-command `--name` and `name_arg`;
  - `--write-config` and `--no-write-config`;
  - start-command `--os`;
  - auth, credential-copy, and Git-config flags;
  - `--all`, `-a`, and VM-list filtering;
  - `network forward [NAME]` parsing;
  - start/list/network JSON output; and
  - first-run config output.
- [x] Distinguish unrelated accepted options such as `image build --name` and `network forward --name` from the removed start-command `--name`.
- [x] Identify current website text that actually needs changing before creating a website feature branch.

## Phase 1 — simplify parser options and help

### T004 — Remove rejected start options

- [x] Remove `--attach` while retaining `--no-attach` with the existing attach destination/default behavior.
- [x] Remove start-command `--name`; make positional `NAME` the direct parser destination used by `run`, `create`, and `recreate`.
- [x] Remove `--no-write-config` while retaining `--write-config` with a tri-state-compatible default.
- [x] Remove start-command `--os`; continue reading `[sbx].os` and its current default.
- [x] Remove start-command auth-port and credential-copy flags.
- [x] Remove Git-config flags from `run`, `create`, `recreate`, and `shell`.
- [x] Do not add hidden aliases, deprecation warnings, or a `start` command.

### T005 — Add standard argparse help groups

- [x] Organize the shared start parser exactly into:
  - Session;
  - Workspace;
  - VM resources; and
  - Configuration and output.
- [x] Keep positional `NAME` visible and optional.
- [x] Keep `--no-attach`, `--write-config`, and every accepted option.
- [x] Preserve both visible command aliases: `list`/`ls` and `remove`/`rm`.
- [x] Add focused help-output assertions for the groups and supported options without snapshotting irrelevant argparse spacing or enumerating removed options.

### T006 — Preserve configuration-driven behavior

- [x] Verify `[sbx].os` still controls preset provisioning.
- [x] Verify `[sbx].auth_port`, `auth_host_port`, and `auth_guest_port` still control automatic OAuth forwarding.
- [x] Verify `[sbx].copy_host_credentials` still controls preset credential copying.
- [x] Verify `[sbx].git_config` still controls both agent and shell Git forwarding.
- [x] Verify generated config keeps `copy_host_credentials = false` when that value is written.
- [x] Add a stderr warning only when `copy_host_credentials = true` is about to provision a preset-backed VM.
- [x] Verify the warning is absent for an existing VM and a local-image path where credentials are not copied.
- [x] Add `sbx.run_user = "agent"` to manifests produced by `sbx image build`.
- [x] Use manifest `sbx.run_user` only when CLI and config omit `run_user`, validating it with the existing guest-user rules.
- [x] Persist a manifest-selected user in automatically generated `.sbx.toml`.
- [x] Keep root behavior for generic presets and custom manifests without `sbx.run_user`.
- [x] Keep `project_path` explicit; do not infer a writable current-directory mount from the image.

### T007 — Preserve config-writing rules

- [x] Keep automatic minimal `.sbx.toml` creation for a newly created VM.
- [x] Keep existing VM/config writes opt-in through `--write-config`.
- [x] Keep updates additive: add missing values and never overwrite existing values.
- [x] Remove docs and transition-only tests for `--no-write-config`.
- [x] Add regression tests covering new VM, existing VM, existing config, and explicit `--write-config` behavior.

## Phase 2 — make machine output deterministic

### T008 — Add minimal structured render helpers

- [x] Add the smallest helpers needed to render:
  - one VM lifecycle result;
  - VM list rows; and
  - network status.
- [x] Reuse the same structured data for human and JSON renderers where practical.
- [x] Do not add a generic formatter class, serialization framework, or JSON error schema.
- [x] Keep unavailable JSON fields as `null`, while human tables may continue using `-`.

### T009 — Fix lifecycle JSON output

- [x] Reject `run --json` unless `--no-attach` is present, with a clear stderr usage error and exit code `2`.
- [x] Emit exactly `{"vm":{"name":...,"status":"running"}}` for successful `run --no-attach --json`, `create --json`, and `recreate --json`.
- [x] Cover both newly created and already-existing VM paths.
- [x] Suppress or redirect sbx-owned human start, reuse, delete, config, and guidance messages so stdout parses as one JSON value.
- [x] Ensure successful underlying start/delete operations do not leak human stdout in JSON mode.
- [x] Keep warnings and diagnostics on stderr.
- [x] Add `json.loads()` tests for every lifecycle command/path instead of string-only assertions.

### T010 — Add list JSON and change list defaults

- [x] Build structured list rows with `name`, `status`, `project`, `image`, and nullable `ssh_port`.
- [x] Make `sbx ls` and `sbx list` request all VMs by default.
- [x] Add `--running` to request only running VMs.
- [x] Remove `-a` and `--all` immediately.
- [x] Add `--json` with the top-level array schema in the plan.
- [x] Verify both aliases have identical filtering, table, and JSON behavior.

### T011 — Add network status JSON

- [x] Build one network-status object with the schema in the plan.
- [x] Preserve the current human status output.
- [x] Add `sbx network status --json`.
- [x] Represent no auth detail as `null` in JSON.
- [x] Cover active, inactive, and busy/untracked auth states with fakes.

## Phase 3 — remove forwarding ambiguity and improve first run

### T012 — Make `network forward` VM selection explicit

- [x] Replace the mixed `forward_args` parser with positional `SPEC...` plus optional `--name NAME`.
- [x] Without `--name`, resolve `[sbx].name` through the existing project-context helper.
- [x] Remove first-argument port/name guessing.
- [x] Preserve all three accepted forward-spec forms and multiple simultaneous specs.
- [x] Test configured-name selection, explicit `--name`, missing name, numeric VM names, invalid specs, and multiple specs.

### T013 — Add initial project guidance

- [x] Make config-bootstrap code report whether it created a new `.sbx.toml` without adding a new result abstraction.
- [x] On human-mode initial VM/config creation, print the accepted concise creation/config/next-step block.
- [x] Do not print guidance when reusing a VM, updating an existing config, or running in JSON mode.
- [x] Avoid duplicate `Started`/`Created` lines across preset and local-image creation paths.
- [x] Add focused output tests for shown and suppressed guidance.

## Phase 4 — update completions and documentation

### T014 — Update static completion generators

- [x] Remove all deleted start and shell options from bash, zsh, and fish completion.
- [x] Retain `--no-attach` and `--write-config`.
- [x] Replace `--all`/`-a` with `--running`; add list `--json`.
- [x] Add `network forward --name` and `network status --json`.
- [x] Add `image list` while retaining `image ls`; give both identical human/JSON behavior and completion.
- [x] Keep all accepted long and short command aliases visible.
- [x] Update completion tests for all three shells to assert the supported surface without keeping a blacklist of removed options.

### T015 — Update package documentation

- [x] Replace the exhaustive README reference with a friendly curated workflow: image build, first sandbox/config creation, daily commands, adding mounts with stop/run, and installing persistent per-VM tools through `sbx shell`.
- [x] Explain that the curated image contains Pi, OpenCode launches from a shell, and agent npm commands use vendor-supported install flags.
- [x] Remove `--run-user agent` from the curated first-run example after the manifest supplies it; retain explicit `--project-path .`.
- [x] Point complete command/config detail to `sbx --help`, `sbx.toml.example`, and focused docs.
- [x] Replace security CLI examples with reproducible `.sbx.toml` examples.
- [x] Update `docs/ergonomics.md` to match accepted name, list, config, and first-run behavior.
- [x] Update `docs/network-command-roadmap.md` for explicit forwarding `--name` and network-status JSON.
- [x] Update `docs/git-config-forwarding.md` to remove deleted CLI flags.
- [x] Inspect `docs/environment-forwarding.md` and other current usage docs; change only affected current guidance.
- [x] Do not rewrite historical plans merely because they mention old proposals.

### T016 — Update website only where required

- [x] If current website content describes changed flags or behavior, create a matching website feature branch/worktree.
- [x] Update only affected command examples and explanations.
- [x] Do not edit `webpage/main` directly.
- [x] If no current website text is affected, record that no website diff is needed and skip the branch.

## Phase 5 — regression and final validation

### T017 — Remove transition-only tests

- [x] Delete tests that enumerate removed flags or completion entries solely to prove their absence; unknown-option rejection is argparse behavior, not a retained product contract.
- [x] Keep positive tests for the supported parser, help, and completion surfaces.
- [x] Keep focused tests proving unrelated `image build --name` and `network forward --name` remain accepted where those tests protect current behavior rather than deleted history.

### T018 — Run focused regression tests

- [x] Run `tests/test_cli.py`, `tests/test_cli_extra.py`, and `tests/test_completion.py` with the external test environment.
- [x] Include tests for:
  - existing VM `run --no-attach` startup;
  - configuration-only security behavior;
  - lifecycle/list/network JSON parsing;
  - all/list filtering defaults;
  - explicit forwarding name selection; and
  - first-run guidance.

### T019 — Run full validation

- [x] Run the full pytest suite without coverage enforcement first.
- [x] Run the project-standard test command with coverage if required by current contributor guidance.
- [x] Run Ruff check.
- [x] Run Ruff format check.
- [x] Inspect `git diff --check`.
- [x] Search again for deleted flags in current source, completions, README, current docs, and affected website files.
- [x] Confirm any remaining matches are intentional historical text, not transition-only rejection tests.

### T020 — Final manual CLI review

- [x] Inspect `sbx --help`, `run --help`, `create --help`, `recreate --help`, `shell --help`, `ls --help`, `network forward --help`, `network status --help`, `image --help`, `image list --help`, and `image ls --help`.
- [x] Confirm the help groups are readable and every displayed option is supported.
- [x] Confirm both `list`/`ls` and `remove`/`rm` remain visible.
- [x] Confirm no removed option appears in generated completion.
- [x] Confirm JSON examples from the implementation plan match actual output.
- [x] Do not commit until explicitly requested.
