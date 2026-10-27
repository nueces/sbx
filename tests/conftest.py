from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sbx import guest_setup, runtime


@pytest.fixture
def fake_guest_lifecycle(monkeypatch):
    """Keep CLI orchestration tests out of real VM/network/guest state."""
    config = SimpleNamespace(env_vars={}, comm_channel="ssh", workspace_mounts=[])
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
    monkeypatch.setattr(
        guest_setup, "stop_vm", lambda name: runtime.run_smolvm(["sandbox", "stop", name])
    )
    return guest
