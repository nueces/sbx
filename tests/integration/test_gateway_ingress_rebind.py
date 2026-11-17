"""TCP ingress must restart after active close without sharing a live listener."""

import socket
from contextlib import suppress

import pytest

from sbx import gateway


def controller(tmp_path, port):
    config = gateway.parse_config(
        {"network": {"ingress": [{"listen": f"127.0.0.1:{port}", "guest_port": 3000}]}},
        platform="linux",
    )
    assert config is not None
    return gateway.GatewayController(config, "rebind", root=tmp_path)


def test_ingress_rebinds_after_server_time_wait_but_not_while_live(tmp_path):
    first = controller(tmp_path, 0)
    second = None
    first._reserve_listeners()
    listener = first.listeners[0]
    listener.settimeout(2)
    port = listener.getsockname()[1]
    try:
        # Address reuse is not permission to take over another live listener.
        with pytest.raises(OSError):
            controller(tmp_path, port)._reserve_listeners()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _ = listener.accept()
            try:
                accepted.settimeout(2)
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert accepted.recv(1) == b""
            finally:
                accepted.close()
        listener.close()
        second = controller(tmp_path, port)
        second._reserve_listeners()
        assert second.listeners[0].getsockname()[1] == port
    finally:
        for instance in (first, second):
            if instance is not None:
                for owned in instance.listeners:
                    with suppress(OSError):
                        owned.close()
