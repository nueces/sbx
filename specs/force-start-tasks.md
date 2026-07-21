# Remove `--force-start` tasks

- [ ] Remove `--force-start` from `sbx run` argparse options.
- [ ] Remove `--force-start` from `sbx shell` argparse options.
- [ ] Remove `--force-start` from shell completions.
- [ ] Remove `force_start` handling from `_start_existing_vm_if_needed(...)`.
- [ ] Ensure `error` VMs always refuse direct `run`/`shell` start and recommend `sbx doctor --fix`.
- [ ] Keep SmolVM error-state repair in `sbx doctor --fix` only.
- [ ] Update tests that currently expect forced retry.
- [ ] Add/keep tests proving `run`/`shell` reject `--force-start` as an unknown option.
- [ ] Run `ruff check src tests`.
- [ ] Run `pytest --no-cov`.
