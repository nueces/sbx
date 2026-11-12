from __future__ import annotations

import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_mediator
from mitmproxy import http, websocket
from mitmproxy.test import tflow
from sbx_gateway_mediator import BrokerClient, CredentialProviderError
from test_mediator import KEY, Admission, CapabilityVerifier, GatewayAddon

from sbx import gateway_supervisor


@pytest.mark.parametrize("failure", ["late", "closed", "malformed"])
def test_broker_failure_stops_mediator_and_replacement_can_fetch(monkeypatch, failure):
    module = sys.modules[test_mediator.GatewayAddon.__module__]
    stopped = threading.Event()
    monkeypatch.setattr(module.ctx, "master", SimpleNamespace(shutdown=stopped.set), raising=False)
    original_get = module.queue.Queue.get

    def fast_timeout(queue, block=True, timeout=None):
        return original_get(queue, block=block, timeout=0.1 if timeout == 10 else timeout)

    monkeypatch.setattr(module.queue.Queue, "get", fast_timeout)
    server, client = socket.socketpair()
    broker = BrokerClient(client.detach(), "worker1")
    delivered = module.queue.Queue(maxsize=1)
    delivered.put({"value": "already delivered"})
    with broker.lock:
        broker.pending["delivered"] = delivered
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(broker.fetch, "packages")
            request = gateway_supervisor.recv_frame(server, 2)
            if failure == "late":
                with pytest.raises(CredentialProviderError):
                    pending.result(timeout=2)
                gateway_supervisor.send_frame(server, {**request, "value": "late"})
            elif failure == "closed":
                server.shutdown(socket.SHUT_RDWR)
            else:
                server.sendall(b"\x00\x00\x00\x01{")
            assert stopped.wait(timeout=2)
            with pytest.raises(CredentialProviderError):
                pending.result(timeout=2)
        with pytest.raises(CredentialProviderError):
            broker.fetch("packages")
        assert not broker.pending
        assert broker.socket.fileno() == -1
    finally:
        server.close()
        broker.socket.close()

    stopped.clear()
    server, client = socket.socketpair()
    replacement = BrokerClient(client.detach(), "worker2")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(replacement.fetch, "packages")
            request = gateway_supervisor.recv_frame(server, 2)
            gateway_supervisor.send_frame(
                server,
                {key: request[key] for key in ("version", "request_id", "incarnation")}
                | {"value": "fresh"},
            )
            assert pending.result(timeout=2) == "fresh"
        assert not stopped.is_set()
    finally:
        server.close()
        assert stopped.wait(timeout=2)


def admitted_flow(addon):
    flow = tflow.tflow(req=http.Request.make("GET", "https://localhost/"))
    flow.live = True
    addon._admissions[flow.client_conn.id] = Admission(
        "vm1", "worker1", "flow", "rule", "localhost", 443, 1, 2
    )
    return flow


def test_websocket_history_is_not_retained_during_sustained_traffic():
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)
    flow.websocket = websocket.WebSocketData()
    for index in range(2048):
        message = websocket.WebSocketMessage(2, index % 2 == 0, bytes([index % 256]) * 4096)
        flow.websocket.messages.append(message)
        addon.websocket_message(flow)
        assert flow.websocket.messages == []
        assert not message.dropped
        assert message.content == bytes([index % 256]) * 4096
        assert flow.live


