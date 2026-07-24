from __future__ import annotations

import hashlib

from sbx import gateway


def test_packaged_gateway_artifacts_have_recorded_digests() -> None:
    checksums = gateway._gateway_checksums()
    expected = {
        "bin/sbx-gateway-linux-amd64",
        "bin/sbx-gateway-darwin-amd64",
        "bin/sbx-gateway-darwin-arm64",
        "mediator/uv.lock",
    }
    assert expected <= set(checksums)
    for name in expected:
        path = gateway.GATEWAY_RUNTIME_DIR / name
        assert checksums[name] == hashlib.sha256(path.read_bytes()).hexdigest()
