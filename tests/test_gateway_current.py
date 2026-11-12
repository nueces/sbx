import errno
import json
import os
import socket
import stat
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sbx import gateway, gateway_supervisor


@pytest.fixture
def config():
    return gateway.parse_config({"network": {}}, platform="linux")


@pytest.mark.parametrize("delta", [0, -1], ids=["same-clock", "clock-backwards"])
def test_current_generation_controls_status_drain_and_stop(config, tmp_path, monkeypatch, delta):
    now = [datetime(2026, 11, 11, 12, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(gateway, "datetime", Clock)
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    now[0] += timedelta(seconds=delta)
    operations = []
    with tempfile.TemporaryDirectory(prefix="sbx-current-") as base:
        runtime = Path(base) / "new"
        runtime.mkdir(mode=0o700)
        current = gateway.create_state(
            config, "vm", "new", root=tmp_path, runtime_directory=runtime
        )
        control = runtime / "control.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(control))
            control.chmod(0o600)
            server.listen(4)
            server.settimeout(3)
            gateway.update_state(current, status="ready", sockets={"control": control})

            def serve():
                for _ in range(5):
                    client, _address = server.accept()
                    with client:
                        request = gateway_supervisor.recv_frame(client, 2)
                        assert request["generation"] == "new"
                        operations.append(request["operation"])
                        gateway_supervisor.send_frame(
                            client,
                            {
                                "version": 1,
                                "generation": "new",
                                "status": "ready",
                                "config_digest": config.digest("vm", "new"),
                                "counters": {},
                                "last_failure": None,
                            },
                        )

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                assert gateway._latest_directory("vm", tmp_path) == current
                assert gateway.control_request("vm", "status", root=tmp_path)["generation"] == "new"
                assert "Gateway generation: new" in gateway.diagnostic_lines(
                    "vm", configured=config, root=tmp_path
                )
                assert not gateway.configuration_drift("vm", config, root=tmp_path)
                gateway.drain("vm", root=tmp_path)
                gateway.stop("vm", root=tmp_path)
            finally:
                worker.join(4)
            assert not worker.is_alive()
    assert operations == ["status", "diagnostics", "status", "drain", "shutdown"]
    assert old.is_dir()
    pointer = current.parent / "current.json"
    assert json.loads(pointer.read_text()) == {"version": 1, "vm_id": "vm", "generation": "new"}
    assert stat.S_IMODE(pointer.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "bad",
    [
        {"version": True, "vm_id": "vm", "generation": "old"},
        {"version": 1, "vm_id": "other", "generation": "old"},
        {"version": 1, "vm_id": "vm", "generation": "missing"},
        {"version": 1, "vm_id": "vm", "generation": "../other"},
        {"version": 1, "vm_id": "vm", "generation": "old", "extra": 1},
    ],
)
def test_invalid_reference_never_falls_back(config, tmp_path, bad):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway._atomic_json(old.parent, "current.json", bad)
    with pytest.raises(gateway.GatewayStateError):
        gateway._latest_directory("vm", tmp_path)
    assert old.is_dir()


@pytest.mark.parametrize("kind", ["missing", "json", "permissions", "symlink"])
def test_unsafe_or_pointerless_state_is_not_migrated(config, tmp_path, kind):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    pointer = old.parent / "current.json"
    if kind in {"missing", "symlink"}:
        pointer.unlink()
        if kind == "symlink":
            pointer.symlink_to(old / "state.json")
    elif kind == "json":
        pointer.write_text("not json")
    else:
        pointer.chmod(0o644)
    with pytest.raises(gateway.GatewayStateError):
        gateway.stop("vm", root=tmp_path)
    assert old.is_dir()


def test_null_reference_still_allows_explicit_removal(config, tmp_path):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    gateway._atomic_json(
        old.parent, "current.json", {"version": 1, "vm_id": "vm", "generation": None}
    )
    assert gateway._latest_directory("vm", tmp_path) is None
    gateway.remove("vm", root=tmp_path)
    assert not old.exists()
    assert not (old.parent / "current.json").exists()
    assert (old.parent / "ownership.lock").is_file()


def test_reference_write_failure_aborts_start_before_data_plane(config, tmp_path, monkeypatch):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    (old / "audit.log").write_bytes(b"retained evidence\n")
    atomic = gateway._atomic_json
    started = []

    def fail_reference(directory, name, data):
        if name == "current.json" and data["generation"] == "new":
            raise OSError("reference sync failed")
        atomic(directory, name, data)

    monkeypatch.setattr(gateway, "_atomic_json", fail_reference)
    with tempfile.TemporaryDirectory(prefix="gw-") as runtime:
        controller = gateway.GatewayController(
            config, "vm", root=tmp_path, runtime_root=Path(runtime), generation="new"
        )
        monkeypatch.setattr(controller, "_start_data_plane", lambda *_args: started.append(True))
        with pytest.raises(OSError, match="reference sync failed"):
            controller.prepare()
    assert not started
    assert gateway._latest_directory("vm", tmp_path) == old
    assert (old / "audit.log").read_bytes() == b"retained evidence\n"


def test_reference_sync_failure_keeps_its_target_until_safe_rollback(config, tmp_path, monkeypatch):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    parent_inode = old.parent.stat().st_ino
    sync = os.fsync
    failed = []
    error = OSError(errno.EIO, "simulated directory sync failure")

    def fail_after_publish(fd):
        if not failed and os.fstat(fd).st_ino == parent_inode:
            reference = json.loads((old.parent / "current.json").read_text())
            if reference["generation"] == "new":
                failed.append(True)
                raise error
        sync(fd)

    monkeypatch.setattr(gateway.os, "fsync", fail_after_publish)
    with pytest.raises(gateway.GatewayStateError) as failure:
        gateway.create_state(config, "vm", "new", root=tmp_path)
    assert failure.value.__cause__ is error
    new = old.parent / "new"
    assert new.is_dir()
    assert gateway._latest_directory("vm", tmp_path) == new
    gateway._rollback_state_directory(new)
    assert gateway._latest_directory("vm", tmp_path) is None
    assert not new.exists() and old.is_dir()


def test_reference_clear_failure_preserves_selected_directory(config, tmp_path, monkeypatch):
    directory = gateway.create_state(config, "vm", "new", root=tmp_path)
    (directory / "audit.log").write_bytes(b"evidence\n")
    atomic = gateway._atomic_json

    def fail_clear(parent, name, data):
        if name == "current.json" and data["generation"] is None:
            raise OSError("reference clear failed")
        atomic(parent, name, data)

    monkeypatch.setattr(gateway, "_atomic_json", fail_clear)
    with pytest.raises(OSError, match="reference clear failed"):
        gateway._remove_state_directory(directory)
    assert gateway._latest_directory("vm", tmp_path) == directory
    assert (directory / "audit.log").read_bytes() == b"evidence\n"


def test_unused_rollback_clears_reference_without_reselecting_history(config, tmp_path):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    new = gateway.create_state(config, "vm", "new", root=tmp_path)
    gateway._rollback_state_directory(new)
    assert gateway._latest_directory("vm", tmp_path) is None
    assert not new.exists() and old.is_dir()
    assert json.loads((old.parent / "current.json").read_text())["generation"] is None


def test_null_reference_cannot_authorize_deleting_live_history(config, tmp_path):
    directory = gateway.create_state(config, "vm", "new", root=tmp_path)
    gateway.update_state(directory, status="ready", pids={"worker": os.getpid()})
    gateway._atomic_json(
        directory.parent, "current.json", {"version": 1, "vm_id": "vm", "generation": None}
    )
    with pytest.raises(gateway.GatewayStateError, match="live"):
        gateway.remove("vm", root=tmp_path)
    assert directory.is_dir()


def test_stop_during_startup_does_not_succeed_against_old_stopped_state(config, tmp_path):
    old = gateway.create_state(config, "vm", "old", root=tmp_path)
    gateway.update_state(old, status="stopped")
    fd = gateway._fallback_lock(old)
    try:
        with pytest.raises(gateway.GatewayStateError, match="lock"):
            gateway.stop("vm", root=tmp_path)
    finally:
        os.close(fd)
