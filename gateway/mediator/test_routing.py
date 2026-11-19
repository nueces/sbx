from __future__ import annotations

import asyncio
import socket
import ssl
import time
from types import SimpleNamespace

import pytest
import test_mediator
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded
from mitmproxy import connection, http
from mitmproxy.proxy.layers.http import GetHttpConnection
from mitmproxy.test import tflow
from test_mediator import KEY, Admission, CapabilityVerifier, GatewayAddon, RequestPolicy


def setup_flow():
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = tflow.tflow()
    flow.request = http.Request.make("GET", "https://allowed.example/ok")
    admission = Admission("vm1", "worker1", "flow", "rule", "allowed.example", 443, 1, 2)
    addon._admissions[flow.client_conn.id] = admission
    return addon, flow, admission


def resolver(monkeypatch, answers):
    calls = []

    async def resolve(self, host, port, **kwargs):
        calls.append((host, port))
        selected = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(selected, BaseException):
            raise selected
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, port),
            )
            for ip in selected
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    return calls


@pytest.mark.parametrize("version", ["HTTP/1.1", "HTTP/2.0"])
@pytest.mark.parametrize("port", [443, 8443])
def test_routing_preserves_wire_identity_and_pool_key(monkeypatch, version, port):
    calls = resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()
    admission = admission._replace(port=port)
    addon._admissions[flow.client_conn.id] = admission
    flow.request = http.Request.make("GET", f"https://allowed.example:{port}/ok")
    flow.request.http_version = version
    flow.request.host_header = f"allowed.example:{port}"
    original = flow.request.host_header, flow.request.authority
    asyncio.run(addon.requestheaders(flow))
    assert flow.response is None
    assert flow.request.host == "93.184.216.34"
    assert (flow.request.host_header, flow.request.authority) == original
    assert flow.metadata["sbx_request"]["host"] == "allowed.example"
    server = connection.Server(address=(flow.request.host, port))
    server.tls = True
    assert GetHttpConnection(server.address, True, None).connection_spec_matches(server)
    assert addon._authorize(flow, admission) is not None
    assert len(calls) == 1


def test_rotation_changes_actual_pin_and_future_hint_without_relabeling_flow(monkeypatch):
    calls = resolver(monkeypatch, [["93.184.216.34"], ["8.8.8.8"]])
    addon, flow, admission = setup_flow()

    async def check():
        await addon.requestheaders(flow)
        server = connection.Server(address=(flow.request.host, 443))
        server.sni = flow.request.host
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server))
        assert server.error is None
        assert server.address == ("8.8.8.8", 443)
        assert server.sni == "allowed.example"
        server.state = connection.ConnectionState.OPEN
        server.peername = server.address
        server.tls = True
        flow.server_conn = server
        assert flow.request.host == "93.184.216.34"
        assert addon._authorize(flow, admission) is not None
        second = tflow.tflow()
        second.client_conn = flow.client_conn
        second.request = http.Request.make("GET", "https://allowed.example/next")
        await addon.requestheaders(second)
        assert second.request.host == "8.8.8.8"
        assert GetHttpConnection((second.request.host, 443), True, None).connection_spec_matches(
            server
        )
        assert len(calls) == 2

    asyncio.run(check())


@pytest.mark.parametrize(
    "answer", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], [], socket.gaierror(), TimeoutError()]
)
def test_rebinding_does_not_dial_but_existing_socket_stays_authorized(monkeypatch, answer):
    resolver(monkeypatch, [["93.184.216.34"], ["93.184.216.34"], answer])
    addon, flow, admission = setup_flow()

    async def check():
        await addon.requestheaders(flow)
        server = connection.Server(address=(flow.request.host, 443))
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server))
        server.state = connection.ConnectionState.OPEN
        server.peername = server.address
        server.tls = True
        flow.server_conn = server
        other = connection.Server(address=server.address)
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=other))
        assert other.error in {"destination_dns_failed", "destination_ip_denied"}
        assert addon._authorize(flow, admission) is not None

    asyncio.run(check())


