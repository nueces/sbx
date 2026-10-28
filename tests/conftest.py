from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sbx import guest_setup, runtime, session_state


@pytest.fixture
def fake_guest_lifecycle(monkeypatch, tmp_path):
    """Keep CLI orchestration tests out of real VM/network/guest state."""
    monkeypatch.setattr(session_state, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(session_state, "SESSIONS_FILE", tmp_path / "state" / "sessions.json")
    config = SimpleNamespace(
        env_vars={}, comm_channel="ssh", workspace_mounts=[], ssh_public_key="ssh-ed25519 cHVibGlj"
    )
    vm = SimpleNamespace(
        info=SimpleNamespace(config=config),
        set_env_vars=lambda *a, **kw: None,
        unset_env_vars=lambda *a, **kw: None,
    )
    guest = SimpleNamespace(vm=vm, mounts=[], phases=[], opened=[], closed=0)

    def phase(name, arguments=None, **kwargs):
        guest.phases.append((name, arguments, kwargs))

    guest.phase = phase

    @contextmanager
    def session(*args, **kwargs):
        guest.opened.append((args, kwargs))
        try:
            yield guest
        finally:
            guest.closed += 1

    monkeypatch.setattr(guest_setup, "bootstrap_session", session)
    monkeypatch.setattr(guest_setup, "wait_for_control", lambda *a, **kw: None)
    monkeypatch.setattr(guest_setup, "start_vm", lambda *a, **kw: None)

    def stop(name, *, only_if_idle=False):
        if only_if_idle:
            with session_state.session_lock():
                if session_state.active_sessions(name):
                    return 0
        return runtime.run_smolvm(["sandbox", "stop", name])

    monkeypatch.setattr(guest_setup, "stop_vm", stop)
    return guest
