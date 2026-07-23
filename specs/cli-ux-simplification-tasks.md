# sbx CLI UX simplification implementation tasks

## Implementation status

Implemented and committed in `feature/cli-ux-simplification`.

Validation completed after rebasing:

- full suite: 204 passed;
- coverage: 91.58% (90% required);
- Ruff check and `git diff --check`: passed;
- full format check: only pre-existing `main` drift remains in `.github/scripts/generate_complexity_report.py`, `src/sbx/cli.py`, `tests/test_build_debian_image.py`, `tests/test_check_image_build_inputs.py`, and `tests/test_smolvm_preset.py`; and
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

- [ ] Create `feature/cli-ux-simplification` from current `main` at `feature/cli-ux-simplification/` if it does not exist.
- [ ] Confirm the path mirrors branch `feature/cli-ux-simplification`.
- [ ] Read the workspace and implementation-worktree `AGENTS.md` files.
- [ ] Confirm the implementation worktree is clean before editing.
- [ ] Do not mix specification files into the implementation branch.

### T002 — Run the pre-change baseline

- [ ] Run the full test suite with the external `/tmp/sbx-cli-ux-venv` environment documented in the plan.
- [ ] Run Ruff check and format check.
- [ ] Record pre-existing failures rather than fixing unrelated code.

### T003 — Inventory every affected reference

- [ ] Search source, tests, completions, README, current docs, and website for:
  - `--attach` and `--no-attach`;
  - start-command `--name` and `name_arg`;
  - `--write-config` and `--no-write-config`;
  - start-command `--os`;
  - auth, credential-copy, and Git-config flags;
  - `--all`, `-a`, and VM-list filtering;
  - `network forward [NAME]` parsing;
  - start/list/network JSON output; and
  - first-run config output.
- [ ] Distinguish unrelated accepted options such as `image build-debian --name` and `network forward --name` from the removed start-command `--name`.
- [ ] Identify current website text that actually needs changing before creating a website feature branch.

## Phase 1 — simplify parser options and help

### T004 — Remove rejected start options

- [ ] Remove `--attach` while retaining `--no-attach` with the existing attach destination/default behavior.
- [ ] Remove start-command `--name`; make positional `NAME` the direct parser destination used by `run`, `create`, and `recreate`.
- [ ] Remove `--no-write-config` while retaining `--write-config` with a tri-state-compatible default.
- [ ] Remove start-command `--os`; continue reading `[sbx].os` and its current default.
- [ ] Remove start-command auth-port and credential-copy flags.
- [ ] Remove Git-config flags from `run`, `create`, `recreate`, and `shell`.
- [ ] Do not add hidden aliases, deprecation warnings, or a `start` command.

### T005 — Add standard argparse help groups

- [ ] Organize the shared start parser exactly into:
  - Session;
  - Workspace;
  - VM resources; and
  - Configuration and output.
- [ ] Keep positional `NAME` visible and optional.
- [ ] Keep `--no-attach`, `--write-config`, and every accepted option.
- [ ] Preserve both visible command aliases: `list`/`ls` and `remove`/`rm`.
- [ ] Add focused help-output assertions for the groups and supported options without snapshotting irrelevant argparse spacing or enumerating removed options.

### T006 — Preserve configuration-driven behavior

- [ ] Verify `[sbx].os` still controls preset provisioning.
- [ ] Verify `[sbx].auth_port`, `auth_host_port`, and `auth_guest_port` still control automatic OAuth forwarding.
- [ ] Verify `[sbx].copy_host_credentials` still controls preset credential copying.
- [ ] Verify `[sbx].git_config` still controls both agent and shell Git forwarding.
- [ ] Verify generated config keeps `copy_host_credentials = false` when that value is written.
- [ ] Add a stderr warning only when `copy_host_credentials = true` is about to provision a preset-backed VM.
- [ ] Verify the warning is absent for an existing VM and a local-image path where credentials are not copied.

### T007 — Preserve config-writing rules

- [ ] Keep automatic minimal `.sbx.toml` creation for a newly created VM.
- [ ] Keep existing VM/config writes opt-in through `--write-config`.
- [ ] Keep updates additive: add missing values and never overwrite existing values.
- [ ] Remove docs and transition-only tests for `--no-write-config`.
- [ ] Add regression tests covering new VM, existing VM, existing config, and explicit `--write-config` behavior.

## Phase 2 — make machine output deterministic

### T008 — Add minimal structured render helpers

- [ ] Add the smallest helpers needed to render:
  - one VM lifecycle result;
  - VM list rows; and
  - network status.
- [ ] Reuse the same structured data for human and JSON renderers where practical.
- [ ] Do not add a generic formatter class, serialization framework, or JSON error schema.
- [ ] Keep unavailable JSON fields as `null`, while human tables may continue using `-`.

### T009 — Fix lifecycle JSON output

- [ ] Reject `run --json` unless `--no-attach` is present, with a clear stderr usage error and exit code `2`.
- [ ] Emit exactly `{"vm":{"name":...,"status":"running"}}` for successful `run --no-attach --json`, `create --json`, and `recreate --json`.
- [ ] Cover both newly created and already-existing VM paths.
- [ ] Suppress or redirect sbx-owned human start, reuse, delete, config, and guidance messages so stdout parses as one JSON value.
- [ ] Ensure successful underlying start/delete operations do not leak human stdout in JSON mode.
- [ ] Keep warnings and diagnostics on stderr.
- [ ] Add `json.loads()` tests for every lifecycle command/path instead of string-only assertions.

