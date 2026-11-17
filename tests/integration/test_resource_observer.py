"""Real nondumpability checks for the explicitly staged validation observer."""

import json
import os
import signal
import subprocess
import sys

import pytest
from gateway.scripts.resource_usage import MeasurementUnavailable, identity, process_usage


@pytest.mark.parametrize("descendant", [False, True])
def test_observer_counts_nondumpable_processes_without_weakening_them(descendant):
    assert os.environ.get("SBX_GATEWAY_RESOURCE_OBSERVER"), "stage the root-owned observer first"
    script = """
import ctypes, json, os, signal, sys, time
fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(7)]
assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0
child = os.fork() if sys.argv[1] == 'yes' else 0
if sys.argv[1] == 'yes' and child == 0:
    time.sleep(30)
    os._exit(0)
print(json.dumps({'pid': os.getpid(), 'child': child}), flush=True)
try:
    sys.stdin.read(1)
finally:
    if child:
        os.kill(child, signal.SIGTERM)
        os.waitpid(child, 0)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", script, "yes" if descendant else "no"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_fd = None
    try:
        info = json.loads(process.stdout.readline())
        if info["child"]:
            child_fd = os.pidfd_open(info["child"])
        with pytest.raises(PermissionError):
            os.listdir(f"/proc/{process.pid}/fd")
        _, descriptors, processes, threads = process_usage([process.pid])
        assert descriptors == 10 * (2 if descendant else 1)
        assert processes == threads == (2 if descendant else 1)
        with pytest.raises(PermissionError):
            os.listdir(f"/proc/{process.pid}/fd")
        root = identity(process.pid)
        root["start_ticks"] += 1
        observer = subprocess.run(
            [
                "sudo",
                "-n",
                "/usr/bin/python3",
                "-I",
                "-S",
                os.environ["SBX_GATEWAY_RESOURCE_OBSERVER"],
            ],
            input=json.dumps([root]),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert observer.returncode != 0 and "identity" in observer.stderr
    finally:
        try:
            process.communicate(input="x", timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        if child_fd is not None:
            try:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(child_fd)
    with pytest.raises(MeasurementUnavailable):
        process_usage([process.pid])
