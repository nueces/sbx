from __future__ import annotations

import json
import os
import select
import time
from pathlib import Path

import pytest

from sbx import gateway


@pytest.fixture
def audit_controller(tmp_path: Path):
    config = gateway.parse_config({"network": {}})
    assert config is not None
    controller = gateway.GatewayController(config, "vm", root=tmp_path)
    controller.directory = gateway.create_state(config, "vm", controller.generation, root=tmp_path)
    controller._audit_stream = controller._open_log("audit.log")
    yield controller
    controller.close()


def test_audit_pipe_preserves_large_complete_records(audit_controller) -> None:
    target = "/pkg/" + "p" * 70000 + "?padding=" + "q" * 70000 + "&token=evidence-at-end"
    record = {"version": 1, "url": "https://example.com" + target, "request_target": target}
    raw = (json.dumps(record) + "\n").encode()
    assert len(raw) > gateway.MAX_LOG_EVENT_BYTES
    read_fd, write_fd = os.pipe()
    audit_controller._route_audit(
        os.fdopen(read_fd, "rb", buffering=0), audit_controller._audit_stream
    )
    try:
        remaining = memoryview(raw)
        while remaining:
            remaining = remaining[os.write(write_fd, remaining) :]
    finally:
        os.close(write_fd)
    for thread in audit_controller._log_threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert audit_controller._audit_failure is None
    logged = (audit_controller.directory / "audit.log").read_text()
    assert json.loads(logged) == record
    assert len(logged.splitlines()) == 1


@pytest.mark.parametrize(
    "failure", ["invalid", "interrupted", "oversized", "storage", "rate", "mediator"]
)
def test_audit_failure_is_visible_and_stops_gateway(
    audit_controller, tmp_path, monkeypatch, failure
):
    raw = b'{"version":1,"url":"https://example.com/full?token=evidence"}\n'
    if failure == "invalid":
        raw = b"not-json\n"
    elif failure == "interrupted":
        raw = raw[:-1]
    elif failure == "oversized":
        monkeypatch.setattr(gateway, "MAX_AUDIT_EVENT_BYTES", len(raw) - 2)
    elif failure == "storage":
        audit_controller._log_bytes = gateway.MAX_LOG_BYTES
    elif failure == "rate":
        audit_controller._log_tokens = -gateway.MAX_LOG_EVENT_BURST
    elif failure == "mediator":
        raw = b'{"version":1,"audit_error":"delivery_failed"}\n'
    source = tmp_path / "audit-input"
    source.write_bytes(raw)
    audit_controller._route_audit(source.open("rb"), audit_controller._audit_stream)
    for thread in audit_controller._log_threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    with pytest.raises(gateway.GatewayLifecycleError):
        audit_controller.check()
    state = gateway.read_state(audit_controller.directory)
    assert state["status"] == "failed"
    assert state["last_failure"]["code"] == "audit_failed"
    assert "controller/audit_failed" in "\n".join(
        gateway.diagnostic_lines("vm", root=audit_controller.root)
    )
    assert (audit_controller.directory / "audit.log").read_bytes() == b""