@pytest.mark.parametrize(
    "mutation",
    [
        "host",
        "port",
        "scheme",
        "header",
        "authority",
        "missing",
        "forged",
        "client",
        "admission",
        "path",
    ],
)
def test_routed_identity_cannot_be_retargeted(monkeypatch, mutation):
    resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()
    addon._policies["rule"] = RequestPolicy("rule", "allowed.example", (443,), paths=("/ok",))
    asyncio.run(addon.requestheaders(flow))
    if mutation == "host":
        flow.request.host = "8.8.8.8"
    elif mutation == "port":
        flow.request.port = 8443
    elif mutation == "scheme":
        flow.request.scheme = "http"
    elif mutation == "header":
        flow.request.host_header = "other.example"
    elif mutation == "authority":
        flow.request.authority = "other.example"
    elif mutation == "missing":
        del flow.metadata["sbx_route"]
    elif mutation == "forged":
        flow.metadata["sbx_route"] = dict(host="allowed.example")
    elif mutation == "client":
        flow.client_conn = tflow.tclient_conn()
    elif mutation == "admission":
        admission = admission._replace(incarnation="worker2")
    elif mutation == "path":
        flow.request.path = "/outside"
    assert addon._authorize(flow, admission) is None


@pytest.mark.parametrize("action", ["disconnect", "cancel", "exception"])
def test_interrupted_preparation_releases_capacity(monkeypatch, action):
    addon, flow, admission = setup_flow()
    flow.live = True

    async def resolve(*args):
        if action == "disconnect":
            addon.client_disconnected(flow.client_conn)
            return "93.184.216.34", 443
        if action == "cancel":
            raise asyncio.CancelledError
        raise ValueError("invalid resolver address")

    monkeypatch.setattr(addon, "_resolve_destination", resolve)
    if action == "disconnect":
        asyncio.run(addon.requestheaders(flow))
        assert flow.response.status_code == 403
    else:
        with pytest.raises(asyncio.CancelledError if action == "cancel" else ValueError):
            asyncio.run(addon.requestheaders(flow))
    assert not addon._active
    assert not addon._request_routes
    assert not addon._client_routes
    if action != "disconnect":
        assert not flow.live


def test_absent_host_relative_redirect_keeps_logical_base(monkeypatch):
    resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()
    flow.request.headers.pop("Host", None)
    flow.request.authority = ""
    asyncio.run(addon.requestheaders(flow))
    assert flow.response is None
    assert flow.request.host_header is None
    flow.response = http.Response.make(302, b"", {"Location": "/next"})
    assert addon._redirect_matches(flow, admission)
    addon.responseheaders(flow)
    assert flow.response.status_code == 302


@pytest.mark.parametrize("upstream_http2", [True, False], ids=["h2", "h2-to-h1"])
def test_real_multiplexing_preserves_authority_and_sni(tmp_path, upstream_http2):
    origin = test_mediator.ProtocolFixture(tmp_path)
    origin.context.set_alpn_protocols(["h2"] if upstream_http2 else ["http/1.1"])
    proxy = None
    try:
        proxy = test_mediator.MediatorProcess(tmp_path, origin.root_ca)
        client = test_mediator.MediatorIntegrationTests()
        client.fixture, client.mediator = origin, proxy
        context = ssl.create_default_context(cafile=str(proxy.ca))
        context.set_alpn_protocols(["h2"])
        with context.wrap_socket(client._socks(), server_hostname="localhost") as sock:
            sock.settimeout(5)
            h2 = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
            h2.initiate_connection()
            streams = set(range(1, 32, 2))
            for stream in streams:
                h2.send_headers(
                    stream,
                    [
                        (":method", "GET"),
                        (":scheme", "https"),
                        (":authority", f"localhost:{origin.port}"),
                        (":path", f"/mux-{stream}"),
                    ],
                    end_stream=True,
                )
            sock.sendall(h2.data_to_send())
            ended, statuses = set(), {}
            while ended != streams:
                data = sock.recv(65536)
                assert data, "proxy disconnected before all streams completed"
                for event in h2.receive_data(data):
                    if isinstance(event, ResponseReceived):
                        statuses[event.stream_id] = dict(event.headers)[":status"]
                    elif isinstance(event, DataReceived):
                        h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, StreamEnded):
                        ended.add(event.stream_id)
                if pending := h2.data_to_send():
                    sock.sendall(pending)
            assert statuses == dict.fromkeys(streams, "200")
        for stream in streams:
            assert origin.count(f"/mux-{stream}") == 1
            assert (f"/mux-{stream}", f"localhost:{origin.port}") in origin.authorities
        assert origin.snis and set(origin.snis) == {"localhost"}
        if upstream_http2:
            assert origin.connections == 1
    finally:
        try:
            if proxy is not None:
                proxy.close()
        finally:
            origin.close()


