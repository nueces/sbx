import asyncio
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_mediator
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded
from mitmproxy import http
from mitmproxy.test import tflow
from sbx_gateway_mediator import DestinationDenied
from test_mediator import KEY, Admission, CapabilityVerifier, GatewayAddon


@pytest.mark.parametrize(
    "host,expected",
    [("93.184.216.34", True), ("127.0.0.1", False), ("10.20.30.40", False), ("198.18.0.1", False)],
)
def test_literal_resolution_never_calls_dns(monkeypatch, host, expected):
    async def check():
        async def forbidden(*args, **kwargs):
            pytest.fail("IP-literal admission performed DNS")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden)
        admission = Admission("vm1", "worker1", "flow", "rule", host, 443, 1, 2)
        addon = test_mediator.GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
        if expected:
            assert await addon._resolve_destination(admission) == (host, 443)
        else:
            with pytest.raises(DestinationDenied):
                await addon._resolve_destination(admission)

    asyncio.run(check())


@pytest.mark.parametrize(
    "admitted,sni,allowed",
    [
        ("93.184.216.34", None, True),
        ("93.184.216.34", "allowed.example", False),
        ("93.184.216.34", "93.184.216.34", False),
        ("allowed.example", None, False),
        ("allowed.example", "allowed.example", True),
    ],
)
def test_sni_is_optional_only_for_explicit_literal(admitted, sni, allowed):
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    client = tflow.tclient_conn()
    addon._admissions[client.id] = Admission("vm1", "worker1", "flow", "rule", admitted, 443, 1, 2)
    addon.tls_clienthello(
        SimpleNamespace(
            context=SimpleNamespace(client=client), client_hello=SimpleNamespace(sni=sni)
        )
    )
    assert (client.id not in addon._invalid_tls) is allowed
    if allowed:
        assert list(addon._tls_contexts) == [admitted]


@pytest.mark.parametrize(
    "authority,allowed",
    [
        ("93.184.216.34", True),
        ("93.184.216.34:443", True),
        ("93.184.216.35", False),
        ("allowed.example", False),
        ("93.184.216.34:8443", False),
    ],
)
@pytest.mark.parametrize("version", ["HTTP/1.1", "HTTP/2.0"])
def test_ip_authority_does_not_grant_dns_or_other_ip_access(authority, allowed, version):
    flow = tflow.tflow()
    flow.request = http.Request.make("GET", "https://93.184.216.34/ok")
    flow.request.http_version = version
    flow.request.host_header = authority
    admission = Admission("vm1", "worker1", "flow", "rule", "93.184.216.34", 443, 1, 2)
    assert GatewayAddon._request_matches(flow, admission) is allowed


