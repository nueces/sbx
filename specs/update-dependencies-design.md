# Update dependencies design

## Goal

Update `sbx` to use the latest stable available versions of its Python dependencies and keep the existing test suite passing.

Latest versions checked on PyPI:

- `smolvm 0.0.26`
- `tomli 2.4.1`
- `pytest 9.1.1`
- `pytest-cov 7.1.0`
- `pre-commit 4.6.0`
- `ruff 0.15.21`

## Scope

In scope:

- Update runtime dependency constraints in `pyproject.toml`.
- Update development dependency minimums in `pyproject.toml`.
- Regenerate `uv.lock` with upgraded transitive dependencies.
- Run the existing tests and lint checks.

Out of scope:

- Adding new dependencies.
- Refactoring application code unless a dependency update requires it.
- Changing supported Python versions.

## Design

Use the package manager's normal upgrade path:

```sh
uv lock --upgrade
uv run --extra dev pytest
uv run --extra dev ruff check .
uv build --wheel
uv tool install --reinstall .
```

Keep the dependency model unchanged: `smolvm` stays pinned, `tomli` stays conditional for Python `<3.11`, and development tools stay lower-bounded.

Do not force-include package resource directories that already live under `src/sbx`; Hatch includes them through the package target, and force-including them again adds duplicate wheel paths.

For env sync, inspect the persisted SmolVM config first. If `comm_channel` is missing, treat it as an old SSH-capable VM and update SmolVM's managed env file directly over the existing SSH command path. This avoids SmolVM's slow failed vsock probe and its repeated SSH endpoint probing. If a channel is configured, use SmolVM's default path and keep the SSH retry only for SmolVM's old-VM recovery hint.

Before `sbx shell` syncs env vars, start an existing stopped VM. Env sync needs SSH/control readiness; syncing before start turns a normal stopped VM into a 30-second timeout.