def test_concurrent_preparations_keep_separate_bindings(monkeypatch):
    addon, first, admission = setup_flow()
    second = tflow.tflow()
    second.client_conn = first.client_conn
    second.request = http.Request.make("GET", "https://allowed.example/second")

    async def check():
        ready, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def resolve(*args):
            nonlocal calls
            calls += 1
            number = calls
            if calls == 2:
                ready.set()
            await release.wait()
            return ("93.184.216.34" if number == 1 else "8.8.8.8"), 443

        monkeypatch.setattr(addon, "_resolve_destination", resolve)
        tasks = [asyncio.create_task(addon.requestheaders(flow)) for flow in (first, second)]
        try:
            await asyncio.wait_for(ready.wait(), 1)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert {first.request.host, second.request.host} == {"93.184.216.34", "8.8.8.8"}
        for flow in (first, second):
            assert addon._authorize(flow, admission) is not None
        first.metadata["sbx_route"], second.metadata["sbx_route"] = (
            second.metadata["sbx_route"],
            first.metadata["sbx_route"],
        )
        for flow in (first, second):
            assert addon._authorize(flow, admission) is None
        addon.client_disconnected(first.client_conn)
        assert not addon._client_routes and not addon._request_routes and not addon._active

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["client", "incarnation", "address", "peer", "sni", "missing"])
def test_connected_destination_binding_is_checked(monkeypatch, tamper):
    resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()

    async def check():
        await addon.requestheaders(flow)
        server = connection.Server(address=(flow.request.host, 443))
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server))
        binding = server.sbx_pinned_destination
        if tamper == "client":
            server.sbx_pinned_destination = binding._replace(client_id="other")
        elif tamper == "incarnation":
            server.sbx_pinned_destination = binding._replace(
                admission=admission._replace(incarnation="old")
            )
        elif tamper == "address":
            server.address = ("8.8.8.8", 443)
        elif tamper == "missing":
            del server.sbx_pinned_destination
        server.state = connection.ConnectionState.OPEN
        server.peername = ("8.8.8.8", 443) if tamper == "peer" else server.address
        server.tls = True
        if tamper == "sni":
            server.sni = "other.example"
        flow.server_conn = server
        assert addon._authorize(flow, admission) is None

    asyncio.run(check())


@pytest.mark.parametrize("address", ["2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_ipv6_route_and_peer_are_bound(monkeypatch, address):
    resolver(monkeypatch, [[address]])
    addon, flow, admission = setup_flow()

    async def check():
        await addon.requestheaders(flow)
        server = connection.Server(address=(flow.request.host, 443))
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server))
        server.state = connection.ConnectionState.OPEN
        server.peername = server.address
        server.tls = True
        flow.server_conn = server
        assert addon._authorize(flow, admission) is not None

    asyncio.run(check())


@pytest.mark.parametrize(
    "host", ["93.184.216.34", "other.example", "xn--bcher-kva.example", "bücher.example"]
)
def test_original_identity_mismatch_never_reaches_dns(monkeypatch, host):
    addon, flow, admission = setup_flow()
    flow.request.host = host
    flow.request.host_header = "allowed.example"

    async def unexpected(*args):
        pytest.fail("unauthorized original identity reached DNS")

    monkeypatch.setattr(addon, "_resolve_destination", unexpected)
    asyncio.run(addon.requestheaders(flow))
    assert flow.response.status_code == 403
    assert not addon._client_routes and not addon._request_routes


@pytest.mark.parametrize("host_header", ["allowed.example:443", "other.example"])
def test_http2_physical_host_is_preserved_or_denied(monkeypatch, host_header):
    resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()
    flow.request.http_version = "HTTP/2.0"
    flow.request.authority = "allowed.example"
    flow.request.headers["Host"] = host_header
    asyncio.run(addon.requestheaders(flow))
    if host_header == "other.example":
        assert flow.response.status_code == 403
        assert not addon._client_routes
    else:
        assert flow.response is None
        assert flow.request.authority == "allowed.example"
        assert flow.request.headers["Host"] == host_header
        assert addon._authorize(flow, admission) is not None
        flow.request.headers["Host"] = "other.example"
        assert addon._authorize(flow, admission) is None


