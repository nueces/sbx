# sbx CLI UX simplification

Status: implemented in `feature/cli-ux-simplification`; curated-image default-user follow-up pending commit.

## Goal

Make the common `sbx` workflow easier to discover without removing advanced VM, image, or networking capabilities.

The preferred curated workflow is:

```bash
sbx image build
cd project
sbx run the-quest \
  --image '~/.smolvm/images/sbx' \
  --run-user agent \
  --project-path . \
  --writable-mounts

# Later runs use the generated .sbx.toml.
sbx run
```

## Current strengths

- Commands describe user intent rather than raw SmolVM operations.
- Project-local `.sbx.toml` makes sandbox names optional after initial setup.
- Destructive commands require confirmation.
- Advanced image and networking operations are already grouped.
- Credential forwarding defaults are conservative.

## Current problems

### Shared start options are too broad

`run`, `create`, and `recreate` share nearly thirty options. This exposes session flags on commands that do not attach, including `--attach`, `--stop-on-exit`, and Git/session setup options. Some are overwritten or have no useful effect.

### Sandbox name has overlapping inputs

A name can come from positional `NAME`, `--name`, or `[sbx].name`. Supplying both CLI forms silently gives `--name` precedence.

### JSON output is inconsistent

`run/create/recreate --json` produces JSON only on some creation paths and may still attach interactively. Image listing supports JSON, while the more useful `sbx ls` and `network status` do not.

### Listing hides stopped sandboxes

`sbx ls` lists only running VMs unless `--all` is supplied, making stopped sandboxes easy to overlook.

### Port forwarding has an ambiguous positional form

`sbx network forward [NAME] SPEC...` guesses whether the first value is a VM name or a port specification. Numeric VM names cannot be selected reliably this way.

### Aliases add visible surface area

Both `remove`/`rm` and `list`/`ls` appear in help and completion. This duplication was reviewed and accepted. Image listing follows the same visible `image list`/`image ls` convention.

## Proposed command model

Keep the existing hierarchy:

```text
Daily
  sbx run [NAME]
  sbx shell [NAME]
  sbx ls
  sbx stop [NAME]
  sbx rm [NAME]
  sbx recreate [NAME]

Provisioning
  sbx create [NAME]

Diagnostics
  sbx doctor

Advanced
  sbx network ...
  sbx image build
  sbx image list / sbx image ls
  sbx completion ...
```

Do not add another namespace or a required setup command.

## Proposed changes

1. Keep `run --no-attach` as the way to start or create a VM without opening an agent session. **Decision: accepted with modifications.**
   - Remove the redundant positive `--attach` flag.
   - Do not add a separate `start` command.
   - Do not otherwise split the shared `run`, `create`, and `recreate` option sets as part of this proposal.
2. Make positional `NAME` canonical. **Decision: accepted.**
   - Remove `--name` immediately; do not add a deprecation period.
3. Define a consistent machine-output contract. **Decision: accepted.**
   - Keep `--json` on `run`, `create`, and `recreate`.
   - Require `run --json` to be combined with `--no-attach`.
   - Emit only valid JSON on stdout on every successful JSON path; send diagnostics to stderr.
   - Add `sbx ls --json` and `sbx network status --json`.
4. Make `sbx ls` show all sandboxes by default and offer `--running` as the filter. **Decision: accepted with modification.**
   - Remove `--all` instead of retaining it as a compatibility option.
5. Change port forwarding selection to the following unambiguous forms. **Decision: accepted.**

   ```bash
   sbx network forward SPEC...
   sbx network forward --name OTHER_VM SPEC...
   ```

   Without `--name`, use `[sbx].name`; remove positional VM-name detection.

