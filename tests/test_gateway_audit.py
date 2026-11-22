from __future__ import annotations

import json
import os
import select
import threading
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
    "failure", ["invalid", "deep", "interrupted", "oversized", "storage", "rate", "mediator"]
)
def test_audit_failure_is_visible_and_stops_gateway(
    audit_controller, tmp_path, monkeypatch, failure
):
    raw = b'{"version":1,"url":"https://example.com/full?token=evidence"}\n'
    if failure == "invalid":
        raw = b"not-json\n"
    elif failure == "deep":
        raw = b'{"x":' + b"[" * 2000 + b"]" * 2000 + b"}\n"
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
    assert (
        gateway.read_audit_failure(audit_controller.directory)["reason"]
        == {
            "invalid": "framing",
            "deep": "framing",
            "interrupted": "interrupted",
            "oversized": "event_limit",
            "storage": "storage_limit",
            "rate": "rate_limit",
            "mediator": "delivery_failed",
        }[failure]
    )
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


def test_closed_ack_after_slow_sync_retains_causal_stage(audit_controller, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_sync = os.fsync
    audit_fd = audit_controller._audit_stream.fileno()

    def stalled_sync(fd):
        if fd == audit_fd:
            entered.set()
            assert release.wait(3)
        return original_sync(fd)

    monkeypatch.setattr(os, "fsync", stalled_sync)
    source_read, source_write = os.pipe()
    ack_read, ack_write = os.pipe()
    event = {
        "version": 2,
        "event": "admitted",
        "request_id": "slow-sync",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
    }
    audit_controller._route_audit(
        os.fdopen(source_read, "rb", buffering=0), audit_controller._audit_stream, ack_write
    )
    try:
        os.write(source_write, (json.dumps(event) + "\n").encode())
        assert entered.wait(1)
        assert not select.select([ack_read], [], [], 1.01)[0]
        # Reproduce the historical one-second peer close, not the current five-second budget.
        os.close(ack_read)
        ack_read = None
    finally:
        release.set()
        os.close(source_write)
        if ack_read is not None:
            os.close(ack_read)
        for thread in audit_controller._log_threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
    detail = gateway.read_audit_failure(audit_controller.directory)
    assert detail["operation"] == "acknowledgement"
    assert detail["errno"] == 32
    assert detail["stage_us"]["sync"] >= 1_000_000
    with pytest.raises(gateway.GatewayLifecycleError):
        audit_controller.check()
    assert gateway.read_state(audit_controller.directory)["last_failure"]["code"] == "audit_failed"
    assert json.loads((audit_controller.directory / "audit.log").read_text()) == event


@pytest.mark.parametrize("operation", ["append", "sync"])
def test_audit_io_failure_reports_stage_and_errno_without_exception_payload(
    audit_controller, monkeypatch, capsys, operation
):
    event = {
        "version": 2,
        "event": "admitted",
        "request_id": "request-1",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
    }

    def fail(*args, **kwargs):
        raise OSError(5, "private-exception-payload")

    monkeypatch.setattr(
        audit_controller, "_emit_log" if operation == "append" else "_sync_audit", fail
    )
    read_fd, write_fd = os.pipe()
    ack_read, ack_write = os.pipe()
    audit_controller._route_audit(
        os.fdopen(read_fd, "rb", buffering=0), audit_controller._audit_stream, ack_write
    )
    try:
        os.write(write_fd, (json.dumps(event) + "\n").encode())
        os.close(write_fd)
        for thread in audit_controller._log_threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert os.read(ack_read, 4096) == b""
        message = capsys.readouterr().err
        assert f"operation={operation}" in message and "errno=5" in message
        assert "private-exception-payload" not in message
    finally:
        os.close(ack_read)


@pytest.mark.parametrize("operation", ["append", "sync"])
def test_redacted_audit_failure_is_retained_when_stderr_is_discarded(
    audit_controller, monkeypatch, operation
):
    def fail(*args, **kwargs):
        raise OSError(5, "secret-bearing-exception")

    monkeypatch.setattr(
        audit_controller, "_emit_log" if operation == "append" else "_sync_audit", fail
    )
    source_read, source_write = os.pipe()
    ack_read, ack_write = os.pipe()
    event = {
        "version": 2,
        "event": "admitted",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
        "request_id": "opaque-request",
    }
    audit_controller._route_audit(
        os.fdopen(source_read, "rb", buffering=0), audit_controller._audit_stream, ack_write
    )
    try:
        os.write(source_write, (json.dumps(event) + "\n").encode())
        os.close(source_write)
        for thread in audit_controller._log_threads:
            thread.join(3)
            assert not thread.is_alive()
        detail = gateway.read_audit_failure(
            audit_controller.directory, incarnation=audit_controller.worker_incarnation
        )
        assert detail == audit_controller._audit_failure_detail
        assert detail["operation"] == operation and detail["errno"] == 5
        assert detail["reason"] == "io_error"
        assert "secret-bearing-exception" not in json.dumps(detail)
        assert "opaque-request" not in json.dumps(detail)
        assert os.read(ack_read, 4096) == b""
        assert (audit_controller.directory / "audit-failure.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.close(ack_read)


def test_failure_cache_precedes_unavailable_diagnostic_storage(audit_controller, monkeypatch):
    detail = {
        "version": 1,
        "component": "controller",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
        "operation": "append",
        "reason": "io_error",
        "errno": 28,
        "elapsed_us": 10,
        "stage_us": {"append": 10},
        "request_id_hash": None,
    }

    def unavailable(*args, **kwargs):
        assert audit_controller._audit_failure is not None
        assert audit_controller._audit_failure_detail == detail
        raise gateway.GatewayStateError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(gateway, "_atomic_json", unavailable)
        audit_controller._record_audit_failure(
            detail, gateway.GatewayLifecycleError("audit failed")
        )
        assert audit_controller._audit_failure_detail == detail
        different = dict(detail, operation="sync")
        audit_controller._record_audit_failure(
            different, gateway.GatewayLifecycleError("later failure")
        )
        assert audit_controller._audit_failure_detail == detail


@pytest.mark.parametrize(
    "unsafe",
    [
        "oversized",
        "symlink",
        "mode",
        "generation",
        "incarnation",
        "unknown",
        "payload",
        "deep_json",
        "utf16",
    ],
)
def test_audit_failure_sidecar_rejects_unsafe_data(audit_controller, unsafe):
    detail = {
        "version": 1,
        "component": "controller",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
        "operation": "read",
        "reason": "eof",
        "errno": None,
        "elapsed_us": 1,
        "stage_us": {},
        "request_id_hash": None,
    }
    path = audit_controller.directory / "audit-failure.json"
    if unsafe in ("generation", "incarnation"):
        detail[unsafe] = "other"
    elif unsafe == "unknown":
        detail["unknown"] = "value"
    elif unsafe == "payload":
        detail["reason"] = "secret-exception-text"
    if unsafe == "oversized":
        path.write_bytes(b" " * 2049)
    elif unsafe == "deep_json":
        path.write_bytes(b'{"x":' + b"[" * 1010 + b"]" * 1010 + b"}")
    elif unsafe == "utf16":
        path.write_bytes(json.dumps(detail).encode("utf-16"))
    elif unsafe == "symlink":
        path.symlink_to(audit_controller.directory / "audit.log")
    else:
        path.write_text(json.dumps(detail))
    if unsafe != "symlink":
        path.chmod(0o644 if unsafe == "mode" else 0o600)
    with pytest.raises(gateway.GatewayStateError):
        gateway.read_audit_failure(
            audit_controller.directory, incarnation=audit_controller.worker_incarnation
        )


def test_startup_rollback_preserves_failure_detail_without_an_admitted_record(audit_controller):
    detail = {
        "version": 1,
        "component": "controller",
        "generation": audit_controller.generation,
        "incarnation": audit_controller.worker_incarnation,
        "operation": "read",
        "reason": "io_error",
        "errno": 5,
        "elapsed_us": 1,
        "stage_us": {},
        "request_id_hash": None,
    }
    audit_controller._record_audit_failure(detail, gateway.GatewayLifecycleError("audit failed"))
    assert (audit_controller.directory / "audit.log").stat().st_size == 0
    gateway._rollback_state_directory(audit_controller.directory)
    assert audit_controller.directory.exists()
    assert gateway.read_audit_failure(audit_controller.directory) == detail


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
