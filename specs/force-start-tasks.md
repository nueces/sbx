# Remove `--force-start` tasks

- [x] Remove `--force-start` from `sbx run` argparse options.
- [x] Remove `--force-start` from `sbx shell` argparse options.
- [x] Remove `--force-start` from shell completions.
- [x] Remove `force_start` handling from `_start_existing_vm_if_needed(...)`.
- [x] Ensure `error` VMs always refuse direct `run`/`shell` start and recommend `sbx doctor --fix`.
- [x] Keep SmolVM error-state repair in `sbx doctor --fix` only.
- [x] Update tests that currently expect forced retry.
- [x] Add/keep tests proving `run`/`shell` reject `--force-start` as an unknown option.
- [x] Run `ruff check src tests`.
- [x] Run `pytest --no-cov`.
