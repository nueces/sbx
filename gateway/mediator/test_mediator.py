from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import select
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, ResponseReceived, StreamEnded
from mitmproxy import http
from mitmproxy.test import tflow

from sbx import gateway

read_fd, write_fd = os.pipe()
os.write(write_fd, b"x" * 32)
os.close(write_fd)
os.environ["SBX_GATEWAY_KEY_FD"] = str(read_fd)
os.environ["SBX_GATEWAY_VM_ID"] = "vm1"
os.environ["SBX_GATEWAY_INCARNATION"] = "worker1"

from launch import command  # noqa: E402
from sbx_gateway_mediator import (  # noqa: E402
    MAX_ACTIVE_REQUESTS,
    MAX_RETAINED_BODY,
    MAX_RETAINED_TOTAL,
    MAX_TLS_CONTEXTS,
    MAX_TLS_HANDSHAKES,
    Admission,
    CapabilityVerifier,
    CredentialProviderError,
    GatewayAddon,
    HeaderCredential,
    Hook,
    RequestPolicy,
    RetainedBufferBudget,
    _basic_authorization,
    _normalized_path,
    _parse_basic_authorization,
    load_policy,
)

KEY = b"k" * 32
PLACEHOLDER = "p" * 43
FAKE_TOKEN = "fake-real-token-task10"
FIXTURE_LAUNCHER = Path(__file__).with_name("fixture_launch.py")


def credential(
    key: bytes = KEY,
    *,
    vm: str = "vm1",
    incarnation: str = "worker1",
    flow: str = "flow1",
    rule: str = "rule1",
    host: str = "localhost",
    port: int = 443,
    issued: int | None = None,
    expires: int | None = None,
) -> tuple[str, str]:
    issued = int(time.time()) if issued is None else issued
    expires = issued + 30 if expires is None else expires
    nonce = base64.urlsafe_b64encode(os.urandom(12)).rstrip(b"=").decode()
    username = f"1|{vm}|{incarnation}|{flow}|{rule}|{issued}|{expires}|{nonce}|{host}|{port}"
    digest = hmac.new(key, username.encode(), hashlib.sha256).digest()
    password = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return username, password


class SocketReader:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()

    def until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("connection closed")
            self.buffer.extend(data)
        end = self.buffer.index(marker) + len(marker)
        data = bytes(self.buffer[:end])
        del self.buffer[:end]
        return data

    def exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("connection closed")
            self.buffer.extend(data)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def line(self) -> bytes:
        return self.until(b"\r\n")


def _certificate_files(directory: Path) -> tuple[Path, Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    root_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "sbx test root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    leaf_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(root_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    root_path, cert_path, key_path = (
        directory / "root.pem",
        directory / "server.pem",
        directory / "server-key.pem",
    )
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return root_path, cert_path, key_path


