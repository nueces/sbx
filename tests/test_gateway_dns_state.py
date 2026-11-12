from __future__ import annotations

import errno
import json
import os

import pytest

from sbx import gateway


@pytest.fixture
def dns_controller(tmp_path):
    config = gateway.parse_config({"network": {}})
    assert config is not None
    controller = gateway.GatewayController(config, "vm", root=tmp_path / "state")
    controller.directory = gateway.create_state(
        config, "vm", controller.generation, root=controller.root
    )
    yield controller
    controller.close()


def test_dns_journal_header_and_reopen_preserve_mapping(dns_controller):
    controller = dns_controller
    policy = {"digest": "a" * 64}
    fd = controller._open_dns_state(policy)
    try:
        header = json.loads(os.read(fd, 4096))
        assert header == {
            "version": 1,
            "vm_id": "vm",
            "generation": controller.generation,
            "digest": policy["digest"],
        }
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        mapping = b'{"host":"wild.example","ip":"198.18.0.1"}\n'
        os.write(fd, mapping)
    finally:
        os.close(fd)
    path = controller.directory / "dns-mappings.jsonl"
    before = path.read_bytes()
    fd = controller._open_dns_state(policy)
    try:
        assert os.read(fd, gateway.MAX_DNS_JOURNAL_BYTES) == before
    finally:
        os.close(fd)
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["permissions", "oversized", "symlink", "empty"])
def test_dns_journal_refuses_unsafe_state(dns_controller, tmp_path, invalid):
    path = dns_controller.directory / "dns-mappings.jsonl"
    if invalid == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(b"must not change")
        path.symlink_to(target)
        with pytest.raises(OSError) as denied:
            dns_controller._open_dns_state({"digest": "a" * 64})
        assert denied.value.errno == errno.ELOOP
        assert target.read_bytes() == b"must not change"
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if invalid == "permissions":
                os.fchmod(fd, 0o644)
            elif invalid == "oversized":
                os.ftruncate(fd, gateway.MAX_DNS_JOURNAL_BYTES + 1)
        finally:
            os.close(fd)
        with pytest.raises(gateway.GatewayStateError):
            dns_controller._open_dns_state({"digest": "a" * 64})


def test_initialized_dns_state_cannot_be_recreated_when_missing(dns_controller):
    fd = dns_controller._open_dns_state({"digest": "a" * 64})
    os.close(fd)
    path = dns_controller.directory / "dns-mappings.jsonl"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        dns_controller._open_dns_state({"digest": "a" * 64})
    assert not path.exists()


def test_incomplete_journal_header_is_not_accepted_and_fd_is_closed(dns_controller, monkeypatch):
    opened = []
    original_open = os.open

    def capture(*args):
        fd = original_open(*args)
        opened.append(fd)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(gateway.os, "open", capture)
        patch.setattr(gateway.os, "write", lambda *_: 0)
        with pytest.raises(gateway.GatewayLifecycleError):
            dns_controller._open_dns_state({"digest": "a" * 64})
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
