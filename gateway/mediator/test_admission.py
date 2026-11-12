from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_mediator
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import StreamEnded
from mitmproxy import http
from mitmproxy.test import tflow
from test_mediator import KEY, CapabilityVerifier, GatewayAddon, MediatorProcess, ProtocolFixture


def records(mediator):
    return [json.loads(line) for line in (mediator.confdir / "audit.log").read_text().splitlines()]


@pytest.mark.parametrize(
    "method,body", [("GET", b""), ("POST", b"x" * 131072)], ids=["get", "streaming-post"]
)
def test_no_http_bytes_before_durable_admission(tmp_path, method, body):
    fixture = ProtocolFixture(tmp_path)
    gate = threading.Event()
    mediator = MediatorProcess(tmp_path, fixture.root_ca, admission_gate=gate)
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    try:
        with client._tls() as tls, ThreadPoolExecutor(max_workers=1) as executor:
            request = (
                f"{method} /ok HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode() + body
            result = executor.submit(client._request, tls, request)
            try:
                assert mediator.admission_waiting.wait(timeout=3)
                assert not fixture.http_started.wait(timeout=0.1)
                assert [r["event"] for r in records(mediator)] == ["admitted"]
                assert not result.done()
            finally:
                gate.set()
            assert result.result(timeout=5)[0] == 200
        deadline = time.monotonic() + 3
        while len(records(mediator)) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        admission, outcome = records(mediator)
        assert admission["request_id"] == outcome["request_id"]
        assert outcome["event"] == "outcome"
    finally:
        gate.set()
        mediator.close()
        fixture.close()


def test_http2_streams_wait_for_admission(tmp_path):
    fixture = ProtocolFixture(tmp_path)
    gate = threading.Event()
    mediator = MediatorProcess(tmp_path, fixture.root_ca, admission_gate=gate)
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    try:
        context = ssl.create_default_context(cafile=str(mediator.ca))
        context.set_alpn_protocols(["h2"])
        with context.wrap_socket(client._socks(), server_hostname="localhost") as tls:
            h2 = H2Connection(config=H2Configuration(client_side=True))
            h2.initiate_connection()
            for stream_id in (1, 3):
                h2.send_headers(
                    stream_id,
                    [
                        (":method", "GET"),
                        (":scheme", "https"),
                        (":authority", f"localhost:{fixture.port}"),
                        (":path", f"/stream-{stream_id}"),
                    ],
                    end_stream=True,
                )
            tls.sendall(h2.data_to_send())
            assert mediator.admission_waiting.wait(timeout=3)
            assert not fixture.http_started.wait(timeout=0.1)
            gate.set()
            ended = set()
            while ended != {1, 3}:
                data = tls.recv(65536)
                assert data
                for event in h2.receive_data(data):
                    if isinstance(event, StreamEnded):
                        ended.add(event.stream_id)
                if pending := h2.data_to_send():
                    tls.sendall(pending)
        assert fixture.count("/stream-1") == fixture.count("/stream-3") == 1
    finally:
        gate.set()
        mediator.close()
        fixture.close()


@pytest.mark.parametrize("failure", ["sync", "timeout"])
def test_admission_failure_never_reaches_destination(tmp_path, failure):
    fixture = ProtocolFixture(tmp_path)
    gate = threading.Event()
    mediator = MediatorProcess(
        tmp_path, fixture.root_ca, admission_gate=gate if failure == "timeout" else None
    )
    if failure == "sync":

        def fail_sync(_destination):
            raise OSError("simulated disk sync failure")

        mediator.audit_controller._sync_audit = fail_sync
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    try:
        with client._tls() as tls:
            request = (
                f"GET /must-not-execute HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            try:
                client._request(tls, request)
            except (EOFError, OSError):
                pass
            else:
                pytest.fail("request unexpectedly completed")
        assert not fixture.http_started.is_set()
        assert fixture.count("/must-not-execute") == 0
        assert [r["event"] for r in records(mediator)] == ["admitted"]
    finally:
        gate.set()
        mediator.close()
        fixture.close()


@pytest.mark.parametrize("stage", ["before_ack", "after_forward"])
def test_crash_leaves_unknown_admission_without_replay(tmp_path, stage):
    fixture = ProtocolFixture(tmp_path)
    gate = threading.Event()
    response_gate = threading.Event()
    response = fixture._response

    def hold_response(*args, **kwargs):
        assert response_gate.wait(timeout=5)
        response(*args, **kwargs)

    fixture._response = hold_response
    mediator = MediatorProcess(
        tmp_path, fixture.root_ca, admission_gate=gate if stage == "before_ack" else None
    )
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    try:
        with client._tls() as tls:
            tls.sendall(
                (
                    f"GET /crash HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            assert mediator.admission_waiting.wait(timeout=3)
            if stage == "after_forward":
                assert fixture.http_started.wait(timeout=3)
                deadline = time.monotonic() + 3
                while fixture.count("/crash") != 1:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            mediator.process.kill()
            mediator.process.wait(timeout=3)
            gate.set()
        with pytest.raises(RuntimeError, match="mediator exited -9"):
            mediator.close()
        assert [event["event"] for event in records(mediator)] == ["admitted"]
        assert fixture.count("/crash") == (1 if stage == "after_forward" else 0)
    finally:
        gate.set()
        response_gate.set()
        if mediator.process.poll() is None:
            mediator.close()
        fixture.close()


@pytest.mark.parametrize("reply", ["wrong_id", "old_incarnation", "invalid", "oversized", "closed"])
def test_bad_acknowledgement_latches_failure_and_kills_request(monkeypatch, reply):
    audit_read, audit_write = os.pipe()
    ack_read, ack_write = os.pipe()
    addon = GatewayAddon(
        CapabilityVerifier(KEY, "vm1", "worker1"),
        audit_fd=audit_write,
        audit_ack_fd=ack_read,
        generation="generation",
    )
    flow = tflow.tflow()
    monkeypatch.setattr(sys.modules[GatewayAddon.__module__].ctx, "master", None, raising=False)
    errors = []

    def acknowledge():
        try:
            raw = bytearray()
            while b"\n" not in raw:
                raw.extend(os.read(audit_read, 4096))
            event = json.loads(raw)
            ack = {
                key: event[key] for key in ("version", "generation", "incarnation", "request_id")
            }
            if reply == "wrong_id":
                ack["request_id"] = "other"
            elif reply == "old_incarnation":
                ack["incarnation"] = "old"
            payload = (json.dumps(ack) + "\n").encode()
            if reply == "invalid":
                payload = b"invalid\n"
            elif reply == "oversized":
                payload = b"x" * 4097
            if reply != "closed":
                os.write(ack_write, payload)
        except BaseException as exc:
            errors.append(exc)
        finally:
            os.close(ack_write)

    thread = threading.Thread(target=acknowledge)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="audit delivery failed"):
            addon._audit(flow, admitted=True)
        assert addon._audit_failed
        with pytest.raises(RuntimeError):
            addon._audit(tflow.tflow(), admitted=True)
    finally:
        os.close(audit_write)
        os.close(ack_read)
        thread.join(timeout=3)
        os.close(audit_read)
    assert not thread.is_alive()
    assert not errors


def test_repeated_request_hook_does_not_duplicate_admission(monkeypatch):
    audit_read, audit_write = os.pipe()
    ack_read, ack_write = os.pipe()
    addon = GatewayAddon(
        CapabilityVerifier(KEY, "vm1", "worker1"),
        audit_fd=audit_write,
        audit_ack_fd=ack_read,
        generation="generation",
    )
    flow = tflow.tflow(req=http.Request.make("GET", "https://localhost/"))
    addon._admissions[flow.client_conn.id] = test_mediator.Admission(
        "vm1", "worker1", "flow", "rule", "localhost", 443, 1, 2
    )
    monkeypatch.setattr(sys.modules[GatewayAddon.__module__].ctx, "master", None, raising=False)
    admissions = []

    def acknowledge():
        raw = bytearray()
        while b"\n" not in raw:
            raw.extend(os.read(audit_read, 4096))
        event = json.loads(raw)
        admissions.append(event)
        ack = {key: event[key] for key in ("version", "generation", "incarnation", "request_id")}
        os.write(ack_write, (json.dumps(ack) + "\n").encode())
        os.close(ack_write)

    thread = threading.Thread(target=acknowledge)
    thread.start()
    try:
        addon.requestheaders(flow)
        addon.requestheaders(flow)
        assert flow.response is None
        assert flow.metadata["sbx_admitted"]
        thread.join(timeout=3)
        assert not thread.is_alive()
        addon.error(flow)
        outcome = json.loads(os.read(audit_read, 4096))
        assert len(admissions) == 1
        assert outcome["event"] == "outcome"
        assert outcome["request_id"] == admissions[0]["request_id"]
    finally:
        os.close(audit_write)
        os.close(ack_read)
        thread.join(timeout=3)
        os.close(audit_read)


def test_concurrent_requests_keep_distinct_admission_outcome_ids(tmp_path):
    fixture = ProtocolFixture(tmp_path)
    mediator = MediatorProcess(tmp_path, fixture.root_ca)
    try:

        def request(index):
            client = test_mediator.MediatorIntegrationTests()
            client.mediator, client.fixture = mediator, fixture
            with client._tls() as tls:
                return client._request(
                    tls,
                    (
                        f"GET /concurrent-{index} HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode(),
                )[0]

        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(request, range(8))) == [200] * 8
        deadline = time.monotonic() + 3
        while len(records(mediator)) < 16:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        grouped = {}
        for event in records(mediator):
            grouped.setdefault(event["request_id"], []).append(event["event"])
        assert len(grouped) == 8
        assert all(events == ["admitted", "outcome"] for events in grouped.values())
    finally:
        mediator.close()
        fixture.close()
