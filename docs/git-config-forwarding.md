# Git config forwarding

`sbx` copies a small, safe subset of the host's global Git configuration into the guest by default. This lets commits made inside the VM use the same author identity as the host without copying Git credentials or broader host configuration.

## Defaults

Git config forwarding is enabled by default:

```toml
[sbx]
git_config = true
```

Disable it reproducibly in project configuration:

```toml
[sbx]
git_config = false
```

## Copied keys

Only these global Git config keys are copied:

```text
user.name
user.email
init.defaultBranch
pull.rebase
push.default
core.autocrlf
core.eol
```

## Not copied

`sbx` does not copy:

- Git credentials
- SSH keys
- GPG/signing keys
- credential helpers
- `include.*` or `includeIf.*`
- `url.*.insteadOf` rewrite rules
- host environment variables

Commit signing is not configured or disabled by this feature. If signing is off on the host, commits inside the VM remain unsigned. If signing needs to work inside the VM, configure that explicitly inside the VM in a future/manual workflow.

## Where it is installed

During the selected user's first successful provisioning, `sbx` creates `.gitconfig` only when the destination is absent. Ordinary runs and later reprovisioning never overwrite or restore it.

For example:

```toml
[sbx]
run_user = "developer"
```

`sbx` writes:

```text
/home/developer/.gitconfig
```

This is guest-internal configuration only and is independent from `copy_host_credentials`. `sbx shell --root` does not write `/root/.gitconfig`.
