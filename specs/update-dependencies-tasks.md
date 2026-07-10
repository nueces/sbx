# Update dependencies tasks

- [x] Create `specification/update-dependencies` and `feature/update-dependencies` worktrees.
- [x] Check latest stable versions on PyPI.
- [x] Update `pyproject.toml` dependency constraints.
- [x] Regenerate `uv.lock` with `uv lock --upgrade`.
- [x] Run `uv run --extra dev pytest`.
- [x] Run `uv run --extra dev ruff check .`.
- [x] Add SSH-control retry for env sync on old SmolVM VMs.
- [x] Start existing stopped VMs before `sbx shell` env sync.
- [x] Run `uv build --wheel`.
- [x] Run `uv tool install --reinstall --force .`.
