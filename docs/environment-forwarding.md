# Environment variable forwarding

`sbx` forwards only environment variables you explicitly allow:

```toml
[sbx]
env = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
```

or for `sbx run` only:

```bash
sbx run --env OPENAI_API_KEY
```

## When values are updated

Before `sbx run` or `sbx shell` attaches to a VM, `sbx` syncs the configured names from the current host process environment into SmolVM's guest-managed environment.

Updates do not require recreating the VM. SBX publishes values in the guest's `/etc/profile.d/` environment; the packaged shell is responsible for loading system profiles. SBX does not source them manually or add per-shell adapters. For example:

```bash
export OPENAI_API_KEY=old
sbx run my-vm

export OPENAI_API_KEY=new
sbx shell my-vm   # updated guest environment is available to native shell startup
sbx run my-vm     # the agent inherits exports from native login startup
```

If a configured name is missing from the host environment, `sbx` unsets it in the guest so stale values do not linger.

## Limits

- `sbx shell` reads only `[sbx].env`; it does not have a `--env` flag.
- Agent launch uses the guest-selected non-interactive login shell. Managed exports load through native system-profile integration; custom images must provide that integration. Bash is the supported guest shell; Zsh checks were exploratory and do not establish support. SBX does not source files manually.
- Bash selects the first readable `.bash_profile`, `.bash_login`, or `.profile` after `/etc/profile`. Only exported values from executed startup branches are inherited. Interactive-only branches are not guaranteed to run; user profiles may override managed exports.
- Login startup runs on agent launch, not as a separate availability check. `--no-attach` does not validate the agent. Profiles can add output/startup time and must allow the launch payload to execute; an early `exit 0` or `exec` can bypass it. Arbitrary profile output is not redacted, so do not print secrets.
- Updates apply to newly attached processes only. Already-running `pi`, `claude`, `codex`, shells, or child processes keep the environment they started with.
- Values are stored in SmolVM's guest-managed environment. This is convenience forwarding, not a secret broker.
- `copy_host_credentials = false` still prevents broad credential/config copying; `env` is the explicit allowlist for selected host variables.
- If sync fails, `sbx` stops before attach rather than starting with stale credentials.
