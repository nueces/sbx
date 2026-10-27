import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sbx import cli, guest_setup, session_state, vm_metadata
from sbx.runtime import ConfigError


@pytest.fixture
def stopping(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    monkeypatch.setattr(session_state, "SBX_STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_state, "SESSIONS_FILE", tmp_path / "sessions" / "sessions.json")
    monkeypatch.setattr(guest_setup, "_STARTED", {})
    state = SimpleNamespace(
        events=[],
        on_ready=lambda: None,
        on_sync=lambda: None,
        on_stop=lambda: None,
        fail_stop=False,
    )

    class Guest:
        def wait_for_ready(self, **kwargs):
            state.events.append("ready")
            state.on_ready()

        def run(self, command, **kwargs):
            assert command == "sync"
            state.events.append("sync")
            state.on_sync()
            return SimpleNamespace(exit_code=0)

    @contextmanager
    def opened(*args, **kwargs):
        yield Guest()

    class Manager:
        data_dir = tmp_path

        def stop(self, name):
            state.events.append("stop")
            state.on_stop()
            if state.fail_stop:
                raise RuntimeError("stop failed")

        def close(self):
            state.events.append("close")

    monkeypatch.setattr(guest_setup, "open_guest", opened)
    monkeypatch.setattr(guest_setup, "SmolVMManager", Manager)
    return state


@pytest.mark.parametrize("stage", ["ready", "sync"])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_new_session_during_flush_cancels_automatic_stop(stopping, stage, fail_flush):
    def join():
        with session_state.session_lock():
            session_state.register_session("vm1", "run")
        if fail_flush:
            raise TimeoutError("synthetic flush timeout")

    setattr(stopping, "on_" + stage, join)
    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)
    assert session_state.active_sessions("vm1")
    assert "stop" not in stopping.events


def test_existing_session_suppresses_automatic_flush_and_stop(stopping):
    session_state.register_session("vm1", "shell")
    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)
    assert stopping.events == []


@pytest.mark.parametrize("fail_flush", [False, True])
@pytest.mark.parametrize("fail_stop", [False, True])
def test_idle_automatic_stop_preserves_stop_result(stopping, fail_flush, fail_stop):
    stopping.fail_stop = fail_stop
    if fail_flush:
        stopping.on_sync = lambda: (_ for _ in ()).throw(TimeoutError("synthetic timeout"))
    assert guest_setup.stop_vm("vm1", only_if_idle=True) == int(fail_stop)
    assert stopping.events.index("sync") < stopping.events.index("stop")
    with session_state.session_lock(blocking=False):
        pass


def test_final_automatic_stop_excludes_session_admission(stopping):
    def stop():
        with (
            pytest.raises(ConfigError, match="session"),
            session_state.session_lock(blocking=False),
        ):
            pytest.fail("admission must not race the committed stop")

    stopping.on_stop = stop
    assert guest_setup.stop_vm("vm1", only_if_idle=True) == 0


@pytest.mark.parametrize("fail_stop", [False, True])
def test_explicit_stop_is_not_suppressed_by_session_or_registry_lock(stopping, fail_stop):
    stopping.fail_stop = fail_stop
    session_state.register_session("vm1", "run")
    with session_state.session_lock():
        assert guest_setup.stop_vm("vm1") == int(fail_stop)
    assert "stop" in stopping.events


def test_session_lock_excludes_another_process_and_releases(stopping):
    driver = """import sys
from pathlib import Path
from sbx import session_state
from sbx.runtime import ConfigError
session_state.SESSIONS_FILE = Path(sys.argv[1])
try:
    with session_state.session_lock(blocking=False):
        pass
except ConfigError:
    raise SystemExit(75)
"""
    command = [sys.executable, "-c", driver, str(session_state.SESSIONS_FILE)]
    with session_state.session_lock():
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode == 75, result.stderr
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert session_state.SESSIONS_FILE.with_suffix(".lock").stat().st_mode & 0o777 == 0o600


def test_attachment_waits_for_committed_stop_and_does_not_launch(stopping, monkeypatch):
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": "/project", "config_path": "/project/sbx.toml"},
        "agent",
        created=True,
    )
    vm_metadata.mark_provisioned("vm1")
    committed, release, admission = threading.Event(), threading.Event(), threading.Event()
    results, errors = {}, []
    stopping.stopped = False
    original_lock = session_state.session_lock

    @contextmanager
    def observed_lock(**kwargs):
        if threading.current_thread().name == "admission":
            admission.set()
        with original_lock(**kwargs):
            yield

    def stop():
        committed.set()
        assert release.wait(5)
        stopping.stopped = True

    stopping.on_stop = stop
    monkeypatch.setattr(session_state, "session_lock", observed_lock)
    monkeypatch.setattr(
        cli, "_get_existing_vm_status", lambda name: "stopped" if stopping.stopped else "running"
    )
    monkeypatch.setattr(
        guest_setup, "attach", lambda *a, **kw: stopping.events.append("attach") or 0
    )

    def run(name, action):
        try:
            results[name] = action()
        except BaseException as error:
            errors.append(error)

    stop_thread = threading.Thread(
        target=run, args=("stop", lambda: guest_setup.stop_vm("vm1", only_if_idle=True))
    )
    attach_thread = threading.Thread(
        name="admission",
        target=run,
        args=(
            "attach",
            lambda: cli._post_start_actions(
                vm_name="vm1",
                started_now=False,
                command="bash",
                attach=True,
                run_user="agent",
                auth_port=False,
                stop_on_exit=False,
                validate_agent=False,
            ),
        ),
    )
    stop_thread.start()
    try:
        assert committed.wait(5)
        attach_thread.start()
        assert admission.wait(5)
        assert "attach" not in stopping.events
    finally:
        release.set()
        stop_thread.join(5)
        if attach_thread.ident is not None:
            attach_thread.join(5)
    assert not errors
    assert not stop_thread.is_alive() and not attach_thread.is_alive()
    assert results == {"stop": 0, "attach": 1}
    assert "attach" not in stopping.events
    assert session_state.active_sessions("vm1") == []


def test_session_lock_rejects_symlink_without_modifying_target(stopping, tmp_path):
    session_state.SESSIONS_FILE.parent.mkdir()
    target = tmp_path / "keep"
    target.write_text("untouched")
    session_state.SESSIONS_FILE.with_suffix(".lock").symlink_to(target)
    with pytest.raises(ConfigError, match="lock file"), session_state.session_lock():
        pytest.fail("symlinked coordination file must be rejected")
    assert target.read_text() == "untouched"
