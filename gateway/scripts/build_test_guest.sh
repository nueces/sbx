#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/sbx-gateway-test
key=$cache/guest_ed25519
mkdir -p "$cache"
[ -f "$key.pub" ] || ssh-keygen -q -t ed25519 -N '' -f "$key"
cd "$root"
uv run sbx image build-debian \
  --name rootless-gateway-test \
  --containerfile "$root/gateway/testdata/guest/Containerfile" \
  --rootfs-size-mb 2048 \
  --ssh-public-key "$key.pub" \
  --cache-dir "$HOME/.smolvm/images" \
  --json
