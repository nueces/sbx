import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from gateway.scripts import resource_usage as resources


@pytest.fixture
def proc(tmp_path):
    def process(pid, parent=1, children="", descriptors=2):
        p = tmp_path / str(pid)
        (p / "fd").mkdir(parents=True)
        for fd in range(descriptors):
            (p / "fd" / str(fd)).touch()
        (p / "task" / str(pid)).mkdir(parents=True)
        (p / "task" / str(pid) / "children").write_text(children)
        (p / "stat").write_text(f"{pid} (name with ) spaces) S {parent} " + "0 " * 17 + "123 0 0")
        (p / "status").write_text(
            f"Uid:\t1000 1000 1000 1000\nPPid:\t{parent}\nVmRSS:\t64 kB\nThreads:\t2\n"
        )
        (p / "cgroup").write_text("0::/campaign\n")
        return p

    process(111, children="112")
    process(112, parent=111, descriptors=4)
    return tmp_path


def test_complete_counts_and_descendants(proc):
    root = resources.identity(111, proc)
    assert root["start_ticks"] == 123
    result = resources.collect([root], 1000, proc)
    assert result["available"] is True
    assert {s["pid"] for s in result["samples"]} == {111, 112}
    assert sum(s["fds"] for s in result["samples"]) == 6
    assert sum(s["rss_bytes"] for s in result["samples"]) == 128 * 1024


@pytest.mark.parametrize("problem", ["identity", "owner", "cgroup", "parent", "vanished"])
def test_incomplete_or_reused_process_invalidates_sample(proc, problem):
    root = resources.identity(111, proc)
    if problem == "identity":
        (proc / "111/stat").write_text((proc / "111/stat").read_text().replace("123", "124"))
    elif problem == "owner":
        (proc / "112/status").write_text((proc / "112/status").read_text().replace("1000", "0"))
    elif problem == "cgroup":
        (proc / "112/cgroup").write_text("0::/elsewhere\n")
    elif problem == "parent":
        (proc / "112/status").write_text((proc / "112/status").read_text().replace("111", "999"))
    else:
        (proc / "112/status").unlink()
    with pytest.raises(resources.MeasurementUnavailable):
        resources.collect([root], 1000, proc)


def test_permission_failure_never_becomes_zero(proc, monkeypatch):
    root = resources.identity(111, proc)
    original = Path.iterdir

    def iterdir(path):
        if path.name == "fd":
            raise PermissionError("nondumpable")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(resources.MeasurementUnavailable, match="nondumpable"):
        resources.collect([root], 1000, proc)


@pytest.fixture
def observer(monkeypatch):
    root = {"pid": 111, "start_ticks": 123, "uids": [1000] * 4, "cgroup": "0::/campaign\n"}
    monkeypatch.setattr(resources, "identity", lambda pid: root)
    monkeypatch.setenv("SBX_GATEWAY_RESOURCE_OBSERVER", "/root-owned/observer.py")

    class ObserverPath:
        parent = None

        def __init__(self, path):
            self.parent = self

        def stat(self):
            return SimpleNamespace(st_uid=0, st_mode=0o444)

        def is_absolute(self):
            return True

        def __str__(self):
            return "/root-owned/observer.py"

    monkeypatch.setattr(resources, "Path", ObserverPath)
    payload = {
        "available": True,
        "roots": [root],
        "monotonic_ns": 1,
        "samples": [root | {"rss_bytes": 4096, "fds": 10, "threads": 2}],
    }
    response = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(resources.subprocess, "run", lambda *args, **kwargs: response)
    return payload, response


def test_explicit_observer_counts(observer):
    assert resources.process_usage([111]) == (4096, 10, 1, 2)


@pytest.mark.parametrize(
    "problem", ["denied", "invalid-json", "unavailable", "identity", "omitted", "negative"]
)
def test_observer_failure_is_not_a_passing_measurement(observer, problem):
    payload, response = observer
    if problem == "denied":
        response.returncode, response.stderr = 1, "sudo denied"
    elif problem == "invalid-json":
        response.stdout = "invalid"
    elif problem == "unavailable":
        payload["available"] = False
    elif problem == "identity":
        payload["roots"] = []
    elif problem == "omitted":
        payload["samples"] = []
    else:
        payload["samples"][0]["fds"] = -1
    if problem != "invalid-json":
        response.stdout = json.dumps(payload)
    with pytest.raises(resources.MeasurementUnavailable):
        resources.process_usage([111])


def test_observer_timeout(observer, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(resources.subprocess, "run", timeout)
    with pytest.raises(resources.MeasurementUnavailable, match="unavailable"):
        resources.process_usage([111])
