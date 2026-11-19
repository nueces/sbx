"""Credential provider waits must not block the mediator event loop."""

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_mediator
from mitmproxy import http
from mitmproxy.test import tflow


def test_loaded_async_request_hook_stays_on_the_master_event_loop(monkeypatch):
    module = sys.modules[test_mediator.GatewayAddon.__module__]
    read_fd, write_fd = module.os.pipe()
    module.os.write(write_fd, test_mediator.KEY)
    module.os.close(write_fd)
    for name in (
        "SBX_GATEWAY_CONFDIR",
        "SBX_GATEWAY_OPEN_FILES",
        "SBX_GATEWAY_READY_FD",
        "SBX_GATEWAY_POLICY_FD",
        "SBX_GATEWAY_BROKER_FD",
        "SBX_GATEWAY_LIFETIME_FD",
        "SBX_GATEWAY_AUDIT_FD",
        "SBX_GATEWAY_AUDIT_ACK_FD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBX_GATEWAY_KEY_FD", str(read_fd))
    monkeypatch.setenv("SBX_GATEWAY_VM_ID", "vm1")
    monkeypatch.setenv("SBX_GATEWAY_INCARNATION", "worker1")
    addon = module._load()
    flow = tflow.tflow(req=http.Request.make("GET", "https://allowed.example/"))
    addon._admissions[flow.client_conn.id] = test_mediator.Admission(
        "vm1", "worker1", "flow", "rule", "allowed.example", 443, 1, 2
    )

    async def run():
        master_loop = asyncio.get_running_loop()

        async def resolve(_admission):
            assert asyncio.get_running_loop() is master_loop
            return "93.184.216.34", 443

        monkeypatch.setattr(addon, "_resolve_destination", resolve)
        await addon.requestheaders(flow)
        assert flow.response is None

    asyncio.run(run())


def test_slow_provider_keeps_the_event_loop_responsive():
    case = test_mediator.CredentialInjectionTests()
    addon, client = case._addon()
    flow = case._flow(client, test_mediator._basic_authorization("aws", test_mediator.PLACEHOLDER))
    release = threading.Event()
    responsive = []

    def fetch(_credential):
        responsive.append(release.wait(timeout=1))
        return test_mediator.FAKE_TOKEN

    addon._broker = SimpleNamespace(fetch=fetch)

    async def run():
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, release.set)
        admission = addon._admissions[client.id]
        addon._client_routes[client.id] = test_mediator.DestinationBinding(
            client.id, admission, ("93.184.216.34", admission.port)
        )
        await addon.requestheaders(flow)

    try:
        asyncio.run(run())
        assert responsive == [True], "credential fetching blocked the mediator event loop"
        assert flow.response is None
        assert flow.request.headers["Authorization"] == test_mediator._basic_authorization(
            "aws", test_mediator.FAKE_TOKEN
        )
    finally:
        release.set()


def test_cancelled_provider_work_keeps_its_capacity_until_it_finishes(monkeypatch):
    case = test_mediator.CredentialInjectionTests()
    addon, client = case._addon()
    monkeypatch.setattr(sys.modules[type(addon).__module__], "MAX_ACTIVE_REQUESTS", 2)
    release = threading.Event()
    calls = []

    def fetch(_credential):
        calls.append(True)
        assert release.wait(timeout=3)
        return test_mediator.FAKE_TOKEN

    addon._broker = SimpleNamespace(fetch=fetch)

    async def run():
        try:
            for expected in (1, 2):
                task = asyncio.create_task(addon._fetch_credential("credential"))
                async with asyncio.timeout(2):
                    while len(calls) != expected:
                        await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert len(addon._provider_tasks) == 2
            with pytest.raises(test_mediator.CredentialProviderError, match="capacity"):
                await addon._fetch_credential("credential")
            assert len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(*tuple(addon._provider_tasks))
        assert not addon._provider_tasks

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_cancelled_queued_fetch_never_calls_the_provider():
    addon, _client = test_mediator.CredentialInjectionTests()._addon()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(credential):
        calls.append(credential)
        entered.set()
        assert release.wait(timeout=3)
        return test_mediator.FAKE_TOKEN

    addon._broker = SimpleNamespace(fetch=fetch)

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        first = asyncio.create_task(addon._fetch_credential("first"))
        queued = None
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            queued = asyncio.create_task(addon._fetch_credential("cancelled"))
            async with asyncio.timeout(2):
                while len(addon._provider_tasks) != 2:
                    await asyncio.sleep(0.001)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            release.set()
            assert await first == test_mediator.FAKE_TOKEN
            await asyncio.gather(*tuple(addon._provider_tasks), return_exceptions=True)
            assert calls == ["first"] and not addon._provider_tasks
        finally:
            release.set()
            await first
            if queued is not None and not queued.done():
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["disconnect", "retarget", "credential_path", "credential_header", "cancel"]
)
def test_pending_provider_never_injects_after_request_identity_changes(change):
    case = test_mediator.CredentialInjectionTests()
    addon, client = case._addon()
    authorization = test_mediator._basic_authorization("aws", test_mediator.PLACEHOLDER)
    flow = case._flow(client, authorization)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fetch(_credential):
        entered.set()
        assert release.wait(timeout=3)
        finished.set()
        return test_mediator.FAKE_TOKEN

    addon._broker = SimpleNamespace(fetch=fetch)

    async def run():
        task = asyncio.create_task(addon.requestheaders(flow))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            assert flow.id in addon._active
            if change == "disconnect":
                addon.client_disconnected(client)
            elif change == "retarget":
                flow.request.host_header = "denied.example"
            elif change == "credential_path":
                flow.request.path = "/outside-credential-scope/"
            elif change == "credential_header":
                flow.request.headers["Authorization"] = "Bearer changed-while-waiting"
            else:
                task.cancel()
            release.set()
            if change == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not flow.live
            else:
                await task
                assert flow.response.status_code == 403
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    try:
        asyncio.run(run())
        assert finished.wait(timeout=1)
        assert flow.request.headers["Authorization"] == (
            "Bearer changed-while-waiting" if change == "credential_header" else authorization
        )
        assert not addon._active and not addon._request_routes
    finally:
        release.set()
