#!/usr/bin/env python3
"""Linux test-only resource sampling; inaccessible measurements never mean zero."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


class MeasurementUnavailable(RuntimeError):
    """The complete, identity-checked process sample could not be obtained."""


def identity(pid: int, proc: Path = Path("/proc")) -> dict[str, object]:
    directory = proc / str(pid)
    fields = dict(line.split(":", 1) for line in (directory / "status").read_text().splitlines())
    return {
        "pid": pid,
        "start_ticks": int((directory / "stat").read_text().rsplit(")", 1)[1].split()[19]),
        "uids": [int(value) for value in fields["Uid"].split()],
        "cgroup": (directory / "cgroup").read_text(),
    }


def process_tree(roots: set[int], proc: Path) -> dict[int, tuple[int, int]]:
    # /proc/PID/task/TID/children is absent when CONFIG_CHECKPOINT_RESTORE is disabled.
    processes = {}
    children = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue  # A process that has already exited holds no live descriptors.
        pid, parent = int(entry.name), int(fields[1])
        processes[pid] = (parent, int(fields[19]))
        children.setdefault(parent, []).append(pid)
    pending, members = list(roots), {}
    while pending:
        pid = pending.pop()
        if pid in members:
            continue
        if len(members) >= 1024:
            raise MeasurementUnavailable("process-tree sample exceeded its bound")
        members[pid] = processes[pid]
        pending.extend(children.get(pid, []))
    return members


def collect(roots: list[dict[str, object]], uid: int, proc: Path = Path("/proc")) -> dict:
    """Read counts only, rejecting ownership, generation and process-tree races."""
    if not roots or len(roots) > 128 or uid <= 0:
        raise MeasurementUnavailable("invalid observer roots or UID")
    samples = []
    try:
        root_ids = {root["pid"] for root in roots}
        if any(not isinstance(pid, int) or pid <= 1 for pid in root_ids):
            raise MeasurementUnavailable("invalid process identity")
        for root in roots:
            if identity(root["pid"], proc) != root:
                raise MeasurementUnavailable("root identity changed before sampling")
        members = process_tree(root_ids, proc)
        groups = {root["cgroup"] for root in roots}
        for pid, (parent, started) in members.items():
            current = identity(pid, proc)
            if current["start_ticks"] != started or current["uids"] != [uid] * 4:
                raise MeasurementUnavailable(f"process {pid} identity/owner changed")
            if current["cgroup"] not in groups:
                raise MeasurementUnavailable(f"process {pid} escaped its parent cgroup")
            directory = proc / str(pid)
            status = dict(
                line.split(":", 1) for line in (directory / "status").read_text().splitlines()
            )
            if int(status["PPid"]) != parent:
                raise MeasurementUnavailable(f"process {pid} parent changed")
            samples.append(
                current
                | {
                    "fds": len(list((directory / "fd").iterdir())),
                    "rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
                    "threads": int(status["Threads"].strip()),
                }
            )
        if process_tree(root_ids, proc) != members:
            raise MeasurementUnavailable("process tree changed during sampling")
        for sample in samples:
            if identity(sample["pid"], proc) != {
                key: sample[key] for key in ("pid", "start_ticks", "uids", "cgroup")
            }:
                raise MeasurementUnavailable("process identity changed during sampling")
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise MeasurementUnavailable(f"incomplete resource sample: {exc}") from exc
    return {
        "available": True,
        "roots": roots,
        "monotonic_ns": time.monotonic_ns(),
        "observer": {"pid": os.getpid(), "cgroup": Path("/proc/self/cgroup").read_text()},
        "samples": samples,
    }


def process_usage(pids: list[int]) -> tuple[int, int, int, int]:
    """Return RSS, FDs, processes and threads, optionally using an explicit observer."""
    try:
        roots = [identity(pid) for pid in pids]
        observer = os.environ.get("SBX_GATEWAY_RESOURCE_OBSERVER")
        if observer:
            path = Path(observer)
            for item in (path, path.parent):
                st = item.stat()
                if not path.is_absolute() or st.st_uid != 0 or st.st_mode & 0o022:
                    raise MeasurementUnavailable(
                        "observer and parent must be absolute, root-owned and not writable"
                    )
            result = subprocess.run(
                ["sudo", "-n", "/usr/bin/python3", "-I", "-S", str(path)],
                input=json.dumps(roots),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode:
                raise MeasurementUnavailable(f"resource observer failed: {result.stderr.strip()}")
            measured = json.loads(result.stdout)
        else:
            measured = collect(roots, os.getuid())
        if measured.get("available") is not True or measured.get("roots") != roots:
            raise MeasurementUnavailable(
                "resource observer returned an incomplete or mismatched sample"
            )
        samples = measured["samples"]
        if not samples or not {root["pid"] for root in roots} <= {s["pid"] for s in samples}:
            raise MeasurementUnavailable("resource observer omitted a root")
        values = [int(s[key]) for s in samples for key in ("rss_bytes", "fds", "threads")]
        if any(value < 0 for value in values):
            raise MeasurementUnavailable("resource observer returned negative counts")
        return (
            sum(s["rss_bytes"] for s in samples),
            sum(s["fds"] for s in samples),
            len(samples),
            sum(s["threads"] for s in samples),
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise MeasurementUnavailable(f"resource measurement unavailable: {exc}") from exc


if __name__ == "__main__":
    try:
        if os.geteuid() != 0:
            raise MeasurementUnavailable("run the staged observer through sudo")
        payload = sys.stdin.read(65537)
        if len(payload) > 65536:
            raise MeasurementUnavailable("observer input exceeds its bound")
        print(json.dumps(collect(json.loads(payload), int(os.environ["SUDO_UID"]))))
    except (MeasurementUnavailable, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