@pytest.mark.parametrize("stage", ["credentials", "audit"])
def test_request_capacity_is_reserved_before_blocking_work(monkeypatch, stage):
    module = sys.modules[GatewayAddon.__module__]
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    addon._active.update(f"existing-{i}" for i in range(module.MAX_ACTIVE_REQUESTS - 1))
    first, second = admitted_flow(addon), admitted_flow(addon)
    entered = threading.Event()
    second_decided = threading.Event()
    release = threading.Event()
    calls = []

    def blocking(flow):
        calls.append(flow.id)
        entered.set()
        if flow is second:
            second_decided.set()
        assert release.wait(timeout=3)

    if stage == "credentials":

        def credentials(flow, _admission):
            blocking(flow)
            return True

        monkeypatch.setattr(addon, "_substitute_credentials", credentials)
    else:

        def audit(flow, *, admitted=False):
            if admitted:
                blocking(flow)

        monkeypatch.setattr(addon, "_audit", audit)

    def request_second():
        addon.requestheaders(second)
        second_decided.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            pending = pool.submit(addon.requestheaders, first)
            assert entered.wait(timeout=2)
            rejected = pool.submit(request_second)
            assert second_decided.wait(timeout=2)
            assert calls == [first.id]
            assert len(addon._active) == module.MAX_ACTIVE_REQUESTS
            assert second.response.status_code == 503
        finally:
            release.set()
        pending.result(timeout=3)
        rejected.result(timeout=3)
    assert first.response is None
    addon.error(first)
    assert len(addon._active) == module.MAX_ACTIVE_REQUESTS - 1


@pytest.mark.parametrize("stage", ["hook", "credentials", "audit", "stream"])
def test_admission_failure_releases_reserved_capacity(monkeypatch, stage):
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)
    error = ValueError("simulated admission failure")

    def fail(*args, **kwargs):
        assert flow.id in addon._active
        raise error

    method = {
        "hook": "_run_header_hooks",
        "credentials": "_substitute_credentials",
        "audit": "_audit",
        "stream": "_body_stream",
    }[stage]
    monkeypatch.setattr(addon, method, fail)
    with pytest.raises(ValueError) as failed:
        addon.requestheaders(flow)
    assert failed.value is error
    assert flow.id not in addon._active
    assert not flow.live


@pytest.mark.parametrize("stage", ["hook", "credentials"])
def test_admission_denial_releases_reserved_capacity(monkeypatch, stage):
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)

    def deny(*args, **kwargs):
        assert flow.id in addon._active
        addon._deny(flow, 403, b"sbx gateway denied destination")
        return False

    monkeypatch.setattr(
        addon, "_run_header_hooks" if stage == "hook" else "_substitute_credentials", deny
    )
    addon.requestheaders(flow)
    assert flow.response.status_code == 403
    assert not addon._active


def test_terminal_audit_failure_still_releases_capacity_and_buffers(monkeypatch):
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)
    addon.requestheaders(flow)
    addon.retained.reserve(f"{flow.id}:request", 10)
    addon.retained.reserve(f"{flow.id}:response", 10)
    error = OSError("simulated terminal audit failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(addon, "_audit", fail)
    with pytest.raises(OSError) as failed:
        addon.error(flow)
    assert failed.value is error
    assert not addon._active
    assert addon.retained.total == 0


def test_response_denial_releases_capacity_without_waiting_for_completion():
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)
    addon.requestheaders(flow)
    flow.response = http.Response.make(200, b"", [(f"X-{i}".encode(), b"x") for i in range(101)])
    addon.responseheaders(flow)
    assert flow.response.status_code == 502
    assert not addon._active


@pytest.mark.parametrize("denial", ["oversized", "destination", "missing"])
def test_websocket_denial_discards_history_and_kills_flow(monkeypatch, denial):
    monkeypatch.setattr(sys.modules[GatewayAddon.__module__], "MAX_WEBSOCKET_MESSAGE", 64)
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    flow = admitted_flow(addon)
    flow.websocket = websocket.WebSocketData()
    message = websocket.WebSocketMessage(2, True, b"x" * (65 if denial == "oversized" else 1))
    if denial != "missing":
        flow.websocket.messages.append(message)
    if denial == "destination":
        flow.request.host = "other.example"
    addon.websocket_message(flow)
    assert not flow.live
    assert flow.websocket.messages == []
    if denial != "missing":
        assert message.dropped
