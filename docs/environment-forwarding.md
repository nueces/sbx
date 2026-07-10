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

That means this works without recreating the VM:

```bash
export OPENAI_API_KEY=old
sbx run my-vm

export OPENAI_API_KEY=new
sbx shell my-vm   # new shell sees OPENAI_API_KEY=new
sbx run my-vm     # new agent process sees OPENAI_API_KEY=new
```

If a configured name is missing from the host environment, `sbx` unsets it in the guest so stale values do not linger.

## Limits

- `sbx shell` reads only `[sbx].env`; it does not have a `--env` flag.
- Updates apply to newly attached processes only. Already-running `pi`, `claude`, `codex`, shells, or child processes keep the environment they started with.
- Values are stored in SmolVM's guest-managed environment. This is convenience forwarding, not a secret broker.
- `copy_host_credentials = false` still prevents broad credential/config copying; `env` is the explicit allowlist for selected host variables.
- If sync fails, `sbx` stops before attach rather than starting with stale credentials.
