"""Authenticated per-VM mitmproxy policy addon for the sbx gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

from mitmproxy import ctx, http, tls
from mitmproxy.net.http import url
from mitmproxy.proxy import server_hooks
from mitmproxy.proxy.layers.modes import Socks5AuthData

MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_FIELDS = 100
MAX_ACTIVE_REQUESTS = 128
MAX_RETAINED_BODY = 8 * 1024 * 1024
MAX_RETAINED_TOTAL = 64 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE = 16 * 1024 * 1024


class Admission(NamedTuple):
    vm_id: str
    incarnation: str
    flow_id: str
    rule_id: str
    host: str
    port: int
    issued: int
    expires: int


def _valid_field(value: str) -> bool:
    return bool(value) and "|" not in value and all(32 < ord(char) < 127 for char in value)


def _normalize_host(value: str) -> str | None:
    host = value.lower().removesuffix(".")
    if not _valid_field(host):
        return None
    return host


class CapabilityVerifier:
    def __init__(self, key: bytes, vm_id: str, incarnation: str) -> None:
        if len(key) != 32:
            raise ValueError("capability key must contain exactly 32 bytes")
        self._key = key
        self._vm_id = vm_id
        self._incarnation = incarnation
        self._used: dict[str, int] = {}

    def verify(self, username: str, password: str, now: int | None = None) -> Admission | None:
        now = int(time.time()) if now is None else now
        fields = username.split("|")
        if len(username) > 255 or len(fields) != 10 or fields[0] != "1":
            return None
        try:
            issued = int(fields[5])
            expires = int(fields[6])
            padding = "=" * (-len(fields[7]) % 4)
            nonce = base64.b64decode(fields[7] + padding, altchars=b"-_", validate=True)
            port = int(fields[9])
        except (ValueError, TypeError, binascii.Error):
            return None
        host = _normalize_host(fields[8])
        if (
            len(nonce) != 12
            or not 0 < port <= 65535
            or issued > now + 2
            or not max(now, issued) < expires <= issued + 30
            or fields[1] != self._vm_id
            or fields[2] != self._incarnation
            or not _valid_field(fields[3])
            or not _valid_field(fields[4])
            or host != fields[8]
        ):
            return None
        expected = base64.urlsafe_b64encode(
            hmac.new(self._key, username.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, password) or username in self._used:
            return None
        self._used = {token: expiry for token, expiry in self._used.items() if expiry >= now}
        self._used[username] = expires
        return Admission(
            fields[1], fields[2], fields[3], fields[4], host, port, issued, expires
        )


class RetainedBufferBudget:
    def __init__(self) -> None:
        self._sizes: dict[str, int] = {}
        self.total = 0

    def reserve(self, flow_id: str, size: int) -> bool:
        prior = self._sizes.get(flow_id, 0)
        total = self.total - prior + size
        if size < 0 or size > MAX_RETAINED_BODY or total > MAX_RETAINED_TOTAL:
            return False
        self._sizes[flow_id] = size
        self.total = total
        return True

    def release(self, flow_id: str) -> None:
        self.total -= self._sizes.pop(flow_id, 0)


class GatewayAddon:
    def __init__(self, verifier: CapabilityVerifier, ready_fd: int | None = None) -> None:
        self._verifier = verifier
        self._ready_fd = ready_fd
        self._admissions: dict[str, Admission] = {}
        self._invalid_tls: set[str] = set()
        self._active: set[str] = set()
        self.retained = RetainedBufferBudget()

    def running(self) -> None:
        if self._ready_fd is None:
            return
        proxyserver = self._master_addon("proxyserver")
        ctx.master.addons.remove(self._master_addon("proxyauth"))
        addresses = proxyserver.listen_addrs()
        if len(addresses) != 1 or addresses[0][0] != "127.0.0.1":
            raise RuntimeError("mediator did not bind exactly one loopback listener")
        payload = json.dumps(
            {"host": addresses[0][0], "port": addresses[0][1], "pid": os.getpid()}
        ).encode() + b"\n"
        try:
            os.write(self._ready_fd, payload)
        finally:
            os.close(self._ready_fd)
            self._ready_fd = None

    @staticmethod
    def _master_addon(name: str) -> Any:
        return ctx.master.addons.get(name)

    def socks5_auth(self, data: Socks5AuthData) -> None:
        admission = self._verifier.verify(data.username, data.password)
        data.valid = admission is not None
        if admission is not None:
            self._admissions[data.client_conn.id] = admission
            data.client_conn.sbx_admission = admission

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        admission = self._admissions.get(data.client.id)
        address = data.server.address
        if (
            admission is None
            or address is None
            or _normalize_host(address[0]) != admission.host
            or address[1] != admission.port
        ):
            data.server.error = "sbx gateway denied destination"

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        admission = self._admissions.get(data.context.client.id)
        if admission is None or _normalize_host(data.client_hello.sni or "") != admission.host:
            self._invalid_tls.add(data.context.client.id)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        admission = self._admissions.get(flow.client_conn.id)
        if (
            admission is None
            or flow.client_conn.id in self._invalid_tls
            or not self._request_matches(flow.request, admission)
        ):
            self._deny(flow, 403, b"sbx gateway denied destination")
            return
        if (
            self._header_size(flow.request) > MAX_HEADER_BYTES
            or self._header_count(flow.request) > MAX_HEADER_FIELDS
        ):
            self._deny(flow, 431, b"sbx gateway rejected request headers")
            return
        if len(self._active) >= MAX_ACTIVE_REQUESTS and flow.id not in self._active:
            self._deny(flow, 503, b"sbx gateway request capacity exhausted")
            return
        self._active.add(flow.id)
        flow.request.stream = True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        if (
            self._header_size(flow.response) > MAX_HEADER_BYTES
            or self._header_count(flow.response) > MAX_HEADER_FIELDS
        ):
            self._deny(flow, 502, b"sbx gateway rejected response headers")
            return
        if 300 <= flow.response.status_code < 400 and "location" in flow.response.headers:
            admission = self._admissions.get(flow.client_conn.id)
            if admission is None or not self._redirect_matches(flow, admission):
                self._deny(flow, 502, b"sbx gateway denied redirect")
                return
        flow.response.stream = True

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        admission = self._admissions.get(flow.client_conn.id)
        message = (
            flow.websocket.messages[-1]
            if flow.websocket and flow.websocket.messages
            else None
        )
        if (
            admission is None
            or not self._request_matches(flow.request, admission)
            or message is None
            or len(message.content) > MAX_WEBSOCKET_MESSAGE
        ):
            if message is not None:
                message.drop()
            flow.kill()

    def response(self, flow: http.HTTPFlow) -> None:
        self._finish(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        self._finish(flow)

    def client_disconnected(self, client: Any) -> None:
        self._admissions.pop(client.id, None)
        self._invalid_tls.discard(client.id)
        if hasattr(client, "sbx_admission"):
            del client.sbx_admission

    def _finish(self, flow: http.HTTPFlow) -> None:
        self._active.discard(flow.id)
        self.retained.release(flow.id)

    @staticmethod
    def _deny(flow: http.HTTPFlow, status: int, body: bytes) -> None:
        if flow.response is None:
            flow.response = http.Response.make(
                status, body, {"content-type": "text/plain"}
            )
            return
        flow.response.status_code = status
        flow.response.reason = ""
        flow.response.headers.clear()
        flow.response.headers["content-length"] = "0"
        flow.response.stream = lambda _: b""

    @staticmethod
    def _request_matches(request: http.Request, admission: Admission) -> bool:
        if _normalize_host(request.host) != admission.host or request.port != admission.port:
            return False
        authorities = {value for value in (request.authority, request.host_header) if value}
        for authority in authorities:
            try:
                host, port = url.parse_authority(authority, check=True)
            except ValueError:
                return False
            if _normalize_host(host) != admission.host:
                return False
            authority_port = port or (443 if request.scheme == "https" else 80)
            if authority_port != admission.port:
                return False
        return True

    @staticmethod
    def _redirect_matches(flow: http.HTTPFlow, admission: Admission) -> bool:
        assert flow.response is not None
        try:
            target = urlsplit(urljoin(flow.request.pretty_url, flow.response.headers["location"]))
            port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False
        return (
            target.scheme in {"http", "https"}
            and target.username is None
            and target.password is None
            and _normalize_host(target.hostname or "") == admission.host
            and port == admission.port
            and target.scheme == flow.request.scheme
        )

    @staticmethod
    def _header_count(message: http.Request | http.Response) -> int:
        pseudo = 4 if isinstance(message, http.Request) and message.is_http2 else 0
        if isinstance(message, http.Response) and message.is_http2:
            pseudo = 1
        return len(message.headers.fields) + pseudo

    @staticmethod
    def _header_size(message: http.Request | http.Response) -> int:
        size = sum(len(name) + len(value) + 4 for name, value in message.headers.fields) + 2
        if isinstance(message, http.Request):
            size += len(message.method) + len(message.path) + len(message.http_version) + 4
            if message.is_http2:
                size += len(message.scheme) + len(message.authority) + 32
        else:
            size += (
                len(message.http_version)
                + len(str(message.status_code))
                + len(message.reason)
                + 4
            )
        return size


def _load() -> GatewayAddon:
    fd = int(os.environ.pop("SBX_GATEWAY_KEY_FD"))
    ready = os.environ.pop("SBX_GATEWAY_READY_FD", None)
    with os.fdopen(fd, "rb") as source:
        key = source.read(33)
    return GatewayAddon(
        CapabilityVerifier(
            key,
            os.environ.pop("SBX_GATEWAY_VM_ID"),
            os.environ.pop("SBX_GATEWAY_INCARNATION"),
        ),
        int(ready) if ready is not None else None,
    )


addons = [_load()]
