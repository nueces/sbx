"""Per-VM mitmproxy policy addon for the sbx gateway."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any

from mitmproxy import http
from mitmproxy.proxy.layers.modes import Socks5AuthData


@dataclass(frozen=True)
class Admission:
    vm_id: str
    incarnation: str
    flow_id: str
    rule_id: str
    host: str
    port: int
    expires: int


class CapabilityVerifier:
    def __init__(self, key: bytes, vm_id: str, incarnation: str) -> None:
        if len(key) < 32:
            raise ValueError("capability key must contain at least 32 bytes")
        self._key = key
        self._vm_id = vm_id
        self._incarnation = incarnation
        self._used: dict[str, int] = {}

    def verify(self, username: str, password: str, now: int | None = None) -> Admission | None:
        now = int(time.time()) if now is None else now
        fields = username.split("|")
        if len(username) > 255 or len(fields) != 9 or fields[0] != "1":
            return None
        try:
            expires = int(fields[5])
            padding = "=" * (-len(fields[6]) % 4)
            base64.b64decode(fields[6] + padding, altchars=b"-_", validate=True)
            port = int(fields[8])
        except (ValueError, TypeError):
            return None
        if not 0 < port <= 65535 or not now < expires <= now + 32:
            return None
        if fields[1] != self._vm_id or fields[2] != self._incarnation:
            return None
        expected = base64.urlsafe_b64encode(
            hmac.new(self._key, username.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, password) or username in self._used:
            return None
        self._used = {token: expiry for token, expiry in self._used.items() if expiry >= now}
        self._used[username] = expires
        return Admission(fields[1], fields[2], fields[3], fields[4], fields[7], port, expires)


class GatewayAddon:
    def __init__(self, verifier: CapabilityVerifier) -> None:
        self._verifier = verifier
        self._admissions: dict[int, Admission] = {}

    def socks5_auth(self, data: Socks5AuthData) -> None:
        admission = self._verifier.verify(data.username, data.password)
        data.valid = admission is not None
        if admission is not None:
            self._admissions[id(data.client_conn)] = admission

    def request(self, flow: http.HTTPFlow) -> None:
        admission = self._admissions.get(id(flow.client_conn))
        host = flow.request.host.lower().rstrip(".")
        if admission is None or host != admission.host or flow.request.port != admission.port:
            flow.response = http.Response.make(403, b"sbx gateway denied destination")

    def client_disconnected(self, client: Any) -> None:
        self._admissions.pop(id(client), None)


def _load() -> GatewayAddon:
    fd = int(os.environ.pop("SBX_GATEWAY_KEY_FD"))
    try:
        key = os.read(fd, 64)
    finally:
        os.close(fd)
    return GatewayAddon(
        CapabilityVerifier(
            key,
            os.environ.pop("SBX_GATEWAY_VM_ID"),
            os.environ.pop("SBX_GATEWAY_INCARNATION"),
        )
    )


addons = [_load()]
