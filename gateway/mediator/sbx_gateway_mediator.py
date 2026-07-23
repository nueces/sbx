"""Authenticated per-VM mitmproxy policy addon for the sbx gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
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


class CredentialProviderError(RuntimeError):
    pass


class CommandTokenProvider:
    def __init__(
        self,
        argv: tuple[str, ...],
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._argv = argv
        self._runner = runner

    def get_token(self) -> str:
        try:
            result = self._runner(self._argv, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise CredentialProviderError("credential command could not be started") from exc
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
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
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
            try:
                result = self._runner(self._argv, text=True, capture_output=True, check=False)
            except OSError as exc:
                raise CredentialProviderError("aws CLI could not be started") from exc
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
    def __init__(
        self,
        verifier: CapabilityVerifier,
        ready_fd: int | None = None,
        package_auth: PackageAuth | None = None,
        get_real_token: Callable[[], str] | None = None,
    ) -> None:
        self._verifier = verifier
        self._ready_fd = ready_fd
        self._package_auth = package_auth
        self._get_real_token = get_real_token
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
        if self._package_auth is not None and admission.rule_id == self._package_auth.rule_id:
            if not self._package_request_matches(flow.request, admission):
                self._deny(flow, 403, b"sbx gateway denied package authorization")
                return
            parsed = _parse_basic_authorization(flow.request.headers.get("Authorization"))
            if parsed != (self._package_auth.username, self._package_auth.placeholder):
                self._deny(flow, 403, b"sbx gateway denied package authorization")
                return
            if self._get_real_token is None:
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
            if (
                self._package_auth is not None
                and admission.rule_id == self._package_auth.rule_id
                and not self._package_redirect_matches(flow)
            ):
                self._deny(flow, 502, b"sbx gateway denied package redirect")
                return
        flow.response.stream = True

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
        if hasattr(client, "sbx_admission"):
            del client.sbx_admission

    def _finish(self, flow: http.HTTPFlow) -> None:
        self._active.discard(flow.id)
        self.retained.release(flow.id)

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


def _path_is_scoped(value: str, prefix: str) -> bool:
    path = unquote(urlsplit(value).path)
    return path.startswith(prefix) and not any(part in {".", ".."} for part in path.split("/"))


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
    with os.fdopen(fd, "rb") as source:
        key = source.read(33)
    package_auth, get_real_token = (
        load_package_auth(int(package_auth_fd)) if package_auth_fd is not None else (None, None)
    )
    return GatewayAddon(
        CapabilityVerifier(
            key,
            os.environ.pop("SBX_GATEWAY_VM_ID"),
            os.environ.pop("SBX_GATEWAY_INCARNATION"),
        ),
        int(ready) if ready is not None else None,
        package_auth,
        get_real_token,
    )


addons = [_load()]
