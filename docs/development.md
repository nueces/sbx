# Development

For a fresh checkout or agent environment, create an isolated test venv outside the
repository. This avoids accidentally reusing a `.venv` from the host machine and
keeps test dependencies reproducible:

```bash
cd /path/to/sbx/main
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov
```

Useful follow-up commands:

```bash
# Run the full test suite.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov

# Run focused tests while iterating.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev pytest --no-cov tests/test_cli.py

# Lint/format.
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev ruff check .
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --extra dev ruff format .
```

Before making code changes in a fresh environment, first run the full test suite
once with the command above to verify the checkout and tool environment are
healthy. After changing CLI behavior, add/update focused tests and run both the
focused tests and the full suite.
