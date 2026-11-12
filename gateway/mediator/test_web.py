from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import test_mediator
from mitmproxy import ctx, options
from mitmproxy.test import tflow
from sbx_gateway_web import MAX_WEB_FLOWS, GatewayWebMaster
from test_mediator import (
    FAKE_TOKEN,
    PLACEHOLDER,
    MediatorProcess,
    ProtocolFixture,
)


def test_web_is_authenticated_read_only_bounded_and_secret_free(tmp_path: Path) -> None:
    fixture = ProtocolFixture(tmp_path)
    policy = {
        "version": 2,
        "rules": [
            {
                "id": "rule1",
                "policy": "packages",
                "host": "localhost",
                "ports": [fixture.port],
                "methods": ["GET"],
                "paths": ["/pypi/repo"],
                "hooks": [],
            }
        ],
        "credentials": [
            {
                "credential": "token",
                "rule_id": "rule1",
                "methods": [],
                "paths": [],
                "header": {
                    "name": "Authorization",
                    "format": "template",
                    "value": "Bearer {placeholder}",
                },
                "placeholder": PLACEHOLDER,
            }
        ],
    }
    mediator = None
    try:
        mediator = MediatorProcess(tmp_path, fixture.root_ca, mitmweb=True, policy=policy)
        auth = json.loads((mediator.confdir / "web-auth.json").read_text())["url"]
        assert f":{mediator.web_port}/?token=" in auth
        assert (mediator.confdir / "web-auth.json").stat().st_mode & 0o777 == 0o600
        base, token = auth.split("?", 1)

        def api(path: str, method: str = "GET") -> bytes:
            with urlopen(Request(base + path + "?" + token, method=method), timeout=5) as response:
                return response.read()

        with pytest.raises(HTTPError) as denied:
            urlopen(base + "flows", timeout=5)
        assert denied.value.code == 403
        for endpoint in ("options", "flows/dump", "commands/replay.client", "clear"):
            with pytest.raises(HTTPError) as denied:
                api(endpoint, "POST" if endpoint != "options" else "PUT")
            assert denied.value.code == 403

        client = test_mediator.MediatorIntegrationTests()
        client.mediator, client.fixture = mediator, fixture
        path = "/pypi/repo/" + "z" * 600 + "/tail-marker"
        target = path + "?padding=" + "q" * 6000 + "&tail=token-at-end"
        with client._tls() as tls:
            status, _, _ = client._request(
                tls,
                (
                    f"GET {target} HTTP/1.1\r\n"
                    f"Host: localhost:{fixture.port}\r\nAuthorization: Bearer {PLACEHOLDER}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode(),
            )
            assert status == 200
        assert fixture.authorizations[-1] == (path, "Bearer " + FAKE_TOKEN)
        deadline = time.monotonic() + 5
        while not (flows := json.loads(api("flows"))):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert len(flows) == 1
        snapshot = api("flows") + api("flows/dump") + api("events")
        assert all(
            value.encode() not in snapshot
            for value in (PLACEHOLDER, FAKE_TOKEN, "Authorization", "Bearer")
        )
        with pytest.raises(HTTPError) as denied:
            api(f"flows/{flows[0]['id']}/replay", "POST")
        assert denied.value.code == 403
        with client._tls() as tls:
            status, _, body = client._request(
                tls,
                (
                    f"GET /outside/ HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode(),
            )
            assert status == 403 and body == b""
        deadline = time.monotonic() + 5
        while (
            len(
                events := [
                    json.loads(line)
                    for line in (mediator.confdir / "audit.log").read_text().splitlines()
                ]
            )
            < 3
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert [event["outcome"] for event in events] == ["admitted", "forwarded", "denied"]
        assert events[0]["request_id"] == events[1]["request_id"]
        assert events[0]["policy"] == "packages"
        assert events[2]["reason"] == "denied_destination"
        assert events[0]["request_target"] == target
        assert events[0]["url"] == f"https://localhost:{fixture.port}{target}"
        assert events[0]["path"] == events[0]["canonical_path"] == path
        assert json.loads(flows[0]["comment"])["request_target"] == target
        assert b"token-at-end" in snapshot
        port = mediator.web_port
        mediator.close()
        captured = mediator.stdout + mediator.stderr
        assert all(
            value not in captured for value in (FAKE_TOKEN, PLACEHOLDER, token.split("=", 1)[1])
        )
        mediator = None
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)
    finally:
        if mediator is not None:
            mediator.close()
        fixture.close()


def test_web_ports_and_authentication_are_per_mediator(tmp_path: Path) -> None:
    fixture = ProtocolFixture(tmp_path)
    first = second = None
    try:
        first = MediatorProcess(tmp_path, fixture.root_ca, mitmweb=True)
        second = MediatorProcess(tmp_path, fixture.root_ca, mitmweb=True)
        assert first.web_port != second.web_port
        urls = [
            json.loads((p.confdir / "web-auth.json").read_text())["url"] for p in (first, second)
        ]
        assert urls[0].split("token=")[1] != urls[1].split("token=")[1]
        wrong = urls[1].split("?", 1)[0] + "?" + urls[0].split("?", 1)[1]
        with pytest.raises(HTTPError) as denied:
            urlopen(wrong, timeout=5)
        assert denied.value.code == 403
        first.close()
        first = None
        with urlopen(urls[1], timeout=5) as response:
            assert response.status == 200
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        fixture.close()


def test_web_snapshot_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctx, "master", None, raising=False)
    monkeypatch.setattr(ctx, "options", None, raising=False)

    async def check() -> None:
        master = GatewayWebMaster(options.Options())
        try:
            assert master.addons.get("view") is None
            assert master.addons.get("eventstore") is None
            for _ in range(MAX_WEB_FLOWS + 2):
                master.gateway_snapshot(
                    {
                        "host": "example.com",
                        "port": 443,
                        "method": "GET",
                        "url": "https://example.com/pkg?literal",
                        "request_target": "/pkg?literal",
                        "path": "/pkg",
                        "status": 200,
                    }
                )
            assert len(master.view) == MAX_WEB_FLOWS
            for flow in master.view:
                assert not flow.request.headers and not flow.response.headers
                assert not flow.request.content and not flow.response.content
                assert flow.request.path == "/pkg?literal"
        finally:
            await master.done()

    asyncio.run(check())


def test_audit_preserves_padded_invalid_request_evidence() -> None:
    read_fd, write_fd = os.pipe()
    captured = bytearray()

    def read_record() -> None:
        while chunk := os.read(read_fd, 65536):
            captured.extend(chunk)
            if captured.endswith(b"\n"):
                return

    reader = threading.Thread(target=read_record)
    addon = test_mediator.GatewayAddon(
        test_mediator.CapabilityVerifier(test_mediator.KEY, "vm", "worker"),
        audit_fd=write_fd,
    )
    target = "/%2e%2e/" + "z" * 70000 + "?padding=" + "q" * 70000 + "&token=tail-evidence"
    flow = tflow.tflow()
    flow.request.host = "h" * 300
    flow.request.path = target
    flow.request.method = "X" * 100
    flow.metadata["sbx_request"] = addon._request_details(flow)
    original_url = flow.request.url
    flow.request.path = "/later-mutation"
    reader.start()
    try:
        addon._deny(flow, 403, b"sbx gateway denied destination")
        addon.response(flow)
        reader.join(timeout=3)
        assert not reader.is_alive()
        event = json.loads(captured)
        assert event["request_target"] == target
        assert event["url"] == original_url
        assert event["host"] == "h" * 300
        assert event["method"] == "X" * 100
        assert event["canonical_path"] == ""
    finally:
        os.close(write_fd)
        reader.join(timeout=3)
        os.close(read_fd)


def test_audit_is_once_complete_and_fails_visibly_on_backpressure() -> None:
    read_fd, write_fd = os.pipe()
    addon = test_mediator.GatewayAddon(
        test_mediator.CapabilityVerifier(test_mediator.KEY, "vm", "worker"),
        audit_fd=write_fd,
        generation="generation",
    )
    try:
        flow = tflow.tflow()
        flow.request.path = "/pkg?secret=guest-supplied-evidence"
        addon._deny(flow, 403, b"sbx gateway denied credential use")
        addon.response(flow)
        addon.error(flow)
        event = json.loads(os.read(read_fd, 4096))
        assert event["path"] == event["canonical_path"] == "/pkg"
        assert event["request_target"] == "/pkg?secret=guest-supplied-evidence"
        assert event["url"].endswith(event["request_target"])
        assert event["reason"] == "denied_credential_use"
        assert flow.response.content == b""
        os.set_blocking(read_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 4096)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        with pytest.raises(RuntimeError, match="audit delivery failed; mediation stopped"):
            addon.response(tflow.tflow())
    finally:
        os.close(read_fd)
        os.close(write_fd)