@pytest.mark.parametrize(
    "failure", ["sync", "write", "closed_ack", "identity", "event_type", "storage", "rate"]
)
def test_admission_failure_never_acknowledges(audit_controller, monkeypatch, failure):
    source_read, source_write = os.pipe()
    ack_read, ack_write = os.pipe()
    event = {
        "version": 2,
        "event": "admitted",
        "request_id": "request-1",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
    }
    if failure == "identity":
        event["incarnation"] = "old-incarnation"
    elif failure == "event_type":
        event["event"] = {}
    elif failure == "storage":
        audit_controller._log_bytes = gateway.MAX_LOG_BYTES
    elif failure == "rate":
        audit_controller._log_tokens = -gateway.MAX_LOG_EVENT_BURST
    elif failure in {"sync", "write"}:

        def fail(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(
            audit_controller, "_sync_audit" if failure == "sync" else "_emit_log", fail
        )
    if failure == "closed_ack":
        os.close(ack_read)
    audit_controller._route_audit(
        os.fdopen(source_read, "rb", buffering=0), audit_controller._audit_stream, ack_write
    )
    os.write(source_write, (json.dumps(event) + "\n").encode())
    os.close(source_write)
    for thread in audit_controller._log_threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    try:
        if failure != "closed_ack":
            assert os.read(ack_read, 4096) == b""
        assert audit_controller._audit_failure is not None
        with pytest.raises(gateway.GatewayLifecycleError):
            audit_controller.check()
    finally:
        if failure != "closed_ack":
            os.close(ack_read)


def test_ack_pipe_creation_failure_closes_unused_audit_reader(audit_controller, monkeypatch):
    pipe = os.pipe
    created = []

    def failing_pipe():
        if len(created) == 2:
            raise OSError("acknowledgement pipe unavailable")
        pair = pipe()
        created.append(pair)
        return pair

    audit_controller._mediator_policy_payload = b"{}"
    monkeypatch.setattr(gateway.os, "pipe", failing_pipe)
    with pytest.raises(OSError, match="pipe unavailable"):
        audit_controller._start_data_plane(time.monotonic() + 1)
    with pytest.raises(OSError):
        os.fstat(created[1][0])
    assert audit_controller._audit_ack_read_fd is None


def test_replacement_preserves_unknown_admission_and_rotates_ack(audit_controller):
    events = []
    for incarnation in ("first-worker", "replacement-worker"):
        audit_controller.worker_incarnation = incarnation
        read_fd, write_fd = os.pipe()
        ack_read, ack_write = os.pipe()
        event = {
            "version": 2,
            "event": "admitted",
            "request_id": incarnation,
            "generation": audit_controller.generation,
            "incarnation": incarnation,
        }
        audit_controller._route_audit(
            os.fdopen(read_fd, "rb", buffering=0), audit_controller._audit_stream, ack_write
        )
        try:
            os.write(write_fd, (json.dumps(event) + "\n").encode())
            assert select.select([ack_read], [], [], 3)[0]
            assert json.loads(os.read(ack_read, 4096))["incarnation"] == incarnation
        finally:
            os.close(write_fd)
            os.close(ack_read)
        for thread in audit_controller._log_threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        events.append(event)
        assert [
            json.loads(line)
            for line in (audit_controller.directory / "audit.log").read_text().splitlines()
        ] == events
    assert audit_controller._audit_failure is None


def test_startup_rollback_refuses_symlinked_audit(tmp_path):
    config = gateway.parse_config({"network": {}})
    directory = gateway.create_state(config, "vm", "generation", root=tmp_path / "state")
    outside = tmp_path / "outside"
    outside.write_bytes(b"evidence")
    (directory / "audit.log").symlink_to(outside)
    with pytest.raises(gateway.GatewayStateError):
        gateway._rollback_state_directory(directory)
    assert directory.exists()
    assert outside.read_bytes() == b"evidence"


def test_darwin_requires_full_sync(audit_controller, monkeypatch):
    stream = audit_controller._audit_stream
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(gateway.sys, "platform", "darwin")
        patch.setattr(gateway.fcntl, "F_FULLFSYNC", 51, raising=False)
        patch.setattr(gateway.fcntl, "fcntl", lambda fd, operation: calls.append((fd, operation)))
        audit_controller._sync_audit(stream)
        assert calls == [(stream.fileno(), 51)]
        assert audit_controller._audit_directory_synced
        patch.delattr(gateway.fcntl, "F_FULLFSYNC")
        with pytest.raises(gateway.GatewayLifecycleError, match="sync unavailable"):
            audit_controller._sync_audit(stream)


def test_admission_is_synced_before_acknowledgement(audit_controller, monkeypatch):
    source_read, source_write = os.pipe()
    ack_read, ack_write = os.pipe()
    event = {
        "version": 2,
        "event": "admitted",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
        "request_id": "request-1",
    }
    synced = []
    original_sync = audit_controller._sync_audit

    def sync(destination):
        assert not select.select([ack_read], [], [], 0)[0]
        assert json.loads((audit_controller.directory / "audit.log").read_text()) == event
        original_sync(destination)
        synced.append(True)

    monkeypatch.setattr(audit_controller, "_sync_audit", sync)
    audit_controller._route_audit(
        os.fdopen(source_read, "rb", buffering=0), audit_controller._audit_stream, ack_write
    )
    try:
        os.write(source_write, (json.dumps(event) + "\n").encode())
        assert select.select([ack_read], [], [], 3)[0]
        ack = json.loads(os.read(ack_read, 4096))
        assert synced == [True]
        assert ack == {
            key: event[key] for key in ("version", "generation", "incarnation", "request_id")
        }
    finally:
        os.close(source_write)
        os.close(ack_read)
    for thread in audit_controller._log_threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert audit_controller._audit_failure is None