### T010 — Add list JSON and change list defaults

- [ ] Build structured list rows with `name`, `status`, `project`, `image`, and nullable `ssh_port`.
- [ ] Make `sbx ls` and `sbx list` request all VMs by default.
- [ ] Add `--running` to request only running VMs.
- [ ] Remove `-a` and `--all` immediately.
- [ ] Add `--json` with the top-level array schema in the plan.
- [ ] Verify both aliases have identical filtering, table, and JSON behavior.

### T011 — Add network status JSON

- [ ] Build one network-status object with the schema in the plan.
- [ ] Preserve the current human status output.
- [ ] Add `sbx network status --json`.
- [ ] Represent no auth detail as `null` in JSON.
- [ ] Cover active, inactive, and busy/untracked auth states with fakes.

## Phase 3 — remove forwarding ambiguity and improve first run

### T012 — Make `network forward` VM selection explicit

- [ ] Replace the mixed `forward_args` parser with positional `SPEC...` plus optional `--name NAME`.
- [ ] Without `--name`, resolve `[sbx].name` through the existing project-context helper.
- [ ] Remove first-argument port/name guessing.
- [ ] Preserve all three accepted forward-spec forms and multiple simultaneous specs.
- [ ] Test configured-name selection, explicit `--name`, missing name, numeric VM names, invalid specs, and multiple specs.

### T013 — Add initial project guidance

- [ ] Make config-bootstrap code report whether it created a new `.sbx.toml` without adding a new result abstraction.
- [ ] On human-mode initial VM/config creation, print the accepted concise creation/config/next-step block.
- [ ] Do not print guidance when reusing a VM, updating an existing config, or running in JSON mode.
- [ ] Avoid duplicate `Started`/`Created` lines across preset and local-image creation paths.
- [ ] Add focused output tests for shown and suppressed guidance.

## Phase 4 — update completions and documentation

### T014 — Update static completion generators

- [ ] Remove all deleted start and shell options from bash, zsh, and fish completion.
- [ ] Retain `--no-attach` and `--write-config`.
- [ ] Replace `--all`/`-a` with `--running`; add list `--json`.
- [ ] Add `network forward --name` and `network status --json`.
- [ ] Keep both long and short command aliases visible.
- [ ] Update completion tests for all three shells to assert the supported surface without keeping a blacklist of removed options.

### T015 — Update package documentation

- [ ] Update `README.md` examples, command table, common options, config bootstrap text, and config reference.
- [ ] Replace security CLI examples with reproducible `.sbx.toml` examples.
- [ ] Update `docs/ergonomics.md` to match accepted name, list, config, and first-run behavior.
- [ ] Update `docs/network-command-roadmap.md` for explicit forwarding `--name` and network-status JSON.
- [ ] Update `docs/git-config-forwarding.md` to remove deleted CLI flags.
- [ ] Inspect `docs/environment-forwarding.md` and other current usage docs; change only affected current guidance.
- [ ] Do not rewrite historical plans merely because they mention old proposals.

### T016 — Update website only where required

- [ ] If current website content describes changed flags or behavior, create a matching website feature branch/worktree.
- [ ] Update only affected command examples and explanations.
- [ ] Do not edit `webpage/main` directly.
- [ ] If no current website text is affected, record that no website diff is needed and skip the branch.

## Phase 5 — regression and final validation

### T017 — Remove transition-only tests

- [x] Delete tests that enumerate removed flags or completion entries solely to prove their absence; unknown-option rejection is argparse behavior, not a retained product contract.
- [x] Keep positive tests for the supported parser, help, and completion surfaces.
- [x] Keep focused tests proving unrelated `image build-debian --name` and `network forward --name` remain accepted where those tests protect current behavior rather than deleted history.

### T018 — Run focused regression tests

- [ ] Run `tests/test_cli.py`, `tests/test_cli_extra.py`, and `tests/test_completion.py` with the external test environment.
- [ ] Include tests for:
  - existing VM `run --no-attach` startup;
  - configuration-only security behavior;
  - lifecycle/list/network JSON parsing;
  - all/list filtering defaults;
  - explicit forwarding name selection; and
  - first-run guidance.

### T019 — Run full validation

- [ ] Run the full pytest suite without coverage enforcement first.
- [ ] Run the project-standard test command with coverage if required by current contributor guidance.
- [ ] Run Ruff check.
- [ ] Run Ruff format check.
- [ ] Inspect `git diff --check`.
- [ ] Search again for deleted flags in current source, completions, README, current docs, and affected website files.
- [ ] Confirm any remaining matches are intentional historical text, not transition-only rejection tests.

### T020 — Final manual CLI review

- [ ] Inspect `sbx --help`, `run --help`, `create --help`, `recreate --help`, `shell --help`, `ls --help`, `network forward --help`, and `network status --help`.
- [ ] Confirm the help groups are readable and every displayed option is supported.
- [ ] Confirm both `list`/`ls` and `remove`/`rm` remain visible.
- [ ] Confirm no removed option appears in generated completion.
- [ ] Confirm JSON examples from the implementation plan match actual output.
- [ ] Do not commit until explicitly requested.
