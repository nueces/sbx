# Development

Use an ignored environment under `local/` in a fresh checkout so test dependencies are
reproducible and the worktree remains clean:

```bash
cd /path/to/sbx/main
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest --no-cov
```

## Writing tests

Keep fast tests directly under `tests/` and name modules and functions `test_*.py` and
`test_*`. Use pytest fixtures for shared setup, `tmp_path` for files, `monkeypatch` for
host state, and fake command runners for external tools. Cover changed behavior and its
error boundary without starting real QEMU, SSH, Docker, or SmolVM processes.

Tests that must orchestrate real system resources belong under `tests/integration/`.
They must declare and validate their prerequisites, use bounded waits, clean up every
process, server, socket, and local artifact even after failure, and fail rather than skip
when explicitly selected. Pytest excludes this directory from the default suite; CI
provisions its prerequisites and runs it on every pull request.

```bash
# Fast suite.
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest --no-cov

# Focused test while iterating.
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev pytest --no-cov tests/test_cli.py

# Linux integration suite after building the worker and test guest.
GO=go gateway/scripts/build_release_artifacts.sh
gateway/scripts/build_test_guest.sh
# Only the separate count-only observer is privileged; gateway processes are not.
OBSERVER_DIR=$(mktemp -d /tmp/sbx-observer.XXXXXX)
sudo chown root:root "$OBSERVER_DIR"
sudo chmod 0755 "$OBSERVER_DIR"
sudo install -m 0444 gateway/scripts/resource_usage.py "$OBSERVER_DIR/observer.py"
export SBX_GATEWAY_RESOURCE_OBSERVER="$OBSERVER_DIR/observer.py"
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev \
  pytest --no-cov -s tests/integration
sudo rm "$OBSERVER_DIR/observer.py"
sudo rmdir "$OBSERVER_DIR"
unset SBX_GATEWAY_RESOURCE_OBSERVER

# Mediator tests.
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev \
  pytest --no-cov gateway/mediator

# Lint and format.
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev ruff check .
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev ruff format .
```

Run the fast suite once before making changes in a fresh environment. After changing
behavior, run the focused test, fast suite, and any affected integration suite.

FD acceptance needs an observer with access to nondumpable processes (normally
`CAP_SYS_PTRACE` in the target user namespace). The staged helper verifies process
identity, ownership and process-tree membership, reads counts rather than descriptor
contents, and runs through `sudo -n`. Missing permissions or incomplete samples fail
measurement instead of being reported as zero. Fast tests need no privileged observer.
Do not disable gateway nondumpability or grant privileges to the gateway to collect metrics.

## Gateway release checks

Use the pinned Go toolchain and root Python lock when touching gateway packaging:

```bash
GO=/path/to/go1.26.5/bin/go gateway/scripts/build_release_artifacts.sh
uv sync --locked --no-build --no-dev --python 3.12
uv audit --locked
UV_PROJECT_ENVIRONMENT=local/test-venv uv run --python 3.12 --extra dev \
  pytest --no-cov gateway/mediator
```

`hatch_build.py` normalizes packaged worker artifacts to mode `0755` before wheel collection;
this is required because shared/9p worktrees may not expose the Git executable bit to the
build user. CI must execute the clean-installed platform worker, not only inspect the source mode.

The integration commands above provide explicit-TCG functional/security acceptance.
macOS artifacts are build checks only unless run on macOS.

Check whether pinned SmolVM/Moby image-build inputs or their license notices changed:

```bash
python .github/scripts/check_image_build_inputs.py
```

A non-zero exit means review is needed. The checker reports new commits and SHA-256
values but never updates executable pins automatically.
