# Environment variable forwarding

`sbx` manages only destinations you explicitly select. Configuration supports
host forwarding, harmless literal values, and renamed host references:

```toml
[sbx]
env = [
  "OPENAI_API_KEY",             # Copy host OPENAI_API_KEY to the same guest name.
  "APP_MODE=development",       # Literal value, independent of the host value.
  "SERVICE_TOKEN=${HOST_TOKEN}", # Copy host HOST_TOKEN to guest SERVICE_TOKEN.
  "EMPTY=",                     # Set an empty string; do not unset it.
]
```

**Do not put secrets in literal assignments:** `.sbx.toml` is commonly committed.
Use host references for credentials, optionally populated by tools such as
`op run -- sbx run` or `doppler run -- sbx run`.

The first `=` separates the destination from its value; later `=` characters and
literal whitespace are preserved. Names must match `[A-Za-z_][A-Za-z0-9_]*`.
Destinations must be unique across all forms, including repeated bare names.
References are expanded once against the host process, not other entries or the
guest. A missing source unsets the destination; an empty source sets an empty value.

Only a whole-value `${SOURCE}` reference is supported. Partial interpolation,
default/required operators, and malformed `${` expressions are errors. To forward
literal `${...}` text, put it in a host variable and reference that variable; host
values are not expanded recursively. `$HOME`, `$(command)`, and provider URIs are
literal text, not expressions or commands evaluated by sbx.

For CLI host-name selection (including `sbx run`):

```bash
sbx run --env OPENAI_API_KEY
```

`--env` remains name-only and repeatable; assignments are not accepted. It replaces
rather than merges the configured selection for that invocation. Repeated CLI
names remain accepted. `sbx shell` reads configuration only and has no `--env` flag.

## Validation and compatibility

Each loaded configuration file is validated before merging, even if a later file
or CLI selection would replace its env entries. Errors identify the file,
`[sbx].env`, the entry index when applicable, and the reason without echoing values.
Host references are not resolved during file loading; missing host values are not
configuration errors. A single string remains accepted in place of the array.

Existing name-only configurations continue to work except duplicate destinations,
which now fail instead of being silently redundant. NUL is not permitted in values.
Older sbx versions reject assignments; restore name-only entries before downgrading.

Recreation still deletes then creates; there is no rollback if creation fails.
Correct the reported issue and retry creation. Environment updates also are not
transactional: set may succeed before unset fails, and retrying reapplies the
selection. A sync failure prevents attachment.

## When values are updated

During boot/provisioning and before normal `sbx run` or `sbx shell` attachment,
`sbx` synchronizes resolved values into SmolVM's guest-managed environment. This
also applies to creation and no-attach paths at their existing setup points.
Host values are resolved once per invocation, before preset provisioning changes
the host process environment; each new invocation reads current host values.
Diagnostic `sbx shell --root` skips synchronization and normal boot setup.

Updates do not require recreating the VM. SBX publishes values in the guest's `/etc/profile.d/` environment; the packaged shell is responsible for loading system profiles. SBX does not source them manually or add per-shell adapters. For example:

```bash
export OPENAI_API_KEY=old
sbx run my-vm

export OPENAI_API_KEY=new
sbx shell my-vm   # updated guest environment is available to native shell startup
sbx run my-vm     # loading follows the guest shell's remote-command semantics
```

If a configured host source is missing, `sbx` unsets its guest destination so stale
values do not linger. Removing an entry from the selection does **not** unset its
previously persisted value; unselected managed variables are left alone.

## Limits

- `sbx shell` reads only `[sbx].env`; it does not have a `--env` flag.
- SSH remote agent commands are non-interactive and not necessarily login shells. Availability in a process follows native shell/image setup; SBX does not promise every invocation loads login profiles.
- Updates apply to newly attached processes only. Already-running `pi`, `claude`, `codex`, shells, or child processes keep the environment they started with.
- Values persist in SmolVM's guest-managed environment. Host references do not
  provide session-scoped secrecy, and unsetting is not guaranteed erasure of all
  copies. This is convenience forwarding, not a secret broker.
- SmolVM 0.0.28's Linux SSH environment reader does not reliably round-trip
  multiline values: later updates can lose/reinterpret them, and malformed-line
  warnings may expose value fragments. This existing dependency issue is tracked
  separately; sbx does not patch it or add newline restrictions in this feature.
  Do not rely on multiline persistence or assume upstream logs are secret-safe.
- `copy_host_credentials = false` still prevents broad credential/config copying; `env` is the explicit allowlist for selected host variables.
- If sync fails, `sbx` stops before attach rather than starting with stale credentials.