class ProtocolFixture:
    def __init__(self, directory: Path) -> None:
        self.root_ca, cert, key = _certificate_files(directory)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.context.set_alpn_protocols(["h2", "http/1.1"])
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.paths: list[str] = []
        self.http_started = threading.Event()
        self.authorizations: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopped.set()
        self.listener.close()
        self._thread.join(timeout=2)

    def count(self, path: str) -> int:
        with self._lock:
            return self.paths.count(path)

    def _record(self, path: str, authorization: str = "") -> None:
        with self._lock:
            self.paths.append(path)
            self.authorizations.append((path, authorization))

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._connection, args=(conn,), daemon=True).start()

    def _connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            if conn.recv(1, socket.MSG_PEEK) == b"\x16":
                conn = self.context.wrap_socket(conn, server_side=True)
            if isinstance(conn, ssl.SSLSocket) and conn.selected_alpn_protocol() == "h2":
                self._http2(conn)
            else:
                self._http1(conn)
        except (EOFError, OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def _http1(self, conn: socket.socket) -> None:
        reader = SocketReader(conn)
        while True:
            try:
                first = reader.exact(1)
                self.http_started.set()
                reader.buffer[:0] = first
                block = reader.until(b"\r\n\r\n")
            except (EOFError, TimeoutError):
                return
            lines = block[:-4].split(b"\r\n")
            method, target, _ = lines[0].decode().split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, value = line.decode("latin1").split(":", 1)
                headers[name.lower()] = value.strip()
            path = urlsplit(target).path
            body = b""
            if headers.get("transfer-encoding", "").lower() == "chunked":
                while True:
                    size = int(reader.line().strip().split(b";", 1)[0], 16)
                    if size == 0:
                        reader.until(b"\r\n")
                        break
                    body += reader.exact(size)
                    if reader.exact(2) != b"\r\n":
                        return
            elif "content-length" in headers:
                body = reader.exact(int(headers["content-length"]))
            self._record(path, headers.get("authorization", ""))
            if path == "/websocket":
                self._websocket(conn, reader, headers)
                return
            if path == "/redirect-ok":
                self._response(conn, 302, b"", {"Location": "/ok"})
            elif path == "/redirect-bad":
                self._response(conn, 302, b"", {"Location": "https://other.example/"})
            elif path == "/pypi/repo/redirect-escape":
                self._response(conn, 302, b"", {"Location": "/outside/"})
            elif path == "/many-response":
                extra = {f"X-Test-{index}": "x" for index in range(101)}
                self._response(conn, 200, b"upstream-limit-bypassed", extra)
            elif path == "/large-response":
                self._response(conn, 200, b"upstream-limit-bypassed", {"X-Large": "x" * 65536})
            elif path == "/stream":
                payload = b"stream:" + str(len(body)).encode() + b":" + b"z" * 131072
                head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                conn.sendall(head)
                for offset in range(0, len(payload), 4096):
                    chunk = payload[offset : offset + 4096]
                    conn.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                conn.sendall(b"0\r\n\r\n")
            else:
                self._response(conn, 200, b"ok:" + method.encode())
            if headers.get("connection", "").lower() == "close":
                return

    @staticmethod
    def _response(
        conn: socket.socket,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        reasons = {200: "OK", 302: "Found"}
        values = {"Content-Length": str(len(body)), "Connection": "keep-alive"}
        values.update(headers or {})
        head = f"HTTP/1.1 {status} {reasons[status]}\r\n".encode()
        head += b"".join(f"{name}: {value}\r\n".encode() for name, value in values.items())
        conn.sendall(head + b"\r\n" + body)

    @staticmethod
    def _websocket(conn: socket.socket, reader: SocketReader, headers: dict[str, str]) -> None:
        accept = base64.b64encode(
            hashlib.sha1(
                (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        while True:
            first, length = reader.exact(2)
            if first & 0x0F != 1 or not length & 0x80:
                return
            size = length & 0x7F
            if size == 126:
                size = struct.unpack("!H", reader.exact(2))[0]
            mask = reader.exact(4)
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(reader.exact(size))
            )
            conn.sendall(bytes([0x81, len(payload)]) + payload)

    def _http2(self, conn: socket.socket) -> None:
        h2 = H2Connection(config=H2Configuration(client_side=False, header_encoding="utf-8"))
        requests: dict[int, dict[str, str]] = {}
        h2.initiate_connection()
        conn.sendall(h2.data_to_send())
        while True:
            data = conn.recv(65536)
            if not data:
                return
            for event in h2.receive_data(data):
                if isinstance(event, RequestReceived):
                    self.http_started.set()
                    requests[event.stream_id] = dict(event.headers)
                elif isinstance(event, DataReceived):
                    h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    path = requests[event.stream_id][":path"]
                    self._record(path, requests[event.stream_id].get("authorization", ""))
                    body = b"h2-ok"
                    h2.send_headers(
                        event.stream_id,
                        [(":status", "200"), ("content-length", str(len(body)))],
                    )
                    h2.send_data(event.stream_id, body, end_stream=True)
            pending = h2.data_to_send()
            if pending:
                conn.sendall(pending)


class MediatorProcess:
    def __init__(
        self,
        directory: Path,
        upstream_ca: Path,
        *,
        mitmweb: bool = False,
        policy: dict | None = None,
        admission_gate: threading.Event | None = None,
        allow_fixture_loopback: bool = True,
    ) -> None:
        self.confdir = Path(tempfile.mkdtemp(prefix="mitmproxy-", dir=directory))
        self.stdout = ""
        self.stderr = ""
        key_read, key_write = os.pipe()
        ready_read, ready_write = os.pipe()
        lifetime_read, self.lifetime_write = os.pipe()
        self.broker, broker_child = socket.socketpair()

        def broker_server() -> None:
            reader = SocketReader(self.broker)
            try:
                while True:
                    size = struct.unpack("!I", reader.exact(4))[0]
                    request = json.loads(reader.exact(size))
                    response = {
                        "version": 1,
                        "request_id": request["request_id"],
                        "incarnation": "worker1",
                    }
                    response["value"] = FAKE_TOKEN
                    raw = json.dumps(response, separators=(",", ":")).encode()
                    self.broker.sendall(struct.pack("!I", len(raw)) + raw)
            except (EOFError, OSError):
                pass
            except BaseException:
                pass

        threading.Thread(target=broker_server, daemon=True).start()
        os.write(key_write, KEY)
        os.close(key_write)
        argv = [
            sys.executable,
            *([str(FIXTURE_LAUNCHER)] if allow_fixture_loopback else []),
            str(Path(__file__).with_name("launch.py")),
            "--vm-id",
            "vm1",
            "--incarnation",
            "worker1",
            "--key-fd",
            str(key_read),
            "--ready-fd",
            str(ready_write),
            "--broker-fd",
            str(broker_child.fileno()),
            "--lifetime-fd",
            str(lifetime_read),
            "--confdir",
            str(self.confdir),
            "--upstream-ca",
            str(upstream_ca),
        ]
        pass_fds = [key_read, ready_write, broker_child.fileno(), lifetime_read]
        extra_fds = []
        if mitmweb:
            argv.append("--mitmweb")
        if policy is not None:
            policy_read, policy_write = os.pipe()
            os.write(policy_write, json.dumps(policy).encode())
            os.close(policy_write)
            pass_fds.append(policy_read)
            extra_fds.append(policy_read)
            argv.extend(("--policy-fd", str(policy_read)))
        config = gateway.parse_config({"network": {}})
        assert config is not None
        self.audit_controller = gateway.GatewayController(config, "vm1", root=directory)
        self.audit_controller.directory = self.confdir
        self.audit_controller.generation = "test-generation"
        self.audit_controller.worker_incarnation = "worker1"
        self.audit_stream = self.audit_controller._open_log("audit.log")
        self.admission_waiting = threading.Event()
        original_sync = self.audit_controller._sync_audit

        def sync(destination):
            self.admission_waiting.set()
            if admission_gate is not None and not admission_gate.wait(timeout=3):
                raise OSError("test admission gate timed out")
            original_sync(destination)

        self.audit_controller._sync_audit = sync
        audit_read, audit_fd = os.pipe()
        ack_fd, ack_write = os.pipe()
        self.audit_controller._route_audit(
            os.fdopen(audit_read, "rb", buffering=0), self.audit_stream, ack_write
        )
        pass_fds.extend((audit_fd, ack_fd))
        extra_fds.extend((audit_fd, ack_fd))
        argv.extend(
            (
                "--audit-fd",
                str(audit_fd),
                "--audit-ack-fd",
                str(ack_fd),
                "--generation",
                "test-generation",
            )
        )
        self.process = subprocess.Popen(
            argv,
            pass_fds=tuple(pass_fds),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for fd in extra_fds:
            os.close(fd)
        os.close(key_read)
        os.close(ready_write)
        os.close(lifetime_read)
        broker_child.close()
        readable, _, _ = select.select([ready_read], [], [], 10)
        payload = os.read(ready_read, 4096) if readable else b""
        os.close(ready_read)
        if not payload:
            self.close()
            raise RuntimeError("mediator failed readiness")
        ready = json.loads(payload)
        self.address = (ready["host"], ready["port"])
        self.web_port = ready.get("web_port")
        self.ca = self.confdir / "mitmproxy-ca-cert.pem"
        if not self.ca.is_file():
            self.close()
            raise RuntimeError("mediator did not create its CA")

    def close(self) -> None:
        os.close(self.lifetime_write)
        self.broker.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate(timeout=5)
        for thread in self.audit_controller._log_threads:
            thread.join(timeout=4)
        self.audit_stream.close()
        if self.process.returncode not in {0, -15}:
            raise RuntimeError(
                f"mediator exited {self.process.returncode}: {self.stderr} {self.stdout}"
            )


class CapabilityTests(unittest.TestCase):
    def test_exactly_once_and_identity_bound(self) -> None:
        verifier = CapabilityVerifier(KEY, "vm1", "worker1")
        username, password = credential(host="example.com")
        admission = verifier.verify(username, password)
        self.assertEqual((admission.host, admission.port), ("example.com", 443))
        self.assertIsNone(verifier.verify(username, password))

        for values in ({"vm": "vm2"}, {"incarnation": "worker0"}):
            altered, altered_password = credential(host="example.com", **values)
            self.assertIsNone(verifier.verify(altered, altered_password))
        self.assertIsNone(
            verifier.verify(username.replace("example.com", "other.example"), password)
        )
        self.assertIsNone(verifier.verify(username.removesuffix("443") + "80", password))

    def test_replacement_rejects_every_old_worker_capability(self) -> None:
        username, password = credential(key=KEY, incarnation="worker1", flow="old-flow")
        replacement = CapabilityVerifier(b"r" * 32, "vm1", "worker2")

        self.assertIsNone(replacement.verify(username, password))

    def test_rejects_expired_malformed_and_altered_credentials(self) -> None:
        now = int(time.time())
        verifier = CapabilityVerifier(KEY, "vm1", "worker1")
        expired, password = credential(issued=now - 31, expires=now - 1)
        self.assertIsNone(verifier.verify(expired, password, now=now))
        username, password = credential(issued=now)
        invalid = [
            (username, password + "x"),
            (username.replace("rule1", "rule2"), password),
            (username.replace("localhost", "LOCALHOST"), password),
            (username.replace(username.split("|")[7], "!"), password),
        ]
        for user, secret in invalid:
            self.assertIsNone(verifier.verify(user, secret, now=now))


class CredentialInjectionTests(unittest.TestCase):
    class Broker:
        def __init__(self, value: str = FAKE_TOKEN) -> None:
            self.value = value

        def fetch(self, credential: str) -> str:
            if credential != "token":
                raise CredentialProviderError("unavailable")
            return self.value

    def _addon(self, *, basic: bool = True) -> tuple[GatewayAddon, object]:
        binding = HeaderCredential(
            "token",
            "rule1",
            ("GET",),
            ("/pypi/repo/",),
            "Authorization",
            "basic-password" if basic else "template",
            "aws" if basic else "Bearer {placeholder}",
            PLACEHOLDER,
        )
        addon = GatewayAddon(
            CapabilityVerifier(KEY, "vm1", "worker1"),
            policies=(RequestPolicy("rule1", "localhost", (443,), ("GET",), ("/",)),),
            credentials=(binding,),
            broker=self.Broker(),  # type: ignore[arg-type]
        )
        client = tflow.tclient_conn()
        addon._admissions[client.id] = Admission(
            "vm1", "worker1", "flow", "rule1", "localhost", 443, 1, 2
        )
        return addon, client

    def _flow(
        self, client: object, authorization: str, path: str = "/pypi/repo/simple/"
    ) -> http.HTTPFlow:
        return tflow.tflow(
            client_conn=client,
            req=http.Request.make(
                "GET", f"https://localhost{path}", headers={"Authorization": authorization}
            ),
        )

    def test_exact_basic_and_literal_substitution(self) -> None:
        addon, client = self._addon()
        flow = self._flow(client, _basic_authorization("aws", PLACEHOLDER))
        addon.requestheaders(flow)
        self.assertEqual(
            flow.request.headers["Authorization"], _basic_authorization("aws", FAKE_TOKEN)
        )

        addon, client = self._addon(basic=False)
        flow = self._flow(client, f"Bearer {PLACEHOLDER}")
        addon.requestheaders(flow)
        self.assertEqual(flow.request.headers["Authorization"], f"Bearer {FAKE_TOKEN}")

    def test_wrong_scope_or_placeholder_fails_closed(self) -> None:
        for authorization, path in (
            ("Bearer wrong", "/pypi/repo/"),
            (f"Bearer {PLACEHOLDER}", "/outside/"),
        ):
            addon, client = self._addon(basic=False)
            flow = self._flow(client, authorization, path)
            addon.requestheaders(flow)
            self.assertEqual(flow.response.status_code, 403)


class MalformedInputTests(unittest.TestCase):
    @staticmethod
    def _parse(parser: object, payload: bytes) -> None:
        with tempfile.TemporaryFile() as source:
            source.write(payload)
            source.seek(0)
            with suppress(ValueError):
                parser(os.dup(source.fileno()))  # type: ignore[operator]

    def test_bounded_malformed_policy_package_and_secret_fields(self) -> None:
        random_source = random.Random(19)
        payloads = [b"", b"null", b"[]", b"{}", b"{" * 65537]
        payloads.extend(
            random_source.randbytes(random_source.randrange(0, 70000)) for _ in range(500)
        )
        started = time.monotonic()
        for payload in payloads:
            self._parse(load_policy, payload)
            text = payload[:4096].decode("latin1")
            _parse_basic_authorization(text)
            _normalized_path(text)
        self.assertLess(time.monotonic() - started, 10)


class HookAuthorizationTests(unittest.TestCase):
    def _addon(
        self,
        *,
        hooks: dict[str, Hook] | None = None,
        policy: RequestPolicy | None = None,
        credentials: tuple[HeaderCredential, ...] = (),
        timeout: float = 1.0,
    ) -> tuple[GatewayAddon, object]:
        policy = policy or RequestPolicy("rule1", "localhost", (443,), hooks=tuple(hooks or ()))
        addon = GatewayAddon(
            CapabilityVerifier(KEY, "vm1", "worker1"),
            policies=(policy,),
            credentials=credentials,
            broker=CredentialInjectionTests.Broker(),  # type: ignore[arg-type]
            hooks=hooks or {},
            hook_timeout=timeout,
        )
        client = tflow.tclient_conn()
        addon._admissions[client.id] = Admission(
            "vm1", "worker1", "flow", "rule1", "localhost", 443, 1, 2
        )
        return addon, client

    @staticmethod
    def _flow(
        client: object,
        *,
        method: str = "GET",
        path: str = "/allowed/item",
        headers: dict[str, str] | None = None,
    ) -> http.HTTPFlow:
        return tflow.tflow(
            client_conn=client,
            req=http.Request.make(method, f"https://localhost{path}", headers=headers or {}),
        )

    def test_request_and_response_hooks_are_ordered_and_mutations_are_deterministic(self) -> None:
        events: list[str] = []

        def header_hook(name: str, phase: str):
            def run(_context: object, headers: http.Headers) -> str:
                events.append(f"{phase}:{name}")
                headers[f"X-{phase}"] = headers.get(f"X-{phase}", "") + name
                return "continue"

            return run

        hooks = {
            "one": Hook(header_hook("1", "request"), None, header_hook("1", "response")),
            "two": Hook(header_hook("2", "request"), None, header_hook("2", "response")),
        }
        addon, client = self._addon(hooks=hooks)
        flow = self._flow(client)

        addon.requestheaders(flow)
        self.assertIsNone(flow.response)
        self.assertEqual(flow.request.headers["X-request"], "12")
        flow.response = http.Response.make(200, b"ok")
        addon.responseheaders(flow)

        self.assertEqual(flow.response.headers["X-response"], "12")
        self.assertEqual(events, ["request:1", "request:2", "response:1", "response:2"])

    def test_request_policy_and_structural_secret_scope(self) -> None:
        secret = HeaderCredential(
            "token",
            "rule1",
            ("GET",),
            ("/allowed",),
            "Authorization",
            "template",
            "Bearer {placeholder}",
            PLACEHOLDER,
        )
        policy = RequestPolicy("rule1", "localhost", (443,), ("GET",), ("/",))
        addon, client = self._addon(policy=policy, credentials=(secret,))
        flow = self._flow(client, headers={"Authorization": f"Bearer {PLACEHOLDER}"})
        addon.requestheaders(flow)
        self.assertEqual(flow.request.headers["Authorization"], f"Bearer {FAKE_TOKEN}")

        other_placeholder = "other-opaque-placeholder-value-123456789012345"
        other = HeaderCredential(
            "other",
            "other-rule",
            (),
            (),
            "X-Other-Token",
            "template",
            "Bearer {placeholder}",
            other_placeholder,
        )
        addon, client = self._addon(policy=policy, credentials=(secret, other))
        flow = self._flow(
            client,
            headers={
                "Authorization": f"Bearer {PLACEHOLDER}",
                "X-Other-Token": f"Bearer {other_placeholder}",
            },
        )
        addon.requestheaders(flow)
        self.assertEqual(flow.response.status_code, 403)

        for name, method, path, headers in (
            ("wrong-method", "POST", "/allowed/item", {"Authorization": f"Bearer {PLACEHOLDER}"}),
            ("wrong-path", "GET", "/outside", {"Authorization": f"Bearer {PLACEHOLDER}"}),
            ("wrong-field", "GET", "/allowed/item", {"X-Token": f"Bearer {PLACEHOLDER}"}),
            ("malformed-value", "GET", "/allowed/item", {"Authorization": PLACEHOLDER}),
        ):
            with self.subTest(name):
                addon, client = self._addon(policy=policy, credentials=(secret,))
                flow = self._flow(client, method=method, path=path, headers=headers)
                addon.requestheaders(flow)
                self.assertEqual(flow.response.status_code, 403)
                self.assertNotIn(FAKE_TOKEN.encode(), flow.response.content)
                self.assertNotIn(PLACEHOLDER.encode(), flow.response.content)

    def test_streaming_default_chunk_mutation_and_explicit_bounded_buffering(self) -> None:
        streaming = {
            "stream": Hook(
                on_request_chunk=lambda _context, chunk: chunk.upper(),
                on_response_chunk=lambda _context, chunk: chunk[::-1],
            )
        }
        addon, client = self._addon(hooks=streaming)
        flow = self._flow(client)
        addon.requestheaders(flow)
        self.assertTrue(callable(flow.request.stream))
        self.assertEqual(flow.request.stream(b"chunk"), b"CHUNK")
        flow.response = http.Response.make(200, b"body")
        addon.responseheaders(flow)
        self.assertTrue(callable(flow.response.stream))
        self.assertEqual(flow.response.stream(b"response"), b"esnopser")
        self.assertEqual(addon.retained.total, 0)

        buffered = {
            "buffer": Hook(
                on_request_chunk=lambda _context, body: b"[" + body + b"]",
                request_buffer_limit=4,
            )
        }
        addon, client = self._addon(hooks=buffered)
        flow = self._flow(client)
        addon.requestheaders(flow)
        self.assertEqual(flow.request.stream(b"ab"), b"")
        self.assertEqual(addon.retained.total, 2)
        self.assertEqual(flow.request.stream(b"cd"), b"")
        self.assertEqual(addon.retained.total, 4)
        self.assertEqual(flow.request.stream(b""), b"[abcd]")
        self.assertEqual(addon.retained.total, 0)

        addon, client = self._addon(hooks=buffered)
        flow = self._flow(client)
        addon.requestheaders(flow)
        self.assertEqual(flow.request.stream(b"abcde"), b"")
        self.assertEqual(flow.response.status_code, 413)
        self.assertEqual(addon.retained.total, 0)

    def test_hook_timeout_crash_malformed_and_destination_widening_fail_closed(self) -> None:
        def timeout(_context: object, _headers: http.Headers) -> str:
            time.sleep(0.05)
            return "continue"

        def crash(_context: object, _headers: http.Headers) -> str:
            raise RuntimeError("secret=" + FAKE_TOKEN)

        def malformed(_context: object, _headers: http.Headers) -> object:
            return {"action": "continue"}

        def widen(_context: object, headers: http.Headers) -> str:
            headers["Host"] = "other.example"
            return "continue"

        for name, callback in (
            ("timeout", timeout),
            ("crash", crash),
            ("malformed", malformed),
            ("widen", widen),
        ):
            with self.subTest(name):
                hooks = {name: Hook(on_request_headers=callback)}
                addon, client = self._addon(hooks=hooks, timeout=0.005)
                flow = self._flow(client)
                addon.requestheaders(flow)
                self.assertEqual(flow.response.status_code, 403)
                self.assertNotIn(FAKE_TOKEN.encode(), flow.response.content)

    def test_malicious_chunk_output_and_response_retargeting_fail_closed(self) -> None:
        hooks = {"bad": Hook(on_request_chunk=lambda _context, _chunk: "not-bytes")}
        addon, client = self._addon(hooks=hooks)
        flow = self._flow(client)
        addon.requestheaders(flow)
        self.assertEqual(flow.request.stream(b"data"), b"")
        self.assertEqual(flow.response.status_code, 403)

        def redirect(_context: object, headers: http.Headers) -> str:
            headers["Location"] = "https://other.example/"
            return "continue"

        hooks = {"redirect": Hook(on_response_headers=redirect)}
        addon, client = self._addon(hooks=hooks)
        flow = self._flow(client)
        addon.requestheaders(flow)
        flow.response = http.Response.make(302, b"", {"Location": "/allowed/next"})
        addon.responseheaders(flow)
        self.assertEqual(flow.response.status_code, 502)


class LimitTests(unittest.TestCase):
    def test_retained_body_per_flow_and_aggregate_limits(self) -> None:
        budget = RetainedBufferBudget()
        self.assertTrue(budget.reserve("one", MAX_RETAINED_BODY))
        self.assertFalse(budget.reserve("one", MAX_RETAINED_BODY + 1))
        for index in range(1, MAX_RETAINED_TOTAL // MAX_RETAINED_BODY):
            self.assertTrue(budget.reserve(str(index), MAX_RETAINED_BODY))
        self.assertEqual(budget.total, MAX_RETAINED_TOTAL)
        self.assertFalse(budget.reserve("overflow", 1))
        budget.release("one")
        self.assertTrue(budget.reserve("overflow", 1))

    def test_tls_handshake_and_context_caches_are_bounded(self) -> None:
        addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))

        def hello(index: int, host: str):
            client = type("Client", (), {"id": f"client-{index}"})()
            context = type("Context", (), {"client": client})()
            data = type(
                "Hello",
                (),
                {"context": context, "client_hello": type("CH", (), {"sni": host})()},
            )()
            addon._admissions[client.id] = Admission(
                "vm1", "worker1", f"flow-{index}", "rule", host, 443, 1, 2
            )
            return data

        handshakes = [hello(index, f"h{index}.example") for index in range(MAX_TLS_HANDSHAKES)]
        for data in handshakes:
            addon.tls_clienthello(data)
        denied = hello(MAX_TLS_HANDSHAKES, "denied.example")
        addon.tls_clienthello(denied)
        self.assertIn(denied.context.client.id, addon._invalid_tls)
        addon.tls_established_client(handshakes[0])
        admitted = hello(MAX_TLS_HANDSHAKES + 1, "admitted.example")
        addon.tls_clienthello(admitted)
        self.assertNotIn(admitted.context.client.id, addon._invalid_tls)

        for index in range(MAX_TLS_HANDSHAKES + 2, MAX_TLS_CONTEXTS + MAX_TLS_HANDSHAKES + 4):
            data = hello(index, f"cache-{index}.example")
            addon.tls_established_client(admitted)
            addon.tls_clienthello(data)
            admitted = data
        self.assertEqual(len(addon._tls_contexts), MAX_TLS_CONTEXTS)

    def test_active_request_limit_is_global_and_releases(self) -> None:
        addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
        client = tflow.tclient_conn()
        admission = Admission("vm1", "worker1", "flow", "rule", "localhost", 443, 1, 2)
        addon._admissions[client.id] = admission
        flows = []
        for _ in range(MAX_ACTIVE_REQUESTS + 1):
            flow = tflow.tflow(
                client_conn=client,
                req=http.Request.make("GET", "https://localhost/"),
            )
            addon.requestheaders(flow)
            flows.append(flow)
        self.assertIsNone(flows[MAX_ACTIVE_REQUESTS - 1].response)
        self.assertEqual(flows[MAX_ACTIVE_REQUESTS].response.status_code, 503)
        addon.error(flows[0])
        replacement = tflow.tflow(
            client_conn=client,
            req=http.Request.make("GET", "https://localhost/"),
        )
        addon.requestheaders(replacement)
        self.assertIsNone(replacement.response)


class MediatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        cls.fixture = ProtocolFixture(directory)
        cls.mediator = MediatorProcess(directory, cls.fixture.root_ca)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mediator.close()
        cls.fixture.close()
        cls.temp.cleanup()

    def _socks(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        host: str = "localhost",
        port: int | None = None,
    ) -> socket.socket:
        port = self.fixture.port if port is None else port
        if username is None or password is None:
            username, password = credential(flow=os.urandom(6).hex(), port=self.fixture.port)
        sock = socket.create_connection(self.mediator.address, timeout=5)
        sock.sendall(b"\x05\x01\x02")
        self.assertEqual(sock.recv(2), b"\x05\x02")
        encoded_user, encoded_password = username.encode(), password.encode()
        sock.sendall(
            b"\x01"
            + bytes([len(encoded_user)])
            + encoded_user
            + bytes([len(encoded_password)])
            + encoded_password
        )
        auth = sock.recv(2)
        if auth != b"\x01\x00":
            sock.close()
            raise PermissionError("SOCKS authentication denied")
        encoded_host = host.encode()
        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(encoded_host)])
            + encoded_host
            + struct.pack("!H", port)
        )
        reply = SocketReader(sock).exact(10)
        if reply[1] != 0:
            sock.close()
            raise ConnectionError("SOCKS destination denied")
        return sock

    def _tls(self, *, server_hostname: str = "localhost") -> ssl.SSLSocket:
        context = ssl.create_default_context(cafile=str(self.mediator.ca))
        context.set_alpn_protocols(["http/1.1"])
        return context.wrap_socket(self._socks(), server_hostname=server_hostname)

    @staticmethod
    def _request(sock: socket.socket, request: bytes) -> tuple[int, dict[str, str], bytes]:
        sock.sendall(request)
        reader = SocketReader(sock)
        block = reader.until(b"\r\n\r\n")
        lines = block[:-4].split(b"\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            name, value = line.decode("latin1").split(":", 1)
            headers[name.lower()] = value.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(reader.line().strip(), 16)
                if size == 0:
                    reader.until(b"\r\n")
                    break
                body += reader.exact(size)
                reader.exact(2)
        else:
            body = reader.exact(int(headers.get("content-length", "0")))
        return status, headers, body

    def test_startup_uses_pinned_private_loopback_configuration(self) -> None:
        argv = command(self.mediator.confdir, self.fixture.root_ca)
        rendered = " ".join(argv)
        self.assertIn("mode=socks5@127.0.0.1:0", rendered)
        self.assertIn("flow_detail=0", rendered)
        self.assertIn("store_streamed_bodies=false", rendered)
        self.assertNotIn("web_host", rendered)
        self.assertEqual(self.mediator.address[0], "127.0.0.1")
        self.assertTrue(
            all(path.stat().st_mode & 0o077 == 0 for path in self.mediator.confdir.iterdir())
        )

        isolated = MediatorProcess(Path(self.temp.name), self.fixture.root_ca)
        try:
            self.assertNotEqual(
                hashlib.sha256(self.mediator.ca.read_bytes()).digest(),
                hashlib.sha256(isolated.ca.read_bytes()).digest(),
            )
        finally:
            isolated.close()

    def test_mediated_tls_requires_per_vm_ca_trust(self) -> None:
        untrusted = ssl.create_default_context()
        raw = self._socks()
        with self.assertRaises(ssl.SSLCertVerificationError):
            untrusted.wrap_socket(raw, server_hostname="localhost")
        raw.close()

        trusted = self._tls()
        status, _, body = self._request(
            trusted,
            f"GET /trusted HTTP/1.1\r\nHost: localhost:{self.fixture.port}\r\n\r\n".encode(),
        )
        self.assertEqual((status, body), (200, b"ok:GET"))
        trusted.close()

    def test_socks_capability_failure_matrix_and_destination_binding(self) -> None:
        valid = credential(flow="replay", port=self.fixture.port)
        self._socks(username=valid[0], password=valid[1]).close()
        failures = [
            valid,
            credential(vm="vm2", flow="cross-vm", port=self.fixture.port),
            credential(incarnation="worker0", flow="old-worker", port=self.fixture.port),
            credential(key=b"z" * 32, flow="wrong-key", port=self.fixture.port),
            credential(
                flow="expired",
                port=self.fixture.port,
                issued=int(time.time()) - 31,
                expires=int(time.time()) - 1,
            ),
        ]
        for username, password in failures:
            with self.assertRaises(PermissionError):
                self._socks(username=username, password=password)

        username, password = credential(flow="retarget-host", port=self.fixture.port)
        retargeted = self._socks(
            username=username,
            password=password,
            host="127.0.0.1",
            port=self.fixture.port,
        )
        status, _, _ = self._request(
            retargeted,
            f"GET /wrong-target HTTP/1.1\r\nHost: 127.0.0.1:{self.fixture.port}\r\n\r\n".encode(),
        )
        self.assertEqual(status, 403)
        retargeted.close()

        username, password = credential(flow="retarget-port", port=self.fixture.port)
        wrong_port = self._socks(
            username=username,
            password=password,
            port=self.fixture.port + 1,
        )
        status, _, _ = self._request(
            wrong_port,
            f"GET /wrong-port HTTP/1.1\r\nHost: localhost:{self.fixture.port + 1}\r\n\r\n".encode(),
        )
        self.assertEqual(status, 403)
        wrong_port.close()

    def test_http1_streaming_redirect_reuse_and_absolute_uri(self) -> None:
        sock = self._tls()
        authority = f"localhost:{self.fixture.port}"
        status, _, body = self._request(
            sock,
            f"GET /ok HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode(),
        )
        self.assertEqual((status, body), (200, b"ok:GET"))
        sock.close()

        sock = self._tls()
        status, _, body = self._request(
            sock,
            (f"GET /http10 HTTP/1.0\r\nHost: {authority}\r\nConnection: close\r\n\r\n").encode(),
        )
        self.assertEqual((status, body), (200, b"ok:GET"))
        sock.close()

        sock = self._tls()
        status, _, _ = self._request(
            sock,
            f"GET /redirect-ok HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode(),
        )
        self.assertEqual(status, 302)
        status, _, _ = self._request(
            sock,
            f"GET /redirect-bad HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode(),
        )
        self.assertEqual(status, 502)
        status, _, _ = self._request(
            sock,
            (
                f"GET /reuse-denied HTTP/1.1\r\nHost: other.example:{self.fixture.port}\r\n\r\n"
            ).encode(),
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.fixture.count("/reuse-denied"), 0)
        sock.close()

        sock = self._tls()
        status, _, _ = self._request(
            sock,
            (
                f"GET https://other.example:{self.fixture.port}/absolute HTTP/1.1\r\n"
                f"Host: localhost:{self.fixture.port}\r\n\r\n"
            ).encode(),
        )
        self.assertEqual(status, 403)
        sock.close()

        upload = b"u" * 131072
        chunks = b"".join(
            f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
            for chunk in (upload[:65536], upload[65536:])
        )
        sock = self._tls()
        status, _, body = self._request(
            sock,
            (
                f"POST /stream HTTP/1.1\r\nHost: {authority}\r\nTransfer-Encoding: chunked\r\n\r\n"
            ).encode()
            + chunks
            + b"0\r\n\r\n",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"stream:131072:"))
        self.assertGreater(len(body), 128 * 1024)
        sock.close()

    def test_http2_stream_authority_is_revalidated(self) -> None:
        context = ssl.create_default_context(cafile=str(self.mediator.ca))
        context.set_alpn_protocols(["h2"])
        sock = context.wrap_socket(self._socks(), server_hostname="localhost")
        self.assertEqual(sock.selected_alpn_protocol(), "h2")
        client = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
        client.initiate_connection()
        sock.sendall(client.data_to_send())
        authority = f"localhost:{self.fixture.port}"
        client.send_headers(
            1,
            [(":method", "GET"), (":scheme", "https"), (":authority", authority), (":path", "/h2")],
            end_stream=True,
        )
        client.send_headers(
            3,
            [
                (":method", "GET"),
                (":scheme", "https"),
                (":authority", f"other.example:{self.fixture.port}"),
                (":path", "/h2-denied"),
            ],
            end_stream=True,
        )
        sock.sendall(client.data_to_send())
        statuses: dict[int, str] = {}
        ended: set[int] = set()
        while ended != {1, 3}:
            for event in client.receive_data(sock.recv(65536)):
                if isinstance(event, ResponseReceived):
                    statuses[event.stream_id] = dict(event.headers)[":status"]
                elif isinstance(event, DataReceived):
                    client.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    ended.add(event.stream_id)
            pending = client.data_to_send()
            if pending:
                sock.sendall(pending)
        self.assertEqual(statuses, {1: "200", 3: "403"})
        self.assertEqual(self.fixture.count("/h2-denied"), 0)
        sock.close()

    def test_tls_sni_and_websocket_are_revalidated(self) -> None:
        try:
            sock = self._tls(server_hostname="other.example")
            status, _, _ = self._request(
                sock,
                f"GET /bad-sni HTTP/1.1\r\nHost: localhost:{self.fixture.port}\r\n\r\n".encode(),
            )
            self.assertNotEqual(status, 200)
            sock.close()
        except (ConnectionError, EOFError, OSError, ssl.SSLError):
            pass
        self.assertEqual(self.fixture.count("/bad-sni"), 0)

        sock = self._tls()
        websocket_key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /websocket HTTP/1.1\r\nHost: localhost:{self.fixture.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        reader = SocketReader(sock)
        sock.sendall(request)
        self.assertIn(b" 101 ", reader.until(b"\r\n\r\n").split(b"\r\n", 1)[0])
        for sequence in range(128):
            payload, mask = f"websocket-ok-{sequence}".encode(), os.urandom(4)
            masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
            self.assertEqual(reader.exact(2), bytes([0x81, len(payload)]))
            self.assertEqual(reader.exact(len(payload)), payload)
        sock.close()

    def test_request_and_response_header_limits_fail_closed(self) -> None:
        authority = f"localhost:{self.fixture.port}"
        sock = self._tls()
        many = b"".join(f"X-Test-{index}: x\r\n".encode() for index in range(101))
        status, _, _ = self._request(
            sock,
            f"GET /many-request HTTP/1.1\r\nHost: {authority}\r\n".encode() + many + b"\r\n",
        )
        self.assertEqual(status, 431)
        self.assertEqual(self.fixture.count("/many-request"), 0)
        sock.close()

        sock = self._tls()
        status, _, _ = self._request(
            sock,
            f"GET /large-request HTTP/1.1\r\nHost: {authority}\r\nX-Large: ".encode()
            + b"x" * 65536
            + b"\r\n\r\n",
        )
        self.assertEqual(status, 431)
        self.assertEqual(self.fixture.count("/large-request"), 0)
        sock.close()

        for path in ("/many-response", "/large-response"):
            sock = self._tls()
            status, _, body = self._request(
                sock,
                f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode(),
            )
            self.assertEqual(status, 502)
            self.assertNotIn(b"upstream-limit-bypassed", body)
            sock.close()

    def test_mediator_crash_has_no_listener_or_direct_fallback(self) -> None:
        directory = Path(self.temp.name)
        isolated = MediatorProcess(directory, self.fixture.root_ca)
        address = isolated.address
        isolated.close()
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