def test_cached_numeric_hint_does_not_authorize_numeric_guest_request(monkeypatch):
    calls = resolver(monkeypatch, [["93.184.216.34"]])
    addon, first, admission = setup_flow()
    asyncio.run(addon.requestheaders(first))
    second = tflow.tflow()
    second.client_conn = first.client_conn
    second.request = http.Request.make("GET", "https://93.184.216.34/ok")
    second.request.host_header = "allowed.example"
    asyncio.run(addon.requestheaders(second))
    assert second.response.status_code == 403
    assert len(calls) == 1


@pytest.mark.parametrize("replace", [False, True])
def test_disconnect_during_connect_cannot_publish_a_pin(monkeypatch, replace):
    addon, flow, admission = setup_flow()
    server = connection.Server(address=(admission.host, admission.port))

    async def resolve(*args):
        addon.client_disconnected(flow.client_conn)
        if replace:
            addon._admissions[flow.client_conn.id] = admission._replace(incarnation="new")
        return "93.184.216.34", 443

    monkeypatch.setattr(addon, "_resolve_destination", resolve)
    asyncio.run(addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server)))
    assert server.error == "destination_denied"
    assert server.address == (admission.host, admission.port)
    assert not addon._client_routes


def test_current_valid_hint_survives_dns_answer_reordering(monkeypatch):
    resolver(monkeypatch, [["93.184.216.34", "8.8.8.8"], ["8.8.8.8", "93.184.216.34"]])
    addon, flow, admission = setup_flow()

    async def check():
        await addon.requestheaders(flow)
        server = connection.Server(address=(flow.request.host, 443))
        await addon.server_connect(SimpleNamespace(client=flow.client_conn, server=server))
        assert server.error is None
        assert server.address == (flow.request.host, 443) == ("93.184.216.34", 443)

    asyncio.run(check())


def test_options_asterisk_keeps_logical_identity(monkeypatch):
    resolver(monkeypatch, [["93.184.216.34"]])
    addon, flow, admission = setup_flow()
    flow.request.method, flow.request.path = "OPTIONS", "*"
    asyncio.run(addon.requestheaders(flow))
    assert flow.response is None
    assert flow.request.path == "*"
    assert addon._authorize(flow, admission) is not None
    assert flow.metadata["sbx_request"]["request_target"] == "*"


@pytest.mark.parametrize("target", ["::1", "10.0.0.1"], ids=["rotate-to-ipv6", "deny-private"])
def test_live_reconnect_revalidates_dns_and_updates_pool_hint(tmp_path, target):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = test_mediator.ProtocolFixture(first_dir)
    second = proxy = None
    try:
        second = test_mediator.ProtocolFixture(second_dir, address=("::1", first.port))
        second.context = first.context
        proxy = test_mediator.MediatorProcess(tmp_path, first.root_ca, fixture_dns="127.0.0.1")
        client = test_mediator.MediatorIntegrationTests()
        client.fixture, client.mediator = first, proxy
        with client._tls() as sock:
            status, _, _ = client._request(
                sock,
                (f"GET /close-silently HTTP/1.1\r\nHost: localhost:{first.port}\r\n\r\n").encode(),
            )
            assert status == 200
            deadline = time.monotonic() + 5
            while not (proxy.confdir / "fixture-disconnected").exists():
                assert time.monotonic() < deadline, "upstream close not observed by proxy"
                time.sleep(0.01)
            (proxy.confdir / "fixture-dns.txt").write_text(target)
            for index in range(16):
                status, _, _ = client._request(
                    sock,
                    (
                        f"GET /rotated-{index} HTTP/1.1\r\nHost: localhost:{first.port}\r\n\r\n"
                    ).encode(),
                )
                if target == "10.0.0.1":
                    assert status == 502
                    break
                assert status == 200
                assert second.count(f"/rotated-{index}") == 1
                assert (f"/rotated-{index}", f"localhost:{first.port}") in second.authorities
        assert first.count("/close-silently") == 1
        assert first.connections == 1
        assert second.connections == (0 if target == "10.0.0.1" else 1)
        if target == "::1":
            assert second.local_endpoints[0][0] == "::1"
            assert first.snis == ["localhost", "localhost"]
        else:
            assert not second.paths
    finally:
        try:
            if proxy is not None:
                proxy.close()
        finally:
            try:
                if second is not None:
                    second.close()
            finally:
                first.close()
