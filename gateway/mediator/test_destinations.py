from __future__ import annotations

import asyncio
import errno
import socket
from types import SimpleNamespace

import pytest
import test_mediator
from mitmproxy import connection
from mitmproxy.test import tflow
from test_mediator import (
    KEY,
    Admission,
    CapabilityVerifier,
    GatewayAddon,
    MediatorProcess,
    ProtocolFixture,
)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["fe80::1"],
        ["::ffff:127.0.0.1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["100.64.0.1"],
        ["192.0.2.1"],
        ["198.18.0.1"],
        ["93.184.216.34", "127.0.0.1"],
        [],
    ],
)
def test_forbidden_dns_answers_never_release_connection(monkeypatch, addresses):
    async def check():
        async def resolve(*args, **kwargs):
            return [
                (
                    socket.AF_INET6 if ":" in ip else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (ip, 443),
                )
                for ip in addresses
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        addon, data = admitted_connection()
        await addon.server_connect(data)
        assert data.server.error == "destination_ip_denied"
        assert data.server.address == ("allowed.example", 443)

    asyncio.run(check())


def admitted_connection():
    addon = GatewayAddon(CapabilityVerifier(KEY, "vm1", "worker1"))
    client = tflow.tclient_conn()
    addon._admissions[client.id] = Admission(
        "vm1", "worker1", "flow", "rule", "allowed.example", 443, 1, 2
    )
    return addon, SimpleNamespace(
        client=client, server=connection.Server(address=("allowed.example", 443))
    )


def test_public_address_is_pinned_without_second_dns_lookup(monkeypatch):
    async def check():
        calls = []

        async def rebinding_resolver(*args, **kwargs):
            calls.append(args)
            address = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", rebinding_resolver)
        addon, data = admitted_connection()
        await addon.server_connect(data)
        assert data.server.error is None
        assert data.server.address == ("93.184.216.34", 443)
        assert data.server.sni == "allowed.example"

        async def connect(sock, address):
            assert address == ("93.184.216.34", 443)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "test connector")

        monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", connect)
        with pytest.raises(ConnectionRefusedError) as refused:
            await asyncio.open_connection(*data.server.address)
        assert refused.value.errno == errno.ECONNREFUSED
        assert len(calls) == 1

    asyncio.run(check())


@pytest.mark.parametrize("failure", [socket.gaierror, TimeoutError])
def test_resolver_failure_is_a_denied_transport_boundary(monkeypatch, failure):
    async def check():
        async def resolve(*args, **kwargs):
            raise failure()

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        addon, data = admitted_connection()
        await addon.server_connect(data)
        assert data.server.error == "destination_dns_failed"

    asyncio.run(check())


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_ipv4_and_ipv6_are_pinned(monkeypatch, address):
    async def check():
        async def resolve(*args, **kwargs):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        addon, data = admitted_connection()
        await addon.server_connect(data)
        assert data.server.error is None
        assert data.server.address == (address, 443)

    asyncio.run(check())


def test_invalid_resolver_address_fails_loudly_and_stays_denied(monkeypatch):
    async def check():
        async def resolve(*args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("invalid-IP", 443))
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        addon, data = admitted_connection()
        with pytest.raises(ValueError):
            await addon.server_connect(data)
        assert data.server.error == "destination_ip_denied"

    asyncio.run(check())


def test_existing_connection_error_is_not_cleared():
    addon, data = admitted_connection()
    data.server.error = "previous_denial"
    asyncio.run(addon.server_connect(data))
    assert data.server.error == "previous_denial"


def test_production_launcher_blocks_loopback_without_fixture_exception(tmp_path):
    fixture = ProtocolFixture(tmp_path)
    mediator = MediatorProcess(tmp_path, fixture.root_ca, allow_fixture_loopback=False)
    client = test_mediator.MediatorIntegrationTests()
    client.mediator, client.fixture = mediator, fixture
    try:
        with client._tls() as tls:
            status, _, _ = client._request(
                tls,
                (
                    f"GET /must-not-execute HTTP/1.1\r\nHost: localhost:{fixture.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode(),
            )
            assert status == 502
        assert not fixture.http_started.is_set()
    finally:
        mediator.close()
        fixture.close()


def test_wrong_destination_is_denied_before_dns(monkeypatch):
    async def check():
        async def unexpected_resolution(*args, **kwargs):
            pytest.fail("unauthorized destination reached DNS")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", unexpected_resolution)
        addon, data = admitted_connection()
        data.server.address = ("other.example", 443)
        await addon.server_connect(data)
        assert data.server.error == "destination_denied"

    asyncio.run(check())
