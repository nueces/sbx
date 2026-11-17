import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.scripts import task19_stability as stability


@pytest.mark.parametrize("field,allowance", stability.ALLOWANCES.items())
def test_stability_growth_is_bounded_after_warmup(field, allowance):
    samples = [{"generation": "g", "pids": [1, 2], "usage": {field: 10}} for _ in range(16)]
    samples[-1]["usage"][field] += allowance
    assert not stability.growth_exceeded(samples, field, allowance)
    samples[-1]["usage"][field] += 1
    assert stability.growth_exceeded(samples, field, allowance)


@pytest.mark.parametrize("count", [0, 1, 11])
def test_stability_cannot_pass_with_missing_samples(count):
    assert stability.growth_exceeded(
        [{"generation": "g", "pids": [1, 2], "usage": {"fds": 1}}] * count, "fds", 2
    )


def test_stability_never_substitutes_zero_for_missing_metrics():
    with pytest.raises(KeyError):
        stability.growth_exceeded([{"generation": "g", "pids": [1, 2], "usage": {}}] * 16, "fds", 2)


def test_replacement_cannot_hide_growth_behind_the_previous_pairs_larger_baseline():
    old = [{"generation": "g", "pids": [1, 2], "usage": {"fds": 100}}] * 16
    new = [{"generation": "g", "pids": [3, 4], "usage": {"fds": value}} for value in range(16)]
    assert stability.growth_exceeded(old + new, "fds", 2)


@pytest.mark.parametrize("identity", ["generation", "pids"])
def test_quiescent_checkpoints_must_belong_to_the_same_pair(identity):
    baseline = {
        "generation": "g",
        "pids": [1, 2],
        "usage": {field: 10 for field in stability.ALLOWANCES},
    }
    final = {**baseline, "usage": dict(baseline["usage"])}
    stability.check_quiescent_growth(baseline, final)
    final["usage"]["fds"] += 3
    with pytest.raises(RuntimeError, match="quiescent growth: fds"):
        stability.check_quiescent_growth(baseline, final)
    final[identity] = "different" if identity == "generation" else [3, 4]
    with pytest.raises(RuntimeError, match="crossed gateway pairs"):
        stability.check_quiescent_growth(baseline, final)


def test_guest_command_survives_ssh_newline_normalization():
    command = stability.guest_command('print("first")\nprint("second")')
    assert "\n" not in command
    outer = shlex.split(command)
    assert outer[:5] == ["sudo", "-iu", "agent", "bash", "-lc"]
    inner = shlex.split(outer[5])
    result = subprocess.run(
        [sys.executable, *inner[1:]], capture_output=True, text=True, check=True
    )
    assert result.stdout == "first\nsecond\n"


@pytest.mark.parametrize("mismatch", [None, "status", "generation", "requested_config_digest"])
def test_stability_reads_private_pids_without_inventing_status_fields(monkeypatch, mismatch):
    status = {"status": "ready", "generation": "generation", "config_digest": "requested"}
    stored = {
        "status": "ready",
        "generation": "generation",
        "requested_config_digest": "requested",
        "config_digest": "runtime",
        "pids": {"worker": 11, "mediator": 12},
    }
    if mismatch is not None:
        stored[mismatch] = "changed"
    monkeypatch.setattr(stability.gateway, "control_request", lambda *args, **kwargs: status)

    def read_state(path, *, vm_id):
        assert path == Path("/state/vm/generation") and vm_id == "vm"
        return stored

    monkeypatch.setattr(stability.gateway, "read_state", read_state)
    if mismatch is None:
        assert stability.ready_state("vm", Path("/state"))["pids"]["worker"] == 11
    else:
        with pytest.raises(RuntimeError, match="state changed"):
            stability.ready_state("vm", Path("/state"))


def test_real_guest_payloads_are_valid_python():
    compile(stability.GUEST_SETUP, "guest-setup", "exec")
    compile(stability.GUEST_WORKLOAD, "guest-workload", "exec")
