import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.scripts import task19_stability as stability


def rows(field="rss_bytes", values=None, *, offset=150):
    return [
        {
            "generation": "g",
            "pids": [1, 2],
            "start_ticks": [10, 20],
            "pair_seconds": offset + i * 2,
            "elapsed_seconds": offset + i * 2,
            "usage": {field: value},
        }
        for i, value in enumerate([10] * 75 if values is None else values)
    ]


@pytest.mark.parametrize("field,allowance", stability.ALLOWANCES.items())
def test_stability_growth_is_bounded_after_warmup(field, allowance):
    samples = rows(field)
    samples[-1]["usage"][field] += allowance
    assert not stability.growth_exceeded(samples, field, allowance)
    samples[-1]["usage"][field] += 1
    assert stability.growth_exceeded(samples, field, allowance)


def test_rss_allowance_is_one_time_not_recurring():
    assert stability.ALLOWANCES["rss_bytes"] == 16 * 1024 * 1024
    samples = rows(values=[i * 1024 * 1024 for i in range(75)])
    assert stability.growth_exceeded(samples, "rss_bytes", stability.ALLOWANCES["rss_bytes"])


@pytest.mark.parametrize("count", [0, 1, 11])
def test_stability_cannot_pass_with_missing_samples(count):
    assert stability.growth_exceeded(rows("fds", [1] * count), "fds", 2)


def test_short_post_warmup_interval_cannot_pass():
    assert stability.growth_exceeded(rows("fds", [1] * 16), "fds", 2)


def test_warmup_is_fixed_not_a_fraction_of_run_duration():
    samples = rows("fds", [500] * 75 + [10] * 75, offset=0)
    assert not stability.growth_exceeded(samples, "fds", 2)
    samples[-1]["usage"]["fds"] = 13
    assert stability.growth_exceeded(samples, "fds", 2)


def test_stability_never_substitutes_zero_for_missing_metrics():
    with pytest.raises(KeyError):
        stability.growth_exceeded(rows("other"), "fds", 2)


def test_replacement_cannot_hide_growth_behind_previous_pairs_larger_baseline():
    old = rows("fds", [100] * 75)
    new = rows("fds", list(range(75)))
    for sample in new:
        sample["pids"] = [3, 4]
    assert stability.growth_exceeded(old + new, "fds", 2)


def test_pid_reuse_cannot_share_a_baseline():
    old = rows("fds", [100] * 75)
    new = rows("fds", list(range(75)))
    for sample in new:
        sample["start_ticks"] = [30, 40]
    assert stability.growth_exceeded(old + new, "fds", 2)


@pytest.mark.parametrize("identity", ["generation", "pids", "start_ticks"])
def test_quiescent_checkpoints_must_belong_to_same_pair(identity):
    baseline = rows()[0] | {"usage": {field: 10 for field in stability.ALLOWANCES}}
    final = baseline | {"usage": dict(baseline["usage"])}
    stability.check_quiescent_growth(baseline, final)
    final["usage"]["fds"] += 3
    with pytest.raises(RuntimeError, match="quiescent growth: fds"):
        stability.check_quiescent_growth(baseline, final)
    final[identity] = "different" if identity == "generation" else [3, 4]
    with pytest.raises(RuntimeError, match="crossed gateway pairs"):
        stability.check_quiescent_growth(baseline, final)


def soak(values):
    samples = rows(values=values, offset=0)
    for sample in samples:
        sample["elapsed_seconds"] = sample["pair_seconds"]
    return samples


def test_flat_tail_passes_but_rising_and_oscillating_tails_fail():
    assert not stability.plateau_exceeded(soak([100] * 1801), "rss_bytes", 2 * 1024 * 1024)
    assert stability.plateau_exceeded(
        soak([i * 4096 for i in range(1801)]), "rss_bytes", 2 * 1024 * 1024
    )
    assert stability.plateau_exceeded(
        soak([i * 4096 + (i % 2) * 8192 for i in range(1801)]),
        "rss_bytes",
        2 * 1024 * 1024,
    )


def test_plateau_requires_complete_windows():
    assert stability.plateau_exceeded(soak([10] * 100), "rss_bytes", 2 * 1024 * 1024)


def test_stalled_work_does_not_manufacture_plateau():
    assert stability.workload_windows_valid([60 * i for i in range(1, 61)], 3600)
    assert not stability.workload_windows_valid([300 * i for i in range(1, 13)], 3600)
    assert not stability.workload_windows_valid([10, 20, 30], 3600)
    assert not stability.workload_windows_valid([1800 + i for i in range(20)], 3600)


def test_only_proven_pool_growth_is_allowed_and_its_ceiling_is_enforced():
    samples = rows("threads", [4] * 74 + [8])
    for sample in samples:
        sample["components"] = {"worker": {"threads": 8}, "mediator": sample["usage"]}
    assert "mediator unexplained thread growth" in stability.thread_violations(samples, {})
    assert not stability.thread_violations(samples, {"mediator": {"ceiling": 19}})
    samples[-1]["components"]["mediator"]["threads"] = 20
    assert "mediator thread ceiling" in stability.thread_violations(
        samples, {"mediator": {"ceiling": 19}}
    )


def test_even_a_proven_pool_must_stop_growing_in_the_tail():
    samples = soak([8] * 1801)
    for sample in samples:
        sample["components"] = {
            "worker": {"threads": 8},
            "mediator": {"threads": 5 if sample["elapsed_seconds"] < 3000 else 6},
        }
    violations = stability.thread_violations(samples, {"mediator": {"ceiling": 19}}, plateau=True)
    assert violations == ["mediator thread tail kept growing"]
    for sample in samples:
        sample["components"]["mediator"]["threads"] = 5
    assert not stability.thread_violations(samples, {"mediator": {"ceiling": 19}}, plateau=True)


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