6. Continue showing both `remove`/`rm` and `list`/`ls` in help and completion. **Decision: canonicalizing these aliases was rejected.**
7. Reduce and group `run --help` rather than presenting one flat option list. **Decision: accepted.**
   - Keep `--write-config`; remove `--no-write-config`. **Decision: accepted.**
   - Make auth, credential-copying, and Git-forwarding policy configuration-only. **Decision: accepted.**
     - Remove `--auth-port`, `--no-auth-port`, `--auth-host-port`, and `--auth-guest-port` from `run`, `create`, and `recreate`.
     - Remove `--copy-host-credentials` and `--no-copy-host-credentials` from those commands.
     - Remove `--git-config` and `--no-git-config` from `run`, `create`, `recreate`, and `shell`.
     - Retain `[sbx].auth_port`, auth port numbers, `[sbx].copy_host_credentials`, and `[sbx].git_config` so recreation is reproducible.
     - Keep credential copying disabled in generated config and warn during provisioning when it is enabled.
     - Retain manual one-off OAuth tunnel management under `sbx network`.
   - Remove `--os` from `run`, `create`, and `recreate`; retain `[sbx].os` for advanced configuration. **Decision: accepted.**
   - Continue automatically writing minimal config when a new VM is created without `.sbx.toml`.
   - For an existing VM or config, write/add missing values only when `--write-config` is supplied; never overwrite existing values.
8. Improve first-run output after automatic `.sbx.toml` creation with concise next-step commands shown only on initial project creation. **Decision: accepted.**
9. Keep `sbx image ls` and add the visible `sbx image list` alias for consistency with top-level listing. **Decision: accepted.**
10. Replace the exhaustive README reference with a friendly guide centered on the curated image, first project creation, durable `.sbx.toml`, adding mounts after creation, and installing extra tools through `sbx shell`. Keep full detail in focused docs and command help. **Decision: accepted.**
11. Make the curated image declare `run_user = "agent"` in its manifest. Use that value only when neither CLI nor configuration selects a user, and persist the effective user in generated `.sbx.toml`. Do not make `agent` the global default for generic presets or custom images. **Decision: accepted.**
12. Keep `--project-path .` explicit on first creation because it grants the VM writable access to a host directory. Persist it in generated `.sbx.toml` so later commands remain short. **Decision: accepted.**

## Configuration wizard decision

**Decision: accepted.** Do not implement the current large interactive wizard by default. `sbx run` already bootstraps a minimal project config, while the proposed wizard asks many questions whose answers are existing defaults.

If explicit initialization is later proven necessary, start with one deterministic, non-interactive `sbx init` that writes a minimal config. Add a wizard only after user feedback shows that editing TOML is a recurring blocker.

## Rollout

**Decision: accepted with no deprecation period.**

1. Remove the rejected commands and flags directly; retain `run --no-attach`.
2. Implement the accepted JSON, listing, forwarding, help, and first-run behavior.
3. Update package documentation, examples, completion scripts, and website content in the same change wherever affected.
4. Do not retain hidden flag compatibility or print deprecation warnings.

Removed syntax is not a permanent behavior contract. Do not keep tests that enumerate deleted flags or completion entries solely to prove their absence; argparse already rejects unknown options. Retain tests for supported commands, help/completion surfaces, and changed behavior such as JSON, listing, forwarding, configuration, and first-run guidance.

## Acceptance criteria

- The default help emphasizes `run`, `shell`, `ls`, `stop`, and `rm`.
- `--attach` is removed while `run --no-attach` remains supported.
- Positional `NAME` is the only name input for `run`, `create`, and `recreate`; their `--name` option is unsupported.
- Every supported `--json` path emits only valid JSON with a documented schema.
- `sbx ls` shows stopped VMs without requiring an option.
- Port-forward VM selection is unambiguous.
- Top-level listing exposes `list`/`ls`, removal exposes `remove`/`rm`, and image listing exposes `image list`/`image ls`.
- First-run use still requires no initialization command.
- The README leads with the curated image and project workflow, including the stop/run cycle required to add mounts to an existing sandbox.
- The curated image defaults to guest user `agent`, while generic presets and manifests without `sbx.run_user` preserve the existing root behavior.
- Host project mounting remains an explicit first-run choice through `--project-path`.
