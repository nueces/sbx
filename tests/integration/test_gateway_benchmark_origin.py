"""The TLS benchmark origin must accept clients independently of idle peers."""

import http.client
import shutil
import socket
import ssl
import threading

from gateway.scripts import task19_gate
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded


def test_idle_tcp_peer_does_not_block_tls_origin(tmp_path, monkeypatch):
    assert shutil.which("openssl"), "openssl is required for the TLS origin regression"
    accepted = threading.Event()
    original = task19_gate.BenchmarkServer.get_request

    def get_request(server):
        result = original(server)
        accepted.set()
        return result

    monkeypatch.setattr(task19_gate.BenchmarkServer, "get_request", get_request)
    monkeypatch.setattr(task19_gate, "SERVERS", [])
    server, ca = task19_gate.make_server(tmp_path)
    idle = socket.create_connection(server.server_address, timeout=2)
    context = ssl.create_default_context(cafile=str(ca))
    context.check_hostname = False
    connection = http.client.HTTPSConnection(
        "127.0.0.1", server.server_port, context=context, timeout=2
    )
    try:
        assert accepted.wait(2), "TLS handshake blocked the accept loop"
        connection.request("GET", "/latency")
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b"xx"
        connection.request("GET", "/latency")
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b"xx"
    finally:
        connection.close()
        idle.close()
        server.shutdown()
        server.server_close()


def test_http2_origin_proves_sustained_overlap(tmp_path, monkeypatch):
    assert shutil.which("openssl"), "openssl is required"
    monkeypatch.setattr(task19_gate, "SERVERS", [])
    monkeypatch.setattr(task19_gate, "CONCURRENT_REQUESTS", 2)
    monkeypatch.setattr(task19_gate, "CONCURRENT_HOLD_SECONDS", 0.03)
    server, ca = task19_gate.make_server(tmp_path)
    context = ssl.create_default_context(cafile=str(ca))
    context.set_alpn_protocols(["h2"])
    try:
        with context.wrap_socket(
            socket.create_connection(server.server_address, timeout=3),
            server_hostname="a.localhost",
        ) as sock:
            assert sock.selected_alpn_protocol() == "h2"
            connection = H2Connection(config=H2Configuration(header_encoding="utf-8"))
            connection.initiate_connection()
            for stream in (1, 3):
                connection.send_headers(
                    stream,
                    [
                        (":method", "GET"),
                        (":scheme", "https"),
                        (":authority", "a.localhost"),
                        (":path", f"/concurrent?run=h2-test&stream={stream}"),
                    ],
                    end_stream=True,
                )
            sock.sendall(connection.data_to_send())
            ended, statuses, bodies = set(), {}, {1: bytearray(), 3: bytearray()}
            while ended != {1, 3}:
                data = sock.recv(65536)
                assert data
                for event in connection.receive_data(data):
                    if isinstance(event, ResponseReceived):
                        statuses[event.stream_id] = dict(event.headers)[":status"]
                    elif isinstance(event, DataReceived):
                        bodies[event.stream_id].extend(event.data)
                        connection.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, StreamEnded):
                        ended.add(event.stream_id)
                if pending := connection.data_to_send():
                    sock.sendall(pending)
            assert statuses == {1: "200", 3: "200"}
            assert all(bytes(body) == b"concurrent" for body in bodies.values())
        row = task19_gate.concurrency_summary(server.concurrency_events)["h2-test"]
        assert row["starts"] == row["ends"] == row["unique_requests"] == 2
        assert row["overlap_seconds"] >= 0.03
        assert (
            len(
                {
                    event["thread"]
                    for event in server.concurrency_events
                    if event["event"] == "start"
                }
            )
            == 1
        )
    finally:
        server.shutdown()
        server.server_close()
