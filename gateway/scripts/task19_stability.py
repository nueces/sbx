#!/usr/bin/env python3
"""Bounded ten-minute Task 19 mixed stability smoke test."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

MEDIATOR = Path(__file__).parents[1] / "mediator"
sys.path.insert(0, str(MEDIATOR))

import test_mediator as tm  # noqa: E402

DURATION_SECONDS = 10 * 60
SAMPLE_SECONDS = 10
RSS_CEILING = 512 * 1024 * 1024
FD_CEILING = 4096


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def usage(pids: list[int]) -> dict[str, int]:
    pending, seen = list(pids), set()
    rss = fds = threads = 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = Path("/proc") / str(pid)
        status = process.joinpath("status").read_text().splitlines()
        for line in status:
            if line.startswith("VmRSS:"):
                rss += int(line.split()[1]) * 1024
            elif line.startswith("Threads:"):
                threads += int(line.split()[1])
        with suppress(OSError):
            fds += sum(1 for _ in process.joinpath("fd").iterdir())
        for task in process.joinpath("task").iterdir():
            with suppress(OSError):
                pending.extend(
                    int(value) for value in task.joinpath("children").read_text().split()
                )
    return {"rss_bytes": rss, "fds": fds, "processes": len(seen), "threads": threads}


def append_json(path: Path, payload: object) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def fail(directory: Path, message: str) -> None:
    atomic_json(
        directory / "FAILURE",
        {"status": "FAIL", "timestamp": utc_now(), "pid": os.getpid(), "message": message},
    )


def run_method(name: str) -> None:
    case = tm.MediatorIntegrationTests(name)
    getattr(case, name)()


def semantic_requests() -> None:
    case = tm.MediatorIntegrationTests("test_http1_streaming_redirect_reuse_and_absolute_uri")
    for path in ("/v1/chat/completions", "/repo.git/git-upload-pack?service=git-upload-pack"):
        connection = case._tls()
        status, _, body = case._request(
            connection,
            f"GET {path} HTTP/1.1\r\nHost: localhost:{case.fixture.port}\r\n\r\n".encode(),
        )
        connection.close()
        if status != 200 or body != b"ok:GET":
            raise RuntimeError(f"mixed semantic request failed: {path}: {status}")


def growth_exceeded(samples: list[dict[str, object]], field: str, allowance: int) -> bool:
    """Reject material end-to-end growth after the first-quarter warm-up."""
    values = [int(sample["usage"][field]) for sample in samples]  # type: ignore[index]
    return len(values) < 12 or values[-1] - values[len(values) // 4] > allowance


def main() -> int:
    os.umask(0o077)
    directory = Path(
        os.environ.get(
            "SBX_TASK19_STABILITY", "/home/nueces/code/sbx/.local/task19-stability-smoke"
        )
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for sentinel in ("PASS", "FAILURE"):
        directory.joinpath(sentinel).unlink(missing_ok=True)
    samples_path = directory / "samples.jsonl"
    samples_path.unlink(missing_ok=True)
    root = Path(__file__).parents[2].resolve()
    start_wall = datetime.now(UTC)
    start_monotonic = time.monotonic()
    deadline_wall = start_wall + timedelta(seconds=DURATION_SECONDS)
    identity = {
        "status": "RUNNING",
        "gate": "ten-minute-stability-smoke",
        "pid": os.getpid(),
        "process_start_ticks": Path(f"/proc/{os.getpid()}/stat").read_text().split()[21],
        "start": start_wall.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deadline": deadline_wall.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "required_elapsed_seconds": DURATION_SECONDS,
        "sample_seconds": SAMPLE_SECONDS,
        "root": str(root),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "command": [sys.executable, str(Path(__file__).resolve())],
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "git_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "--binary"], cwd=root)
        ).hexdigest(),
    }
    atomic_json(directory / "run.json", identity)
    directory.joinpath("pid").write_text(f"{os.getpid()}\n", encoding="ascii")

    def interrupted(signum: int, _frame: object) -> None:
        raise RuntimeError(f"stability monitor interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    samples: list[dict[str, object]] = []
    cycle = 0
    workloads = [
        "test_http1_streaming_redirect_reuse_and_absolute_uri",
        "test_http2_stream_authority_is_revalidated",
        "test_tls_sni_and_websocket_are_revalidated",
        "test_request_and_response_header_limits_fail_closed",
    ]
    workload_counts = dict.fromkeys(workloads, 0)
    tm.MediatorIntegrationTests.setUpClass()
    try:
        while time.monotonic() - start_monotonic < DURATION_SECONDS:
            workload = workloads[cycle % len(workloads)]
            run_method(workload)
            workload_counts[workload] += 1
            if cycle % len(workloads) == 0:
                semantic_requests()
            cycle += 1
            mediator_pid = tm.MediatorIntegrationTests.mediator.process.pid
            if tm.MediatorIntegrationTests.mediator.process.poll() is not None:
                raise RuntimeError("persistent mediator exited")
            measured = usage([os.getpid(), mediator_pid])
            if measured["rss_bytes"] > RSS_CEILING:
                raise RuntimeError(f"RSS hard ceiling exceeded: {measured}")
            if measured["fds"] > FD_CEILING:
                raise RuntimeError(f"FD hard ceiling exceeded: {measured}")
            sample = {
                "timestamp": utc_now(),
                "elapsed_seconds": time.monotonic() - start_monotonic,
                "cycle": cycle,
                "workload": workload,
                "mediator_pid": mediator_pid,
                "usage": measured,
            }
            samples.append(sample)
            append_json(samples_path, sample)
            time.sleep(
                min(SAMPLE_SECONDS, max(0, DURATION_SECONDS - time.monotonic() + start_monotonic))
            )
        elapsed = time.monotonic() - start_monotonic
        if elapsed < DURATION_SECONDS or min(workload_counts.values()) < 3:
            raise RuntimeError(
                f"stability workload coverage was short: {elapsed}, {workload_counts}"
            )
        allowances = {"rss_bytes": 4 * 1024 * 1024, "fds": 2, "processes": 0, "threads": 0}
        for field, allowance in allowances.items():
            if growth_exceeded(samples, field, allowance):
                raise RuntimeError(f"material {field} growth detected")
        result = identity | {
            "status": "PASS",
            "finish": utc_now(),
            "elapsed_seconds": elapsed,
            "cycles": cycle,
            "samples": len(samples),
            "workload_counts": workload_counts,
            "maximum": {
                field: max(int(sample["usage"][field]) for sample in samples)  # type: ignore[index]
                for field in ("rss_bytes", "fds", "processes", "threads")
            },
        }
        atomic_json(directory / "PASS", result)
        atomic_json(directory / "run.json", result)
        return 0
    except BaseException:
        message = traceback.format_exc()
        fail(directory, message)
        identity.update({"status": "FAIL", "finish": utc_now(), "error": message})
        atomic_json(directory / "run.json", identity)
        raise
    finally:
        tm.MediatorIntegrationTests.tearDownClass()


if __name__ == "__main__":
    raise SystemExit(main())
