"""Authenticated per-VM mitmproxy policy addon for the sbx gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import queue
import re
import selectors
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

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
MAX_HOOK_OUTPUT = 1024 * 1024
MAX_HOOK_WORKERS = 8
MAX_PROVIDER_OUTPUT = 64 * 1024
MAX_PROVIDER_WORKERS = 8
MAX_TLS_HANDSHAKES = 32
MAX_TLS_CONTEXTS = 256
TLS_CONTEXT_TTL = 10 * 60
PROVIDER_TIMEOUT = 10
HOOK_TIMEOUT = 1.0
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


class PackageAuth(NamedTuple):
    rule_id: str
    host: str
    port: int
    path_prefix: str
    username: str
    placeholder: str


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


class HeaderSecret(NamedTuple):
    name: str
    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    methods: tuple[str, ...]
    path_prefix: str | None
    header_name: str
    header_template: str
    placeholder: str
    value: str


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


_PROVIDER_SLOTS = threading.BoundedSemaphore(MAX_PROVIDER_WORKERS)


def _bounded_provider_run(
    argv: list[str] | tuple[str, ...],
    *,
    text: bool,
    capture_output: bool,
    check: bool,
    timeout: float,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    if not text or not capture_output or check:
        raise ValueError("invalid bounded provider invocation")
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        close_fds=True,
    )
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in streams:
        assert stream is not None
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(chunk)
                if sum(map(len, streams.values())) > MAX_PROVIDER_OUTPUT:
                    raise CredentialProviderError("credential command returned oversized output")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(
            argv,
            process.wait(timeout=remaining),
            bytes(streams[process.stdout]).decode(errors="replace"),
            bytes(streams[process.stderr]).decode(errors="replace"),
        )
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        for stream in streams:
            stream.close()


def _provider_result(
    runner: Callable[..., subprocess.CompletedProcess[str]], argv: list[str] | tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    if not _PROVIDER_SLOTS.acquire(blocking=False):
        raise CredentialProviderError("credential provider capacity exhausted")
    try:
        result = runner(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=PROVIDER_TIMEOUT,
            env={"HOME": os.environ.get("HOME", ""), "PATH": os.defpath, "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialProviderError("credential command could not be started") from exc
    finally:
        _PROVIDER_SLOTS.release()
    if len(result.stdout.encode()) + len(result.stderr.encode()) > MAX_PROVIDER_OUTPUT:
        raise CredentialProviderError("credential command returned oversized output")
    return result


class CommandTokenProvider:
    def __init__(
        self,
        argv: tuple[str, ...],
        runner: Callable[..., subprocess.CompletedProcess[str]] = _bounded_provider_run,
    ) -> None:
        self._argv = argv
        self._runner = runner

    def get_token(self) -> str:
        result = _provider_result(self._runner, self._argv)
        if result.returncode != 0:
            raise CredentialProviderError(
                f"credential command failed{_diagnostic_suffix(result.stderr)}"
            )
        token = result.stdout.strip()
        if not token:
            raise CredentialProviderError("credential command returned an empty token")
        return token


class AwsCodeArtifactTokenProvider:
    def __init__(
        self,
        *,
        domain: str,
        domain_owner: str,
        region: str,
        profile: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = _bounded_provider_run,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._argv = ["aws"]
        if profile:
            self._argv.extend(("--profile", profile))
        self._argv.extend(
            (
                "codeartifact",
                "get-authorization-token",
                "--domain",
                domain,
                "--domain-owner",
                domain_owner,
                "--region",
                region,
                "--output",
                "json",
            )
        )
        self._runner = runner
        self._clock = clock
        self._cached: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._cached is not None and self._cached[1] - self._clock() > 60:
                return self._cached[0]
            result = _provider_result(self._runner, self._argv)
            if result.returncode != 0:
                raise CredentialProviderError(
                    f"aws CodeArtifact token refresh failed{_diagnostic_suffix(result.stderr)}"
                )
            try:
                payload = json.loads(result.stdout)
                token = payload["authorizationToken"].strip()
                expires = _expiration(payload["expiration"])
            except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CredentialProviderError(
                    "aws CodeArtifact returned an invalid response"
                ) from exc
            if not token or expires is None:
                raise CredentialProviderError("aws CodeArtifact returned an invalid response")
            self._cached = token, expires
            return token


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
        package_auth: PackageAuth | None = None,
        get_real_token: Callable[[], str] | None = None,
        *,
        policies: tuple[RequestPolicy, ...] = (),
        secrets: tuple[HeaderSecret, ...] = (),
        hooks: Mapping[str, Hook] = BUILTIN_HOOKS,
        hook_timeout: float = HOOK_TIMEOUT,
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
        self._package_auth = package_auth
        self._get_real_token = get_real_token
        self._policies = {policy.rule_id: policy for policy in policies}
        self._secrets = secrets
        self._hooks = dict(hooks)
        self._hook_timeout = hook_timeout
        self._hook_slots = threading.BoundedSemaphore(MAX_HOOK_WORKERS)
        self._admissions: dict[str, Admission] = {}
        self._invalid_tls: set[str] = set()
        self._active: set[str] = set()
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
            self._tls_contexts
            and next(iter(self._tls_contexts.values())) <= now - TLS_CONTEXT_TTL
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
        admission = self._admissions.get(flow.client_conn.id)
        policy = self._authorize(flow, admission)
        if admission is None or policy is None or flow.client_conn.id in self._invalid_tls:
            self._deny(flow, 403, b"sbx gateway denied destination")
            return
        if not self._headers_valid(flow.request):
            self._deny(flow, 431, b"sbx gateway rejected request headers")
            return
        if len(self._active) >= MAX_ACTIVE_REQUESTS and flow.id not in self._active:
            self._deny(flow, 503, b"sbx gateway request capacity exhausted")
            return
        if not self._run_header_hooks(flow, admission, policy, request=True):
            return
        if self._authorize(flow, admission) is None:
            self._deny(flow, 403, b"sbx gateway denied destination")
            return
        if self._package_auth is not None and admission.rule_id == self._package_auth.rule_id:
            if not self._package_request_matches(flow.request, admission):
                self._deny(flow, 403, b"sbx gateway denied package authorization")
                return
            parsed = _parse_basic_authorization(flow.request.headers.get("Authorization"))
            if parsed != (self._package_auth.username, self._package_auth.placeholder):
                self._deny(flow, 403, b"sbx gateway denied package authorization")
                return
            if self._get_real_token is None or self._authorize(flow, admission) is None:
                self._deny(flow, 503, b"sbx gateway credential provider unavailable")
                return
            try:
                token = self._get_real_token()
            except CredentialProviderError:
                self._deny(flow, 503, b"sbx gateway credential provider failed")
                return
            flow.request.headers["Authorization"] = _basic_authorization(
                self._package_auth.username, token
            )
        if not self._substitute_secrets(flow, admission):
            return
        if self._authorize(flow, admission) is None or not self._headers_valid(flow.request):
            self._deny(flow, 403, b"sbx gateway denied destination")
            return
        self._active.add(flow.id)
        flow.request.stream = self._body_stream(flow, admission, policy, request=True)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.id not in self._active:
            return
        admission = self._admissions.get(flow.client_conn.id)
        policy = self._authorize(flow, admission)
        if flow.response is None or admission is None or policy is None:
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

    def _substitute_secrets(self, flow: http.HTTPFlow, admission: Admission) -> bool:
        matches = [
            secret
            for secret in self._secrets
            if flow.request.scheme == "https"
            and admission.host in secret.hosts
            and admission.port in secret.ports
            and (not secret.methods or flow.request.method.upper() in secret.methods)
            and (
                secret.path_prefix is None or _path_is_scoped(flow.request.path, secret.path_prefix)
            )
        ]
        if len({secret.header_name.lower() for secret in matches}) != len(matches):
            self._deny(flow, 403, b"sbx gateway denied ambiguous secret binding")
            return False
        for secret in matches:
            if self._authorize(flow, admission) is None:
                self._deny(flow, 403, b"sbx gateway denied destination")
                return False
            expected = secret.header_template.replace("{placeholder}", secret.placeholder)
            if flow.request.headers.get(secret.header_name) != expected:
                self._deny(flow, 403, b"sbx gateway denied secret substitution")
                return False
            replacement = secret.header_template.replace("{placeholder}", secret.value)
            if not _valid_header_value(replacement):
                self._deny(flow, 503, b"sbx gateway credential provider failed")
                return False
            flow.request.headers[secret.header_name] = replacement
        return True

    def _response_destination_allowed(self, flow: http.HTTPFlow, admission: Admission) -> bool:
        if (
            flow.response is None
            or not 300 <= flow.response.status_code < 400
            or "location" not in flow.response.headers
        ):
            return True
        if not self._redirect_matches(flow, admission):
            return False
        return not (
            self._package_auth is not None
            and admission.rule_id == self._package_auth.rule_id
            and not self._package_redirect_matches(flow)
        )

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

    def _finish(self, flow: http.HTTPFlow) -> None:
        self._active.discard(flow.id)
        self.retained.release(f"{flow.id}:request")
        self.retained.release(f"{flow.id}:response")

    @staticmethod
    def _deny(flow: http.HTTPFlow, status: int, body: bytes) -> None:
        if flow.response is None:
            flow.response = http.Response.make(status, body, {"content-type": "text/plain"})
            return
        flow.response.status_code = status
        flow.response.reason = ""
        flow.response.headers.clear()
        flow.response.headers["content-length"] = "0"
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

    def _package_request_matches(self, request: http.Request, admission: Admission) -> bool:
        package_auth = self._package_auth
        assert package_auth is not None
        return (
            admission.host == package_auth.host
            and admission.port == package_auth.port
            and request.scheme == "https"
            and _path_is_scoped(request.path, package_auth.path_prefix)
        )

    def _package_redirect_matches(self, flow: http.HTTPFlow) -> bool:
        package_auth = self._package_auth
        assert package_auth is not None and flow.response is not None
        try:
            target = urlsplit(urljoin(flow.request.pretty_url, flow.response.headers["location"]))
            port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False
        return (
            target.scheme == "https"
            and _normalize_host(target.hostname or "") == package_auth.host
            and port == package_auth.port
            and _path_is_scoped(target.path, package_auth.path_prefix)
        )

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
                len(message.http_version) + len(str(message.status_code)) + len(message.reason) + 4
            )
        return size


def _normalized_path(value: str) -> str:
    try:
        path = urlsplit(value).path
        for _ in range(4):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        else:
            return ""
    except ValueError:
        return ""
    return path if not any(part in {".", ".."} for part in path.split("/")) else ""


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


def _expiration(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _diagnostic_suffix(stderr: str) -> str:
    text = re.sub(r"(?i)(authorization(?:token)?\s*[=:]\s*)\S+", r"\1REDACTED", stderr.strip())
    text = re.sub(r"(https://[^\s:]+:)[^@\s]+@", r"\1REDACTED@", text)
    return f": {text[:1000]}" if text else ""


def load_policy(fd: int) -> tuple[tuple[RequestPolicy, ...], tuple[HeaderSecret, ...]]:
    with os.fdopen(fd, "rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("mediator policy is too large")
    try:
        payload = json.loads(raw)
        if set(payload) != {"version", "rules", "secrets"} or payload["version"] != 1:
            raise ValueError
        if not isinstance(payload["rules"], list) or not isinstance(payload["secrets"], list):
            raise ValueError
        policies = []
        for item in payload["rules"]:
            if not isinstance(item, dict) or set(item) != {
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
            policies.append(RequestPolicy(item["id"], host, ports, methods, paths, tuple(hooks)))
        secrets = []
        for item in payload["secrets"]:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "hosts",
                "ports",
                "methods",
                "path_prefix",
                "header",
                "placeholder",
                "value",
            }:
                raise ValueError
            hosts = tuple(_normalize_policy_host(host, exact=True) for host in item["hosts"])
            ports = _policy_ports(item["ports"])
            methods = _policy_methods(item["methods"])
            path_prefix = item["path_prefix"]
            if path_prefix is not None:
                path_prefix = _policy_paths([path_prefix])[0]
            header = item["header"]
            placeholder = item["placeholder"]
            value = item["value"]
            if (
                not _valid_field(item["name"])
                or not isinstance(header, str)
                or header.count("{placeholder}") != 1
                or ":" not in header
                or not isinstance(placeholder, str)
                or not placeholder.startswith("SBX_SECRET_")
                or len(placeholder) < 32
                or not _valid_field(placeholder)
                or not isinstance(value, str)
                or not value
            ):
                raise ValueError
            header_name, template = (part.strip() for part in header.split(":", 1))
            if (
                re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header_name) is None
                or not _valid_header_value(template.replace("{placeholder}", placeholder))
                or not _valid_header_value(template.replace("{placeholder}", value))
            ):
                raise ValueError
            secrets.append(
                HeaderSecret(
                    item["name"],
                    hosts,
                    ports,
                    methods,
                    path_prefix,
                    header_name,
                    template,
                    placeholder,
                    value,
                )
            )
        if len({policy.rule_id for policy in policies}) != len(policies) or len(
            {secret.name for secret in secrets}
        ) != len(secrets):
            raise ValueError
        return tuple(policies), tuple(secrets)
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


def load_package_auth(fd: int) -> tuple[PackageAuth, Callable[[], str]]:
    with os.fdopen(fd, "rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("package-auth configuration is too large")
    try:
        payload = json.loads(raw)
        if (
            set(payload)
            != {
                "version",
                "rule_id",
                "host",
                "port",
                "path_prefix",
                "username",
                "placeholder",
                "provider",
            }
            or payload["version"] != 1
        ):
            raise ValueError
        host = _normalize_host(payload["host"])
        port = payload["port"]
        path_prefix = payload["path_prefix"]
        placeholder = payload["placeholder"]
        if (
            host is None
            or host != payload["host"]
            or type(port) is not int
            or not 0 < port <= 65535
            or not isinstance(path_prefix, str)
            or path_prefix == "/"
            or not path_prefix.startswith("/")
            or not path_prefix.endswith("/")
            or ".." in path_prefix.split("/")
            or "?" in path_prefix
            or "#" in path_prefix
            or not isinstance(placeholder, str)
            or not placeholder.startswith("SBX_SECRET_")
            or len(placeholder) < 32
            or not _valid_field(placeholder)
        ):
            raise ValueError
        for name in ("rule_id", "username"):
            if not isinstance(payload[name], str) or not _valid_field(payload[name]):
                raise ValueError
        provider = payload["provider"]
        if not isinstance(provider, dict) or "type" not in provider:
            raise ValueError
        if provider["type"] == "command" and set(provider) == {"type", "argv"}:
            argv = provider["argv"]
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) and item for item in argv)
            ):
                raise ValueError
            token_provider: CommandTokenProvider | AwsCodeArtifactTokenProvider = (
                CommandTokenProvider(tuple(argv))
            )
        elif (
            provider["type"] == "aws-codeartifact"
            and set(provider)
            <= {
                "type",
                "domain",
                "domain_owner",
                "region",
                "profile",
            }
            and set(provider) >= {"type", "domain", "domain_owner", "region"}
        ):
            if not all(
                isinstance(provider[name], str) and provider[name]
                for name in ("domain", "domain_owner", "region")
            ) or ("profile" in provider and not isinstance(provider["profile"], str)):
                raise ValueError
            token_provider = AwsCodeArtifactTokenProvider(
                domain=provider["domain"],
                domain_owner=provider["domain_owner"],
                region=provider["region"],
                profile=provider.get("profile"),
            )
        else:
            raise ValueError
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid package-auth configuration") from exc
    package_auth = PackageAuth(
        payload["rule_id"],
        host,
        port,
        path_prefix,
        payload["username"],
        placeholder,
    )
    return package_auth, token_provider.get_token


def _load() -> GatewayAddon:
    fd = int(os.environ.pop("SBX_GATEWAY_KEY_FD"))
    ready = os.environ.pop("SBX_GATEWAY_READY_FD", None)
    package_auth_fd = os.environ.pop("SBX_GATEWAY_PACKAGE_AUTH_FD", None)
    policy_fd = os.environ.pop("SBX_GATEWAY_POLICY_FD", None)
    with os.fdopen(fd, "rb") as source:
        key = source.read(33)
    package_auth, get_real_token = (
        load_package_auth(int(package_auth_fd)) if package_auth_fd is not None else (None, None)
    )
    policies, secrets = load_policy(int(policy_fd)) if policy_fd is not None else ((), ())
    return GatewayAddon(
        CapabilityVerifier(
            key,
            os.environ.pop("SBX_GATEWAY_VM_ID"),
            os.environ.pop("SBX_GATEWAY_INCARNATION"),
        ),
        int(ready) if ready is not None else None,
        package_auth,
        get_real_token,
        policies=policies,
        secrets=secrets,
    )


addons = [_load()]
