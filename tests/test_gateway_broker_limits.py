import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from sbx import gateway, gateway_supervisor
from test_gateway_credentials import credential_config


def make_broker(tmp_path, monkeypatch, limit=2):
    monkeypatch.setattr(gateway_supervisor, "BROKER_CONCURRENCY", limit)
    return gateway_supervisor.CredentialBroker(
        credential_config("command").credentials,
        {"baseline": {"HOME": str(tmp_path), "PATH": os.defpath, "TZ": "UTC"}, "inherited": {}},
        tmp_path,
    )


def request(sock, incarnation, request_id):
    gateway_supervisor.send_frame(
        sock,
        {
            "version": 1,
            "incarnation": incarnation,
            "request_id": request_id,
            "credential": "packages",
        },
    )


def test_pending_budget_spans_connections_and_overload_is_not_fatal(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, monkeypatch)
    release = threading.Event()
    entered = threading.Condition()
    running = []

    def blocked_fetch(_credential, **_kwargs):
        with entered:
            running.append(threading.current_thread())
            entered.notify_all()
        assert release.wait(3)
        return "token"

    monkeypatch.setattr(broker, "fetch", blocked_fetch)
    servers, clients, threads = [], [], []
    try:
        for index in range(3):
            server, client = socket.socketpair()
            servers.append(server)
            clients.append(client)
            thread = threading.Thread(target=broker.serve, args=(server, str(index)))
            thread.start()
            threads.append(thread)
            if index == 0:
                request(client, "0", "first")
                request(client, "0", "second")
                with entered:
                    assert entered.wait_for(lambda: len(running) == 2, timeout=2)
            else:
                request(client, str(index), "overload")
                response = gateway_supervisor.recv_frame(client, 2)
                assert response == {
                    "version": 1,
                    "incarnation": str(index),
                    "request_id": "overload",
                    "error": "credential unavailable",
                }
                assert len(running) == 2
            if index < 2:
                client.close()
                thread.join(2)
                assert not thread.is_alive()
        release.set()
        for thread in running:
            thread.join(2)
            assert not thread.is_alive()
        request(clients[-1], "2", "healthy")
        assert gateway_supervisor.recv_frame(clients[-1], 2)["value"] == "token"
    finally:
        release.set()
        for client in clients:
            client.close()
        for thread in threads + running:
            thread.join(2)
        for server in servers:
            server.close()


@pytest.mark.parametrize("capacity", ["credential", "provider"])
def test_disconnected_waiters_cancel_without_launching_provider(tmp_path, monkeypatch, capacity):
    broker = make_broker(tmp_path, monkeypatch)
    resource = broker.locks["packages"] if capacity == "credential" else broker.slots
    count = 1 if capacity == "credential" else gateway_supervisor.PROVIDER_CONCURRENCY
    for _ in range(count):
        assert resource.acquire(blocking=False)
    cancelled = threading.Event()
    waiting = threading.Event()
    acquire = broker._acquire

    def observed_acquire(candidate, signal):
        if candidate is resource:
            waiting.set()
        acquire(candidate, signal)

    monkeypatch.setattr(broker, "_acquire", observed_acquire)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(broker.fetch, "packages", cancelled=cancelled)
            assert waiting.wait(1)
            cancelled.set()
            with pytest.raises(gateway.GatewayLifecycleError) as failed:
                pending.result(timeout=1)
            cause = failed.value.__cause__ if capacity == "provider" else failed.value
            assert isinstance(cause, gateway.GatewayLifecycleError)
            assert str(cause) == "credential lookup cancelled"
    finally:
        for _ in range(count):
            resource.release()
    assert broker.locks["packages"].acquire(blocking=False)
    broker.locks["packages"].release()


def test_channel_disconnect_cancels_queued_fetch(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, monkeypatch)
    waiting = threading.Event()
    acquire = broker._acquire

    def observed_acquire(resource, cancelled):
        if resource is broker.locks["packages"]:
            waiting.set()
        acquire(resource, cancelled)

    monkeypatch.setattr(broker, "_acquire", observed_acquire)
    server, client = socket.socketpair()
    thread = threading.Thread(target=broker.serve, args=(server, "worker"))
    with broker.locks["packages"]:
        thread.start()
        try:
            request(client, "worker", "queued")
            assert waiting.wait(1)
            client.close()
            thread.join(2)
            assert not thread.is_alive()
            for _ in range(2):
                assert broker.pending.acquire(timeout=1)
            for _ in range(2):
                broker.pending.release()
        finally:
            client.close()
            thread.join(2)
            server.close()


@pytest.mark.parametrize("failure", ["provider", "send", "start"])
def test_dispatch_failures_release_global_reservations(tmp_path, monkeypatch, failure):
    broker = make_broker(tmp_path, monkeypatch)
    server, client = socket.socketpair()

    def fetch(_credential, **_kwargs):
        if failure == "provider":
            raise gateway.GatewayLifecycleError("simulated provider failure")
        return "token"

    monkeypatch.setattr(broker, "fetch", fetch)
    send = gateway_supervisor.send_frame
    start = threading.Thread.start

    def failing_send(sock, value):
        if sock is server and failure == "send":
            raise BrokenPipeError
        send(sock, value)

    def failing_start(thread):
        if thread._target.__name__ == "answer" and failure == "start":
            raise RuntimeError("cannot start handler")
        start(thread)

    monkeypatch.setattr(gateway_supervisor, "send_frame", failing_send)
    monkeypatch.setattr(threading.Thread, "start", failing_start)
    thread = threading.Thread(target=broker.serve, args=(server, "worker"))
    thread.start()
    try:
        request(client, "worker", "failure")
        if failure == "provider":
            assert gateway_supervisor.recv_frame(client, 2)["error"] == "credential unavailable"
        else:
            with pytest.raises(gateway.GatewayLifecycleError, match="closed"):
                gateway_supervisor.recv_frame(client, 2)
        for _ in range(2):
            assert broker.pending.acquire(timeout=1)
        for _ in range(2):
            broker.pending.release()
    finally:
        client.close()
        thread.join(2)
        server.close()
    assert not thread.is_alive()


def test_slow_credential_does_not_block_another_credential(tmp_path, monkeypatch):
    credential = credential_config("command").credentials[0]
    other = replace(
        credential,
        id="other",
        environment="OTHER_TOKEN",
        bindings=(gateway.CredentialBinding(("other-policy",), "Authorization: {placeholder}"),),
    )
    broker = gateway_supervisor.CredentialBroker((credential, other), {}, tmp_path)
    started, release = threading.Event(), threading.Event()

    def invoke(selected, _operation, **_kwargs):
        if selected.id == "packages":
            started.set()
            assert release.wait(3)
        return {"value": selected.id, "expires_at": None}

    monkeypatch.setattr(broker, "_invoke", invoke)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(broker.fetch, "packages")
        try:
            assert started.wait(1)
            assert pool.submit(broker.fetch, "other").result(timeout=1) == "other"
            assert not slow.done()
        finally:
            release.set()
        assert slow.result(timeout=1) == "packages"


def test_waits_have_a_deadline(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, monkeypatch)
    monkeypatch.setattr(gateway_supervisor, "PROVIDER_TIMEOUT_SECONDS", 0.05)
    with broker.locks["packages"], pytest.raises(gateway.GatewayLifecycleError, match="capacity"):
        broker.fetch("packages")
