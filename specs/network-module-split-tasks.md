# Network module split tasks

- [x] Create `src/sbx/constants.py` for shared state/backend constants.
- [x] Create `src/sbx/runtime.py` for shared process, SSH, JSON, PID, and error helpers.
- [x] Create `src/sbx/config.py` for tiny config access and VM-name resolution helpers.
- [x] Move network command implementations/helpers into `src/sbx/network.py`.
- [x] Keep argparse parser construction in `src/sbx/cli.py`, wired to `sbx.network` command functions.
- [x] Update `cli.py` to use `network.port_forwards_from_specs` for startup config.
- [x] Update tests that import moved internals.
- [x] Run `ruff check .` and `pytest --no-cov`.
