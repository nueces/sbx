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
SAMPLE_SECONDS = 60
TCG_INTERVAL_SECONDS = DURATION_SECONDS
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


def run_tcg(root: Path, directory: Path, cycle: int) -> None:
    output = directory / f"tcg-{cycle:04d}.log"
    environment = os.environ.copy()
    environment["SBX_GATEWAY_BIN"] = str(root / "gateway" / "sbx-gateway")
    wrapper = """
import sys
sys.path.insert(0, sys.argv[1])
import test_tcg as test
test.COMMAND_TIMEOUT = 30
try:
    raise SystemExit(test.main())
finally:
    for process in reversed(test.PROCESSES):
        test.terminate(process)
    for server in test.SERVERS:
        server.shutdown()
        server.server_close()
"""
    with output.open("wb") as log:
        result = subprocess.run(
            [sys.executable, "-c", wrapper, str(root / "gateway" / "scripts")],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"hourly TCG gate failed ({result.returncode}): {output}")


def monotonic_growth(samples: list[dict[str, object]], field: str) -> bool:
    values = [int(sample["usage"][field]) for sample in samples]  # type: ignore[index]
    return len(values) >= 8 and all(
        later > earlier for earlier, later in zip(values, values[1:], strict=False)
    )


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
    next_sample = start_monotonic
    next_tcg = start_monotonic
    tm.MediatorIntegrationTests.setUpClass()
    try:
        while True:
            now = time.monotonic()
            elapsed = now - start_monotonic
            if elapsed >= DURATION_SECONDS:
                break
            cycle += 1
            workloads = [
                "test_http1_streaming_redirect_reuse_and_absolute_uri",
                "test_http2_stream_authority_is_revalidated",
                "test_tls_sni_and_websocket_are_revalidated",
                "test_package_auth_workload_and_negative_cases",
            ]
            for workload in workloads:
                run_method(workload)
            semantic_requests()
            tcg_ran = False
            if now >= next_tcg:
                run_tcg(root, directory, cycle)
                next_tcg += TCG_INTERVAL_SECONDS
                tcg_ran = True
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
                "tcg_ran": tcg_ran,
                "workloads": ["llm-stream", "package", "git-smart-http", "http2", "websocket"],
                "mediator_pid": mediator_pid,
                "usage": measured,
            }
            samples.append(sample)
            append_json(samples_path, sample)
            next_sample += SAMPLE_SECONDS
            time.sleep(max(0, next_sample - time.monotonic()))
        elapsed = time.monotonic() - start_monotonic
        if elapsed < DURATION_SECONDS:
            raise RuntimeError(f"elapsed time was short: {elapsed}")
        for field in ("rss_bytes", "fds", "processes", "threads"):
            if monotonic_growth(samples, field):
                raise RuntimeError(f"monotonic {field} growth detected")
        result = identity | {
            "status": "PASS",
            "finish": utc_now(),
            "elapsed_seconds": elapsed,
            "cycles": cycle,
            "samples": len(samples),
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
