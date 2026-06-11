# Agent guidance

## Coding style

- Avoid local imports inside functions. Prefer module-level imports so dependencies are visible and easier to test. If a local import seems necessary because of a circular import, treat that as a code-organization problem: surface it explicitly so we can decide how and when to do the proper refactor rather than hiding it in the implementation.
- Keep user-facing configuration small and explicit. Avoid adding multiple narrowly-scoped options when one higher-level concept is enough.

## Fresh environment setup

Before making code changes in a fresh checkout/agent environment, verify the test environment works:

```bash
cd /path/to/your/local/sbx-checkout
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov
```

Use the same `UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ...` prefix for focused pytest, ruff, and other dev commands. Keeping the venv in `/tmp` avoids broken in-repo virtualenv symlinks and keeps the checkout clean.

## Test style

- Prefer pytest fixtures for shared setup, monkeypatching, fake command runners, temporary config files, and reusable test data.
- Avoid large per-test setup blocks when a fixture can make the intent clearer.
- Keep tests isolated from real host state. Use `tmp_path`, `monkeypatch`, and fake command implementations instead of real QEMU, SSH, Docker, or SmolVM VM startup.
- Prefer testing command construction and state transitions with mocks/fakes over invoking external tools.
- Add focused tests for error branches and edge cases when changing CLI behavior.
