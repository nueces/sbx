"""Authenticated per-VM mitmproxy policy addon for the sbx gateway."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import posixpath
import queue
import re
import select
import socket
import struct
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

from hardening import apply
from mitmproxy import ctx, http, tls
from mitmproxy.net.http import url
from mitmproxy.proxy import server_hooks
from mitmproxy.proxy.layers.modes import Socks5AuthData
from mitmproxy.script import concurrent

MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_FIELDS = 100
MAX_ACTIVE_REQUESTS = 128
MAX_RETAINED_BODY = 8 * 1024 * 1024
MAX_RETAINED_TOTAL = 64 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE = 16 * 1024 * 1024
MAX_HOOK_OUTPUT = 1024 * 1024
MAX_HOOK_WORKERS = 8
MAX_TLS_HANDSHAKES = 32
MAX_TLS_CONTEXTS = 256
TLS_CONTEXT_TTL = 10 * 60
HOOK_TIMEOUT = 1.0
# Bound memory and backpressure, not URL fields; overflow terminates mediation visibly.
MAX_AUDIT_EVENT_BYTES = 1 << 20
AUDIT_WRITE_TIMEOUT = 1.0
_IMMUTABLE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


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


def _destination_allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and "%" not in address


class CapabilityVerifier:
    def __init__(self, key: bytes, vm_id: str, incarnation: str) -> None:
        if len(key) != 32:
            raise ValueError("capability key must contain exactly 32 bytes")
        self._key = key
        self._vm_id = vm_id
        self._incarnation = incarnation
        self._used: dict[str, int] = {}

    @property
    def identity(self) -> tuple[str, str]:
        return self._vm_id, self._incarnation

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
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(self._key, username.encode(), hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        if not hmac.compare_digest(expected, password) or username in self._used:
            return None
        self._used = {token: expiry for token, expiry in self._used.items() if expiry >= now}
        self._used[username] = expires
        return Admission(fields[1], fields[2], fields[3], fields[4], host, port, issued, expires)


class HookContext(NamedTuple):
    vm_id: str
    incarnation: str
    flow_id: str
    rule_id: str
    host: str
    port: int
    scheme: str
    method: str
    path: str


class Hook(NamedTuple):
    on_request_headers: Callable[[HookContext, http.Headers], object] | None = None
    on_request_chunk: Callable[[HookContext, bytes], object] | None = None
    on_response_headers: Callable[[HookContext, http.Headers], object] | None = None
    on_response_chunk: Callable[[HookContext, bytes], object] | None = None
    request_buffer_limit: int = 0
    response_buffer_limit: int = 0


class RequestPolicy(NamedTuple):
    rule_id: str
    host: str
    ports: tuple[int, ...]
    methods: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    policy_id: str = ""


class HeaderCredential(NamedTuple):
    credential: str
    rule_id: str
    methods: tuple[str, ...]
    paths: tuple[str, ...]
    header_name: str
    header_format: str
    header_value: str
    placeholder: str


class BrokerClient:
    def __init__(self, fd: int, incarnation: str) -> None:
        self.socket = socket.socket(fileno=fd)
        self.incarnation = incarnation
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._read, daemon=True, name="sbx-broker-reader").start()

    @staticmethod
    def _exact(sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def _read(self) -> None:
        try:
            while True:
                size = struct.unpack("!I", self._exact(self.socket, 4))[0]
                if not 0 < size <= 64 * 1024:
                    raise ValueError
                response = json.loads(self._exact(self.socket, size))
                request_id = response.get("request_id") if isinstance(response, dict) else None
                with self.lock:
                    result = self.pending.get(request_id)
                if (
                    result is None
                    or response.get("version") != 1
                    or response.get("incarnation") != self.incarnation
                ):
                    raise ValueError
                result.put(response)
        except Exception:
            # Broken framing/transport is fatal; fail waiters and let supervision replace us.
            with self.lock:
                self._closed = True
                pending = tuple(self.pending.values())
                self.pending.clear()
            self.socket.close()
            for result in pending:
                # This reader is the sole producer; do not block on an already delivered reply.
                if not result.full():
                    result.put_nowait({"error": "broker closed"})
            print("sbx: credential broker channel failed; mediation stopped", file=sys.stderr)
            ctx.master.shutdown()

    def fetch(self, credential: str) -> str:
        request_id = uuid.uuid4().hex
        result: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.lock:
            if self._closed:
                raise CredentialProviderError("credential broker unavailable")
            self.pending[request_id] = result
        request = json.dumps(
            {
                "version": 1,
                "request_id": request_id,
                "incarnation": self.incarnation,
                "credential": credential,
            },
            separators=(",", ":"),
        ).encode()
        try:
            with self.send_lock:
                self.socket.sendall(struct.pack("!I", len(request)) + request)
            response = result.get(timeout=10)
        except (OSError, queue.Empty) as exc:
            raise CredentialProviderError("credential broker unavailable") from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        value = response.get("value")
        if set(response) != {"version", "request_id", "incarnation", "value"} or not isinstance(
            value, str
        ):
            raise CredentialProviderError("credential broker unavailable")
        return value


class HookFailure(RuntimeError):
    pass


def _package_audit(_context: HookContext, _headers: http.Headers) -> str:
    return "continue"


BUILTIN_HOOKS = {
    "package-audit": Hook(
        on_request_headers=_package_audit,
        on_response_headers=_package_audit,
    )
}


class CredentialProviderError(RuntimeError):
    pass


class RetainedBufferBudget:
    def __init__(self) -> None:
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()
        self.total = 0

    def reserve(self, flow_id: str, size: int) -> bool:
        with self._lock:
            prior = self._sizes.get(flow_id, 0)
            total = self.total - prior + size
            if size < 0 or size > MAX_RETAINED_BODY or total > MAX_RETAINED_TOTAL:
                return False
            self._sizes[flow_id] = size
            self.total = total
            return True

    def release(self, flow_id: str) -> None:
        with self._lock:
            self.total -= self._sizes.pop(flow_id, 0)


class GatewayAddon:
    def __init__(
        self,
        verifier: CapabilityVerifier,
        ready_fd: int | None = None,
        *,
        policies: tuple[RequestPolicy, ...] = (),
        credentials: tuple[HeaderCredential, ...] = (),
        broker: BrokerClient | None = None,
        hooks: Mapping[str, Hook] = BUILTIN_HOOKS,
        hook_timeout: float = HOOK_TIMEOUT,
        audit_fd: int | None = None,
        audit_ack_fd: int | None = None,
        generation: str = "unknown",
    ) -> None:
        if not 0 < hook_timeout <= HOOK_TIMEOUT:
            raise ValueError("hook timeout must be positive and at most one second")
        if len({policy.rule_id for policy in policies}) != len(policies):
            raise ValueError("duplicate mediator request policy")
        if any(
            type(limit) is not int or not 0 <= limit <= MAX_RETAINED_BODY
            for hook in hooks.values()
            for limit in (hook.request_buffer_limit, hook.response_buffer_limit)
        ):
            raise ValueError("hook body buffer limit is invalid")
        configured_hooks = {name for policy in policies for name in policy.hooks}
        if configured_hooks - hooks.keys():
            raise ValueError("mediator policy names an unavailable hook")
        self._verifier = verifier
        self._ready_fd = ready_fd
        self._audit_fd = audit_fd
        self._audit_ack_fd = audit_ack_fd
        self._audit_failed = False
        self._audit_lock = threading.Lock()
        self._generation = generation
        if audit_fd is not None:
            os.set_blocking(audit_fd, False)
        if audit_ack_fd is not None:
            os.set_blocking(audit_ack_fd, False)
        self._policies = {policy.rule_id: policy for policy in policies}
        self._credentials = credentials
        self._broker = broker
        self._hooks = dict(hooks)
        self._hook_timeout = hook_timeout
        self._hook_slots = threading.BoundedSemaphore(MAX_HOOK_WORKERS)
        self._admissions: dict[str, Admission] = {}
        self._invalid_tls: set[str] = set()
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._handshakes: set[str] = set()
        self._tls_contexts: OrderedDict[str, float] = OrderedDict()
        self._certstore: Any | None = None
        self.retained = RetainedBufferBudget()

    def running(self) -> None:
        if self._ready_fd is None:
            return
        proxyserver = self._master_addon("proxyserver")
        self._certstore = self._master_addon("tlsconfig").certstore
        self._certstore.STORE_CAP = min(self._certstore.STORE_CAP, MAX_TLS_CONTEXTS)
        ctx.master.addons.remove(self._master_addon("proxyauth"))
        addresses = proxyserver.listen_addrs()
        if len(addresses) != 1 or addresses[0][0] != "127.0.0.1":
            raise RuntimeError("mediator did not bind exactly one loopback listener")
        vm_id, incarnation = self._verifier.identity
        payload = (
            json.dumps(
                {
                    "status": "ready",
                    "host": addresses[0][0],
                    "port": addresses[0][1],
                    "pid": os.getpid(),
                    "vm_id": vm_id,
                    "incarnation": incarnation,
                    "hardening": os.environ.get("SBX_GATEWAY_HARDENING", "unavailable"),
                    **(
                        {"web_port": ctx.options.web_port}
                        if hasattr(ctx.master, "gateway_snapshot")
                        else {}
                    ),
                }
            ).encode()
            + b"\n"
        )
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

    async def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        if data.server.error is not None:
            return
        admission = self._admissions.get(data.client.id)
        address = data.server.address
        pinned = getattr(data.server, "sbx_pinned_destination", None)
        if pinned is not None and address == pinned[1]:
            address = pinned[0]
        # Addon exceptions alone do not stop mitmproxy from connecting.
        data.server.error = "destination_denied"
        if (
            admission is None
            or address is None
            or _normalize_host(address[0]) != admission.host
            or address[1] != admission.port
        ):
            return
        try:
            answers = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    admission.host, admission.port, type=socket.SOCK_STREAM
                ),
                timeout=HOOK_TIMEOUT,
            )
        except (OSError, TimeoutError):
            # DNS failures are translated only at this fail-closed connection boundary.
            data.server.error = "destination_dns_failed"
            return
        data.server.error = "destination_ip_denied"
        if not answers or not all(_destination_allowed(answer[4][0]) for answer in answers):
            return
        # Keep TLS identity logical while the connector receives only a validated numeric IP.
        data.server.sni = data.server.sni or admission.host
        selected = min(answers, key=lambda answer: answer[0] != socket.AF_INET)
        endpoint = (selected[4][0], admission.port)
        data.server.sbx_pinned_destination = ((admission.host, admission.port), endpoint)
        data.server.address = endpoint
        data.server.error = None

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        client_id = data.context.client.id
        admission = self._admissions.get(client_id)
        host = _normalize_host(data.client_hello.sni or "")
        if admission is None or host != admission.host:
            self._invalid_tls.add(client_id)
            data.context.client.error = "sbx gateway denied TLS identity"
            return
        if client_id not in self._handshakes and len(self._handshakes) >= MAX_TLS_HANDSHAKES:
            self._invalid_tls.add(client_id)
            data.context.client.error = "sbx gateway TLS handshake capacity exhausted"
            return
        self._handshakes.add(client_id)
        now = time.monotonic()
        expired = False
        while (
            self._tls_contexts and next(iter(self._tls_contexts.values())) <= now - TLS_CONTEXT_TTL
        ):
            self._tls_contexts.popitem(last=False)
            expired = True
        if expired and self._certstore is not None:
            self._certstore.certs.clear()
            self._certstore.expire_queue.clear()
        self._tls_contexts.pop(host, None)
        self._tls_contexts[host] = now
        while len(self._tls_contexts) > MAX_TLS_CONTEXTS:
            self._tls_contexts.popitem(last=False)

    def tls_established_client(self, data: Any) -> None:
        self._handshakes.discard(data.context.client.id)

    def tls_failed_client(self, data: Any) -> None:
        self._handshakes.discard(data.context.client.id)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        flow.metadata["sbx_request"] = self._request_details(flow)
        if flow.is_replay:
            self._deny(flow, 403, b"sbx gateway denied replay")
            return
        admission = self._admissions.get(flow.client_conn.id)
        policy = self._authorize(flow, admission)
        if admission is None or policy is None or flow.client_conn.id in self._invalid_tls:
            self._deny(flow, 403, b"sbx gateway denied destination")
            return
        flow.metadata["sbx_policy"] = policy.policy_id or policy.rule_id
        if not self._headers_valid(flow.request):
            self._deny(flow, 431, b"sbx gateway rejected request headers")
            return
        with self._active_lock:
            full = flow.id not in self._active and len(self._active) >= MAX_ACTIVE_REQUESTS
            if not full:
                self._active.add(flow.id)
        if full:
            self._deny(flow, 503, b"sbx gateway request capacity exhausted")
            return
        prepared = False
        try:
            if not self._run_header_hooks(flow, admission, policy, request=True):
                return
            if self._authorize(flow, admission) is None:
                self._deny(flow, 403, b"sbx gateway denied destination")
                return
            if not self._substitute_credentials(flow, admission):
                return
            if self._authorize(flow, admission) is None or not self._headers_valid(flow.request):
                self._deny(flow, 403, b"sbx gateway denied destination")
                return
            if not flow.metadata.get("sbx_admitted"):
                self._audit(flow, admitted=True)
                flow.metadata["sbx_admitted"] = True
            flow.request.stream = self._body_stream(flow, admission, policy, request=True)
            prepared = True
        except Exception:
            # Addon exceptions are otherwise logged and ignored: kill before propagating.
            if flow.killable:
                flow.kill()
            raise
        finally:
            if not prepared:
                self._release_request(flow)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        with self._active_lock:
            active = flow.id in self._active
        if not active:
            return
        admission = self._admissions.get(flow.client_conn.id)
        policy = self._authorize(flow, admission)
        if flow.response is None or admission is None or policy is None:
            self._release_request(flow)
            if flow.response is not None:
                self._deny(flow, 502, b"sbx gateway denied destination")
            return
        if not self._headers_valid(flow.response):
            self._deny(flow, 502, b"sbx gateway rejected response headers")
            return
        if not self._response_destination_allowed(flow, admission):
            self._deny(flow, 502, b"sbx gateway denied redirect")
            return
        if not self._run_header_hooks(flow, admission, policy, request=False):
            return
        if self._authorize(flow, admission) is None or not self._response_destination_allowed(
            flow, admission
        ):
            self._deny(flow, 502, b"sbx gateway denied destination")
            return
        flow.response.stream = self._body_stream(flow, admission, policy, request=False)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        admission = self._admissions.get(flow.client_conn.id)
        message = (
            flow.websocket.messages[-1] if flow.websocket and flow.websocket.messages else None
        )
        if flow.websocket is not None:
            # The relay holds the current message independently; retain no forwarded history.
            flow.websocket.messages.clear()
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
        self._handshakes.discard(client.id)
        if hasattr(client, "sbx_admission"):
            del client.sbx_admission

    def _authorize(self, flow: http.HTTPFlow, admission: Admission | None) -> RequestPolicy | None:
        if admission is None or not self._request_matches(flow.request, admission):
            return None
        if not self._policies:
            return RequestPolicy(admission.rule_id, admission.host, (admission.port,))
        policy = self._policies.get(admission.rule_id)
        if (
            policy is None
            or not _host_matches(policy.host, admission.host)
            or admission.port not in policy.ports
            or (policy.methods and flow.request.method.upper() not in policy.methods)
            or (
                policy.paths
                and not any(_path_is_scoped(flow.request.path, path) for path in policy.paths)
            )
        ):
            return None
        return policy

    def _context(self, flow: http.HTTPFlow, admission: Admission) -> HookContext:
        return HookContext(
            admission.vm_id,
            admission.incarnation,
            flow.id,
            admission.rule_id,
            admission.host,
            admission.port,
            flow.request.scheme,
            flow.request.method,
            _normalized_path(flow.request.path),
        )

    def _invoke(self, callback: Callable[..., object], *args: object) -> object:
        if not self._hook_slots.acquire(blocking=False):
            raise HookFailure("hook capacity exhausted")
        results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def call() -> None:
            try:
                results.put((True, callback(*args)))
            except BaseException as exc:
                results.put((False, exc))
            finally:
                self._hook_slots.release()

        threading.Thread(target=call, daemon=True, name="sbx-hook").start()
        try:
            succeeded, result = results.get(timeout=self._hook_timeout)
        except queue.Empty as exc:
            raise HookFailure("hook timed out") from exc
        if not succeeded:
            raise HookFailure("hook crashed")
        return result

    def _run_header_hooks(
        self,
        flow: http.HTTPFlow,
        admission: Admission,
        policy: RequestPolicy,
        *,
        request: bool,
    ) -> bool:
        message = flow.request if request else flow.response
        assert message is not None
        attribute = "on_request_headers" if request else "on_response_headers"
        allowed = {"allow", "continue", "deny"} if request else {"continue", "deny"}
        for name in policy.hooks:
            callback = getattr(self._hooks[name], attribute)
            if callback is None:
                continue
            if self._authorize(flow, admission) is None:
                self._deny(flow, 403 if request else 502, b"sbx gateway denied destination")
                return False
            headers = message.headers.copy()
            immutable = self._immutable_headers(headers)
            try:
                action = self._invoke(callback, self._context(flow, admission), headers)
            except HookFailure:
                self._deny(flow, 403 if request else 502, b"sbx gateway hook failed")
                return False
            if (
                type(action) is not str
                or action not in allowed
                or action == "deny"
                or immutable != self._immutable_headers(headers)
            ):
                self._deny(flow, 403 if request else 502, b"sbx gateway hook denied request")
                return False
            message.headers = headers.copy()
            if (
                self._authorize(flow, admission) is None
                or not self._headers_valid(message)
                or (not request and not self._response_destination_allowed(flow, admission))
            ):
                self._deny(flow, 403 if request else 502, b"sbx gateway denied hook output")
                return False
        return True

    def _body_stream(
        self,
        flow: http.HTTPFlow,
        admission: Admission,
        policy: RequestPolicy,
        *,
        request: bool,
    ) -> bool | Callable[[bytes], bytes]:
        attribute = "on_request_chunk" if request else "on_response_chunk"
        callbacks = [
            (self._hooks[name], getattr(self._hooks[name], attribute))
            for name in policy.hooks
            if getattr(self._hooks[name], attribute) is not None
        ]
        if not callbacks:
            return True
        limits = [
            hook.request_buffer_limit if request else hook.response_buffer_limit
            for hook, _ in callbacks
            if (hook.request_buffer_limit if request else hook.response_buffer_limit)
        ]
        if request:
            flow.request.headers.pop("content-length", None)
        else:
            assert flow.response is not None
            flow.response.headers.pop("content-length", None)
        buffer = bytearray()
        key = f"{flow.id}:{'request' if request else 'response'}"
        limit = min(limits) if limits else 0
        failed = False

        def transform(chunk: bytes) -> bytes:
            nonlocal failed
            if failed:
                return b""
            if self._authorize(flow, admission) is None:
                failed = True
                self.retained.release(key)
                self._deny(flow, 403 if request else 502, b"sbx gateway denied destination")
                return b""
            if limit and chunk:
                buffer.extend(chunk)
                if len(buffer) > limit or not self.retained.reserve(key, len(buffer)):
                    failed = True
                    self.retained.release(key)
                    buffer.clear()
                    self._deny(flow, 413 if request else 502, b"sbx gateway hook body limit")
                return b""
            if limit:
                chunk = bytes(buffer)
                buffer.clear()
                self.retained.release(key)
            output = self._run_chunk_hooks(flow, admission, callbacks, chunk, request=request)
            failed = output is None
            return b"" if output is None else output

        return transform

    def _run_chunk_hooks(
        self,
        flow: http.HTTPFlow,
        admission: Admission,
        callbacks: list[tuple[Hook, Callable[[HookContext, bytes], object] | None]],
        chunk: bytes,
        *,
        request: bool,
    ) -> bytes | None:
        output = chunk
        for _, callback in callbacks:
            assert callback is not None
            if self._authorize(flow, admission) is None:
                self._deny(flow, 403 if request else 502, b"sbx gateway denied destination")
                return None
            try:
                result = self._invoke(callback, self._context(flow, admission), output)
            except HookFailure:
                self._deny(flow, 403 if request else 502, b"sbx gateway hook failed")
                return None
            if result == "deny" or type(result) is not bytes or len(result) > MAX_HOOK_OUTPUT:
                self._deny(flow, 403 if request else 502, b"sbx gateway denied hook output")
                return None
            output = result
            if self._authorize(flow, admission) is None:
                self._deny(flow, 403 if request else 502, b"sbx gateway denied destination")
                return None
        return output

    def _substitute_credentials(self, flow: http.HTTPFlow, admission: Admission) -> bool:
        matches = [
            binding
            for binding in self._credentials
            if binding.rule_id == admission.rule_id
            and flow.request.scheme == "https"
            and (not binding.methods or flow.request.method.upper() in binding.methods)
            and (
                not binding.paths
                or any(_path_is_scoped(flow.request.path, path) for path in binding.paths)
            )
        ]
        known = {binding.placeholder for binding in self._credentials}
        authorized = {(binding.header_name.lower(), binding.placeholder) for binding in matches}
        inspected = {
            binding.header_name.lower()
            for binding in self._credentials
            if binding.header_name.lower() in flow.request.headers
        }
        if any(
            placeholder in value and (name, placeholder) not in authorized
            for name in inspected
            for value in flow.request.headers.get_all(name)
            for placeholder in known
        ):
            self._deny(flow, 403, b"sbx gateway denied credential use")
            return False
        for binding in matches:
            values = flow.request.headers.get_all(binding.header_name)
            if binding.header_format == "template":
                expected = binding.header_value.replace("{placeholder}", binding.placeholder)
                valid = values == [expected]
            else:
                valid = len(values) == 1 and _parse_basic_authorization(values[0]) == (
                    binding.header_value,
                    binding.placeholder,
                )
            if not valid or self._broker is None or self._authorize(flow, admission) is None:
                self._deny(flow, 403, b"sbx gateway denied credential substitution")
                return False
            try:
                value = self._broker.fetch(binding.credential)
            except CredentialProviderError:
                self._deny(flow, 503, b"sbx gateway credential provider failed")
                return False
            rendered = (
                binding.header_value.replace("{placeholder}", value)
                if binding.header_format == "template"
                else _basic_authorization(binding.header_value, value)
            )
            if not _valid_header_value(rendered):
                self._deny(flow, 503, b"sbx gateway credential provider failed")
                return False
            flow.request.headers[binding.header_name] = rendered
        return True

    def _response_destination_allowed(self, flow: http.HTTPFlow, admission: Admission) -> bool:
        if (
            flow.response is None
            or not 300 <= flow.response.status_code < 400
            or "location" not in flow.response.headers
        ):
            return True
        return self._redirect_matches(flow, admission)

    @staticmethod
    def _immutable_headers(headers: http.Headers) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple((name, tuple(headers.get_all(name))) for name in sorted(_IMMUTABLE_HEADERS))

    def _headers_valid(self, message: http.Request | http.Response) -> bool:
        return (
            self._header_size(message) <= MAX_HEADER_BYTES
            and self._header_count(message) <= MAX_HEADER_FIELDS
            and all(
                _valid_header_name(name) and _valid_header_bytes(value)
                for name, value in message.headers.fields
            )
        )

    @staticmethod
    def _request_details(flow: http.HTTPFlow) -> dict[str, Any]:
        target = flow.request.path
        return {
            "url": flow.request.url,
            "request_target": target,
            "path": target.split("?", 1)[0].split("#", 1)[0],
            "method": flow.request.method,
            "host": flow.request.host,
            "port": flow.request.port,
            "scheme": flow.request.scheme,
            "authority": flow.request.authority or flow.request.host_header or "",
        }

    def _write_audit(self, encoded: bytes, acknowledgement: dict | None = None) -> None:
        if self._audit_fd is None:
            return
        try:
            if self._audit_failed:
                raise OSError("audit transport failed")
            if len(encoded) > MAX_AUDIT_EVENT_BYTES:
                raise OSError("audit event exceeds transport memory limit")
            deadline = time.monotonic() + AUDIT_WRITE_TIMEOUT
            if not self._audit_lock.acquire(timeout=AUDIT_WRITE_TIMEOUT):
                raise OSError("audit transport busy")
            try:
                if self._audit_failed:
                    raise OSError("audit transport failed")
                if acknowledgement is not None and self._audit_ack_fd is None:
                    raise OSError("audit acknowledgement unavailable")
                remaining = memoryview(encoded)
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0 or not select.select([], [self._audit_fd], [], timeout)[1]:
                        raise OSError("audit transport timed out")
                    try:
                        written = os.write(self._audit_fd, remaining)
                    except BlockingIOError:
                        continue
                    if written <= 0:
                        raise OSError("audit transport closed")
                    remaining = remaining[written:]
                if acknowledgement is not None:
                    reply = bytearray()
                    while b"\n" not in reply:
                        timeout = deadline - time.monotonic()
                        if (
                            timeout <= 0
                            or not select.select([self._audit_ack_fd], [], [], timeout)[0]
                        ):
                            raise OSError("audit acknowledgement timed out")
                        try:
                            chunk = os.read(self._audit_ack_fd, 4097 - len(reply))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise OSError("audit acknowledgement closed")
                        reply.extend(chunk)
                        if len(reply) > 4096:
                            raise OSError("audit acknowledgement oversized")
                    if reply.count(b"\n") != 1 or not reply.endswith(b"\n"):
                        raise OSError("audit acknowledgement malformed")
                    if json.loads(reply) != acknowledgement:
                        raise OSError("audit acknowledgement mismatch")
            except (OSError, ValueError):
                self._audit_failed = True
                raise
            finally:
                self._audit_lock.release()
        except (OSError, ValueError) as exc:
            self._audit_failed = True
            with suppress(OSError):
                os.write(self._audit_fd, b'{"version":1,"audit_error":"delivery_failed"}\n')
            master = getattr(ctx, "master", None)
            if master is not None:
                master.shutdown()
            raise RuntimeError("gateway audit delivery failed; mediation stopped") from exc

    def _audit(self, flow: http.HTTPFlow, *, admitted: bool = False) -> None:
        if not admitted:
            if flow.metadata.get("sbx_audited"):
                return
            flow.metadata["sbx_audited"] = True
        reason = flow.metadata.get("sbx_reason", "upstream_error" if flow.error else "allowed")
        details = flow.metadata.get("sbx_request") or self._request_details(flow)
        event = {
            "version": 2,
            "event": "admitted" if admitted else "outcome",
            "request_id": flow.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "generation": self._generation,
            "incarnation": self._verifier.identity[1],
            **details,
            "canonical_path": _normalized_path(details["request_target"]),
            "policy": flow.metadata.get("sbx_policy"),
            "status": flow.response.status_code if flow.response else 0,
            "outcome": "admitted"
            if admitted
            else (
                "error"
                if flow.error or str(reason).endswith("failed")
                else ("forwarded" if reason == "allowed" else "denied")
            ),
            "reason": reason,
        }
        for key, text in event.items():
            if isinstance(text, str):
                for credential in self._credentials:
                    text = text.replace(credential.placeholder, "[redacted]")
                event[key] = text
        encoded = (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        acknowledgement = (
            {key: event[key] for key in ("version", "generation", "incarnation", "request_id")}
            if admitted
            else None
        )
        self._write_audit(encoded, acknowledgement)
        snapshot = getattr(getattr(ctx, "master", None), "gateway_snapshot", None)
        if snapshot is not None and not admitted:
            ctx.master.event_loop.call_soon_threadsafe(snapshot, event)

    def _release_request(self, flow: http.HTTPFlow) -> None:
        with self._active_lock:
            self._active.discard(flow.id)

    def _finish(self, flow: http.HTTPFlow) -> None:
        try:
            self._audit(flow)
        finally:
            self._release_request(flow)
            self.retained.release(f"{flow.id}:request")
            self.retained.release(f"{flow.id}:response")

    def _deny(self, flow: http.HTTPFlow, status: int, body: bytes) -> None:
        self._release_request(flow)
        flow.metadata["sbx_reason"] = (
            body.decode("ascii").removeprefix("sbx gateway ").replace(" ", "_")
        )
        if flow.response is None:
            flow.response = http.Response.make(status, b"", {"content-type": "text/plain"})
            return
        flow.response.status_code = status
        flow.response.reason = ""
        flow.response.headers.clear()
        flow.response.headers["content-type"] = "text/plain"
        flow.response.headers["content-length"] = "0"
        flow.response.content = b""
        flow.response.stream = lambda _: b""

    @staticmethod
    def _request_matches(request: http.Request, admission: Admission) -> bool:
        if (
            request.scheme not in {"http", "https"}
            or _normalize_host(request.host) != admission.host
            or request.port != admission.port
        ):
            return False
        try:
            target = urlsplit(request.pretty_url)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False
        if (
            target.username is not None
            or target.password is not None
            or _normalize_host(target.hostname or "") != admission.host
            or target_port != admission.port
        ):
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

    def _redirect_matches(self, flow: http.HTTPFlow, admission: Admission) -> bool:
        assert flow.response is not None
        try:
            target = urlsplit(urljoin(flow.request.pretty_url, flow.response.headers["location"]))
            port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False
        policy = self._policies.get(admission.rule_id)
        return (
            target.scheme in {"http", "https"}
            and target.username is None
            and target.password is None
            and _normalize_host(target.hostname or "") == admission.host
            and port == admission.port
            and target.scheme == flow.request.scheme
            and (
                policy is None
                or not policy.paths
                or any(_path_is_scoped(target.path, path) for path in policy.paths)
            )
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
                len(message.http_version) + len(str(message.status_code)) + len(message.reason) + 4
            )
        return size


def _normalized_path(value: str) -> str:
    try:
        path = urlsplit(value).path
        for _ in range(4):
            if re.search(r"%(?![0-9A-Fa-f]{2})", path):
                return ""
            decoded = unquote(path, errors="strict")
            if decoded == path:
                break
            path = decoded
        else:
            return ""
    except (UnicodeDecodeError, ValueError):
        return ""
    if (
        "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        return ""
    return posixpath.normpath(path)


def _path_is_scoped(value: str, prefix: str) -> bool:
    path = _normalized_path(value)
    base = prefix.rstrip("/") or "/"
    return bool(path) and (base == "/" or path == base or path.startswith(base + "/"))


def _host_matches(rule: str, host: str) -> bool:
    if not rule.startswith("*."):
        return rule == host
    suffix = rule[1:]
    prefix = host.removesuffix(suffix)
    return host.endswith(suffix) and bool(prefix) and "." not in prefix


def _valid_header_name(value: bytes) -> bool:
    try:
        return bool(re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value))
    except TypeError:
        return False


def _valid_header_bytes(value: bytes) -> bool:
    return all(byte == 9 or 32 <= byte < 127 for byte in value)


def _valid_header_value(value: str) -> bool:
    try:
        return _valid_header_bytes(value.encode("latin1"))
    except UnicodeEncodeError:
        return False


def _basic_authorization(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def _parse_basic_authorization(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    scheme, separator, encoded = value.partition(" ")
    if scheme.lower() != "basic" or not separator or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


def load_policy(fd: int) -> tuple[tuple[RequestPolicy, ...], tuple[HeaderCredential, ...]]:
    with os.fdopen(fd, "rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("mediator policy is too large")
    try:
        payload = json.loads(raw)
        if set(payload) != {"version", "rules", "credentials"} or payload["version"] != 2:
            raise ValueError
        if not isinstance(payload["rules"], list) or not isinstance(payload["credentials"], list):
            raise ValueError
        policies = []
        for item in payload["rules"]:
            if not isinstance(item, dict) or set(item) - {"policy"} != {
                "id",
                "host",
                "ports",
                "methods",
                "paths",
                "hooks",
            }:
                raise ValueError
            host = _normalize_policy_host(item["host"])
            ports = _policy_ports(item["ports"])
            methods = _policy_methods(item["methods"])
            paths = _policy_paths(item["paths"])
            hooks = item["hooks"]
            if (
                not _valid_field(item["id"])
                or not isinstance(hooks, list)
                or len(set(hooks)) != len(hooks)
                or not all(name in BUILTIN_HOOKS for name in hooks)
            ):
                raise ValueError
            policy_id = item.get("policy", item["id"])
            if not _valid_field(policy_id):
                raise ValueError
            policies.append(
                RequestPolicy(item["id"], host, ports, methods, paths, tuple(hooks), policy_id)
            )
        credentials = []
        for item in payload["credentials"]:
            if not isinstance(item, dict) or set(item) != {
                "credential",
                "rule_id",
                "methods",
                "paths",
                "header",
                "placeholder",
            }:
                raise ValueError
            methods = _policy_methods(item["methods"])
            paths = _policy_paths(item["paths"])
            header = item["header"]
            placeholder = item["placeholder"]
            if (
                not _valid_field(item["credential"])
                or not _valid_field(item["rule_id"])
                or not isinstance(placeholder, str)
                or len(placeholder) < 32
                or not _valid_field(placeholder)
                or not isinstance(header, dict)
            ):
                raise ValueError
            name = header.get("name")
            kind = header.get("format")
            if not isinstance(name, str) or not _valid_header_name(name.encode()):
                raise ValueError
            if kind == "template" and set(header) == {"name", "format", "value"}:
                value = header["value"]
                if (
                    not isinstance(value, str)
                    or value.count("{placeholder}") != 1
                    or not _valid_header_value(value.replace("{placeholder}", placeholder))
                ):
                    raise ValueError
            elif kind == "basic-password" and set(header) == {"name", "format", "username"}:
                value = header["username"]
                if not isinstance(value, str) or not value or any(c in value for c in ":\r\n\0"):
                    raise ValueError
            else:
                raise ValueError
            credentials.append(
                HeaderCredential(
                    item["credential"],
                    item["rule_id"],
                    methods,
                    paths,
                    name,
                    kind,
                    value,
                    placeholder,
                )
            )
        if len({policy.rule_id for policy in policies}) != len(policies):
            raise ValueError
        return tuple(policies), tuple(credentials)
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid mediator policy") from exc


def _normalize_policy_host(value: object, *, exact: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError
    wildcard = value.startswith("*.")
    normalized = _normalize_host(value[2:] if wildcard else value)
    if (
        normalized is None
        or normalized != (value[2:] if wildcard else value)
        or (wildcard and (exact or "." not in normalized))
    ):
        raise ValueError
    return f"*.{normalized}" if wildcard else normalized


def _policy_ports(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(port) is not int or not 0 < port <= 65535 for port in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError
    return tuple(value)


def _policy_methods(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(set(value)) != len(value)
        or not all(
            isinstance(method, str)
            and method == method.upper()
            and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method)
            for method in value
        )
    ):
        raise ValueError
    return tuple(value)


def _policy_paths(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(set(value)) != len(value)
        or not all(
            isinstance(path, str)
            and path.startswith("/")
            and "?" not in path
            and "#" not in path
            and _normalized_path(path) == path
            for path in value
        )
    ):
        raise ValueError
    return tuple(value)


def _load() -> GatewayAddon:
    confdir = os.environ.pop("SBX_GATEWAY_CONFDIR", None)
    open_files = os.environ.pop("SBX_GATEWAY_OPEN_FILES", None)
    if confdir is not None and open_files is not None:
        status = apply(Path(confdir), int(open_files), Path(sys.executable))
        os.environ["SBX_GATEWAY_HARDENING"] = status.encode()
    fd = int(os.environ.pop("SBX_GATEWAY_KEY_FD"))
    ready = os.environ.pop("SBX_GATEWAY_READY_FD", None)
    policy_fd = os.environ.pop("SBX_GATEWAY_POLICY_FD", None)
    broker_fd = os.environ.pop("SBX_GATEWAY_BROKER_FD", None)
    lifetime_fd = os.environ.pop("SBX_GATEWAY_LIFETIME_FD", None)
    audit_fd = os.environ.pop("SBX_GATEWAY_AUDIT_FD", None)
    audit_ack_fd = os.environ.pop("SBX_GATEWAY_AUDIT_ACK_FD", None)
    if ready is not None and (audit_fd is None or audit_ack_fd is None):
        raise RuntimeError("gateway requires durable admission audit transport")
    generation = os.environ.pop("SBX_GATEWAY_GENERATION", "unknown")
    if lifetime_fd is not None:

        def monitor_supervisor() -> None:
            with os.fdopen(int(lifetime_fd), "rb") as lifetime:
                lifetime.read()
            os._exit(0)

        threading.Thread(target=monitor_supervisor, daemon=True).start()
    with os.fdopen(fd, "rb") as source:
        key = source.read(33)
    policies, credentials = load_policy(int(policy_fd)) if policy_fd is not None else ((), ())
    vm_id = os.environ.pop("SBX_GATEWAY_VM_ID")
    incarnation = os.environ.pop("SBX_GATEWAY_INCARNATION")
    addon = GatewayAddon(
        CapabilityVerifier(key, vm_id, incarnation),
        int(ready) if ready is not None else None,
        policies=policies,
        credentials=credentials,
        broker=BrokerClient(int(broker_fd), incarnation) if broker_fd is not None else None,
        audit_fd=int(audit_fd) if audit_fd is not None else None,
        audit_ack_fd=int(audit_ack_fd) if audit_ack_fd is not None else None,
        generation=generation,
    )
    requestheaders = concurrent(addon.requestheaders)
    requestheaders.__name__ = "requestheaders"
    addon.requestheaders = requestheaders  # type: ignore[method-assign]
    return addon


addons = [_load()]
