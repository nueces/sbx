# Pi npm global install design

## Goal

Build local `sbx` Pi images so `pi update` can update Pi from inside the VM.

Expected behavior inside a VM built from the image:

```bash
which pi
# /home/agent/.nodejs/bin/pi

pi update
# updates Pi instead of reporting "cannot self-update this installation"
```

## Problem

The current Pi image installs Pi with a package-local npm prefix and then creates a manual symlink:

```dockerfile
npm install --prefix ~/.nodejs --ignore-scripts @earendil-works/pi-coding-agent
ln -s ~/.nodejs/node_modules/.bin/pi ~/.local/bin/pi
```

Pi sees the executable at `/home/agent/.local/bin/pi`, which is not an npm-managed global binary, so self-update refuses:

```text
error: pi cannot self-update this installation.
This installation is not managed by a global npm install.
```

## Scope

In scope:

- Change the packaged Pi Containerfile to install Pi as an npm global package under the agent-owned npm prefix.
- Remove the manual `~/.local/bin/pi` symlink.
- Keep install scripts disabled with `--ignore-scripts`.
- Keep the image user-owned; no sudo/global system npm install.
- Document that existing images/VMs must be rebuilt/recreated.

Out of scope:

- Implementing `pi update` itself.
- Updating Pi extensions/packages.
- Adding an sbx command to update Pi.
- Changing SmolVM presets.
- Supporting old already-built images in place.

## Design

Use npm's own global install machinery with the existing user prefix while preserving the previous PATH order:

```dockerfile
ENV PATH="/home/agent/.local/bin:/home/agent/.nodejs/bin:${PATH}"

RUN npm config set prefix ~/.nodejs && \
    npm install --global --ignore-scripts @earendil-works/pi-coding-agent && \
    printf '\nexport PATH="$HOME/.local/bin:$HOME/.nodejs/bin:$PATH"\n' >> ~/.profile
```

Cut the manual symlink. The npm global install creates `~/.nodejs/bin/pi`, which lets Pi recognize the install as npm-managed. Keeping `.local/bin` first avoids letting unrelated npm globals shadow local user tools.

Persist the npm bin path in the guest login environment. Docker image `ENV` metadata is not enough by itself because SmolVM builds the VM rootfs from an exported filesystem.

## Test strategy

No real Docker build is needed for the unit test. Add a small test that reads `Containers/Agents/Pi.Containerfile` and asserts:

- it uses `npm install --global --ignore-scripts @earendil-works/pi-coding-agent`,
- image `PATH` keeps `/home/agent/.local/bin` before `/home/agent/.nodejs/bin`,
- `.profile` exports `$HOME/.local/bin:$HOME/.nodejs/bin:$PATH`.

## Verification plan

Run focused tests and lint:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev pytest --no-cov tests/test_build_debian_image.py
UV_PROJECT_ENVIRONMENT=/tmp/sbx-test-venv uv run --python /usr/bin/python3 --extra dev ruff check .
```
