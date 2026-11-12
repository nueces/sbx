#!/usr/bin/env bash
set -euo pipefail

: "${GO:=go}"
export GOTOOLCHAIN=local

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out="$root/src/sbx/gateway_runtime"
mkdir -p "$out/bin" "$out/mediator"

rm -f "$out/SHA256SUMS" "$out/mediator/pyproject.toml" "$out/mediator/.python-version" "$out/mediator/uv.lock"
for file in launch.py hardening.py sbx_gateway_mediator.py sbx_gateway_web.py; do
  cp "$root/gateway/mediator/$file" "$out/mediator/$file"
done

build() {
  local goos="$1" goarch="$2"
  CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" "$GO" -C "$root/gateway" build \
    -trimpath -ldflags='-s -w' \
    -o "$out/bin/sbx-gateway-$goos-$goarch" ./cmd/sbx-gateway
  chmod 0555 "$out/bin/sbx-gateway-$goos-$goarch"
}

build linux amd64
build darwin amd64
build darwin arm64
