#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
uv=$(command -v uv || true)
[ -n "$uv" ] || { echo "uv is required" >&2; exit 1; }
cache=${XDG_CACHE_HOME:-$HOME/.cache}/sbx-gateway-test
key=$cache/guest_ed25519
mkdir -p "$cache"
[ -f "$key.pub" ] || ssh-keygen -q -t ed25519 -N '' -f "$key"
cd "$root"
rm -f "$HOME/.smolvm/images/rootless-gateway-test/rootfs.ext4"
"$uv" run sbx image build \
  --name rootless-gateway-test \
  --containerfile "$root/gateway/testdata/guest/Containerfile" \
  --rootfs-size-mb 2048 \
  --cache-dir "$HOME/.smolvm/images" \
  --json
