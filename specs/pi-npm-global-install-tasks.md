# Pi npm global install tasks

## Implementation

1. [x] Keep the existing `PATH` order in `src/sbx/image/resources/Containers/Agents/Pi.Containerfile`.
2. [x] Replace the prefixed local npm install plus manual symlink with `npm install --global --ignore-scripts @earendil-works/pi-coding-agent` after `npm config set prefix ~/.nodejs`.
3. [x] Persist the npm bin path in the guest login environment; Docker image `ENV` metadata alone is not enough after rootfs export.
4. [x] Update docs that mention the Pi binary path if they still point to `/home/agent/.local/bin/pi`.

## Tests

1. [x] Add one Containerfile test asserting npm global install, image `PATH` keeps `/home/agent/.local/bin` before `/home/agent/.nodejs/bin`, and `.profile` exports the same order.

## Checks

```bash
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-pi-npm-global-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_build_debian_image.py
[x] UV_PROJECT_ENVIRONMENT=/tmp/sbx-pi-npm-global-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
