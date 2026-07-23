#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
uv=${SBX_UV_BIN:-$(command -v uv || true)}
[ -n "$uv" ] && [ -x "$uv" ] || { echo "uv is required (set SBX_UV_BIN)" >&2; exit 1; }
cache=${XDG_CACHE_HOME:-$HOME/.cache}/sbx-gateway-test
key=$cache/guest_ed25519
mkdir -p "$cache"
[ -f "$key.pub" ] || ssh-keygen -q -t ed25519 -N '' -f "$key"
cd "$root"
rm -f "$HOME/.smolvm/images/rootless-gateway-test/rootfs.ext4"
"$uv" run sbx image build-debian \
  --name rootless-gateway-test \
  --containerfile "$root/gateway/testdata/guest/Containerfile" \
  --rootfs-size-mb 2048 \
  --ssh-public-key "$key.pub" \
  --cache-dir "$HOME/.smolvm/images" \
  --json