@pytest.mark.parametrize("version", ["http/1.1", "h2"])
@pytest.mark.parametrize(
    "certificate", ["valid", "wrong-ip", "dns-san", "cn-only", "expired", "untrusted"]
)
def test_real_ip_tls_verifies_sans_chain_authority_and_durable_gate(tmp_path, version, certificate):
    # Loopback exception is fixture-only; real-VM acceptance exercises the production address check.
    host = "127.0.0.1"
    fixture = test_mediator.ProtocolFixture(
        tmp_path,
        certificate_host="127.0.0.2" if certificate == "wrong-ip" else host,
        dns_san=certificate == "dns-san",
        expired=certificate == "expired",
        no_san=certificate == "cn-only",
    )
    gate = threading.Event()
    if certificate != "valid":
        gate.set()
    policy = {
        "version": 2,
        "rules": [
            {
                "id": "rule1",
                "host": host,
                "ports": [fixture.port],
                "methods": ["GET", "POST"],
                "paths": [],
                "hooks": [],
            }
        ],
        "credentials": [],
    }
    upstream_ca = fixture.root_ca
    if certificate == "untrusted":
        unrelated = tmp_path / "unrelated-ca"
        unrelated.mkdir()
        upstream_ca, _, _ = test_mediator._certificate_files(unrelated)
    try:
        mediator = test_mediator.MediatorProcess(
            tmp_path, upstream_ca, policy=policy, admission_gate=gate
        )
    except BaseException:
        fixture.close()
        raise
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    user, password = test_mediator.credential(host=host, port=fixture.port)
    context = ssl.create_default_context(cafile=str(mediator.ca))
    context.set_alpn_protocols([version])
    tls = None
    try:
        tls = context.wrap_socket(
            client._socks(host=host, username=user, password=password), server_hostname=host
        )
        assert ("IP Address", host) in tls.getpeercert()["subjectAltName"]

        def request():
            if version == "http/1.1":
                return client._request(
                    tls,
                    (
                        f"GET /ip-test HTTP/1.1\r\nHost: {host}:{fixture.port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode(),
                )[0]
            protocol = H2Connection(
                config=H2Configuration(client_side=True, header_encoding="utf-8")
            )
            protocol.initiate_connection()
            protocol.send_headers(
                1,
                [
                    (":method", "GET"),
                    (":scheme", "https"),
                    (":authority", f"{host}:{fixture.port}"),
                    (":path", "/ip-test"),
                ],
                end_stream=True,
            )
            tls.sendall(protocol.data_to_send())
            status = 0
            while data := tls.recv(65536):
                for event in protocol.receive_data(data):
                    if isinstance(event, ResponseReceived):
                        status = int(dict(event.headers)[":status"])
                    elif isinstance(event, DataReceived):
                        protocol.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, StreamEnded):
                        return status
                if outgoing := protocol.data_to_send():
                    tls.sendall(outgoing)
            return status

        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request)
            try:
                if certificate == "valid":
                    assert mediator.admission_waiting.wait(3)
                    assert not fixture.http_started.wait(0.1)
                    assert not response.done()
            finally:
                gate.set()
            status = response.result(timeout=8)
        assert (status == 200) is (certificate == "valid")
        assert fixture.count("/ip-test") == (1 if certificate == "valid" else 0)
        if certificate == "valid":
            assert fixture.snis and set(fixture.snis) == {None}
            assert fixture.authorities == [("/ip-test", f"{host}:{fixture.port}")]
            if version == "http/1.1":
                tls.close()
                user, password = test_mediator.credential(host=host, port=fixture.port)
                tls = context.wrap_socket(
                    client._socks(host=host, username=user, password=password), server_hostname=host
                )
                authority = f"{host}:{fixture.port}"
                chunk = b"x" * 65536
                status, _, body = client._request(
                    tls,
                    (
                        f"POST /stream HTTP/1.1\r\nHost: {authority}\r\n"
                        "Transfer-Encoding: chunked\r\n\r\n"
                    ).encode()
                    + b"10000\r\n"
                    + chunk
                    + b"\r\n10000\r\n"
                    + chunk
                    + b"\r\n0\r\n\r\n",
                )
                assert status == 200 and body.startswith(b"stream:131072:")
                for path, expected in (("/redirect-ok", 302), ("/redirect-bad", 502)):
                    assert (
                        client._request(
                            tls, f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()
                        )[0]
                        == expected
                    )
                assert (
                    client._request(
                        tls, b"GET /ip-reuse-denied HTTP/1.1\r\nHost: other.example\r\n\r\n"
                    )[0]
                    == 403
                )
                assert fixture.count("/ip-reuse-denied") == 0
                tls.close()
                user, password = test_mediator.credential(host=host, port=fixture.port)
                tls = context.wrap_socket(
                    client._socks(host=host, username=user, password=password), server_hostname=host
                )
                tls.sendall(
                    (
                        f"GET /websocket HTTP/1.1\r\nHost: {authority}\r\n"
                        "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                    ).encode()
                )
                reader = test_mediator.SocketReader(tls)
                assert b" 101 " in reader.until(b"\r\n\r\n")
                payload, mask = b"ip-ws", b"mask"
                tls.sendall(
                    bytes([0x81, 0x80 | len(payload)])
                    + mask
                    + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                )
                assert reader.exact(2) == bytes([0x81, len(payload)])
                assert reader.exact(len(payload)) == payload
    finally:
        gate.set()
        if tls is not None:
            tls.close()
        mediator.close()
        fixture.close()
