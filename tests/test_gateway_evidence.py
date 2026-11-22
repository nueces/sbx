"""Fast checks for the real-VM fixture's evidence and removal boundary."""

import errno
import importlib.util
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

INTEGRATION = Path(__file__).parent / "integration"
TCG_SPEC = importlib.util.spec_from_file_location(
    "test_gateway_tcg", INTEGRATION / "test_gateway_tcg.py"
)
assert TCG_SPEC is not None and TCG_SPEC.loader is not None
tcg = importlib.util.module_from_spec(TCG_SPEC)
sys.modules[TCG_SPEC.name] = tcg
TCG_SPEC.loader.exec_module(tcg)
DIRECT_SPEC = importlib.util.spec_from_file_location(
    "direct_ip_evidence", INTEGRATION / "test_gateway_direct_ip.py"
)
assert DIRECT_SPEC is not None and DIRECT_SPEC.loader is not None
direct = importlib.util.module_from_spec(DIRECT_SPEC)
DIRECT_SPEC.loader.exec_module(direct)


def spawn(script, *, worker=False):
    return subprocess.Popen(
        [sys.executable, "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if worker else subprocess.PIPE,
    )


@pytest.mark.parametrize("worker", [False, True])
def test_capture_drains_past_pipe_capacity_and_preserves_partial_lines(tmp_path, worker):
    capture = tcg.DiagnosticCapture(tmp_path)
    process = spawn(
        'import os; os.write(1,b\'{"status":"ready"}\\n\'); '
        "os.write(1,b'x'*1048576+b'stdout-tail'); os.write(2,b'stderr-tail')",
        worker=worker,
    )
    try:
        capture.attach(process, "worker" if worker else "mediator", "one")
        if worker:
            assert capture.read_ready(process) == b'{"status":"ready"}\n'
        assert process.wait(timeout=5) == 0
        capture.finish()
        contents = b"".join(path.read_bytes() for path in capture.paths)
        assert contents.count(b"x") == 1048576
        assert all(marker in contents for marker in (b"stdout-tail", b"stderr-tail", b'"ready"'))
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in capture.paths)
    finally:
        process.kill() if process.poll() is None else None
        process.wait(timeout=5)


def test_archive_contains_both_incarnations_and_initial_readiness(tmp_path):
    runtime, destination = tmp_path / "runtime", tmp_path / "archive"
    runtime.mkdir()
    destination.mkdir()
    capture = tcg.DiagnosticCapture(runtime)
    for incarnation in ("one", "two"):
        for role in ("worker", "mediator"):
            marker = f"{role}-{incarnation}"
            process = spawn(
                f'import os; os.write(1,b\'{{"status":"ready"}}\\n{marker}-out\'); '
                f"os.write(2,b'{marker}-err')",
                worker=role == "worker",
            )
            capture.attach(process, role, incarnation)
            if role == "worker":
                assert b'"ready"' in capture.read_ready(process)
            assert process.wait(timeout=5) == 0
            capture.finish(process)
    assert len(capture.paths) == 6
    direct.archive_and_remove(runtime, destination, capture)
    assert not runtime.exists()
    with tarfile.open(destination / "direct-ip-evidence.tar.gz") as bundle:
        contents = b"".join(bundle.extractfile(member).read() for member in bundle)
    for incarnation in ("one", "two"):
        for role in ("worker", "mediator"):
            for stream in ("out", "err"):
                assert f"{role}-{incarnation}-{stream}".encode() in contents
    assert contents.count(b'{"status":"ready"}') == 4


def test_exit_before_readiness_still_preserves_diagnostics(tmp_path):
    runtime, destination = tmp_path / "runtime", tmp_path / "archive"
    runtime.mkdir()
    destination.mkdir()
    capture = tcg.DiagnosticCapture(runtime)
    process = spawn("import os; os.write(2,b'early-startup-failure')", worker=True)
    capture.attach(process, "worker", "failed")
    with pytest.raises(AssertionError, match="readiness|invalid"):
        tcg.worker_readiness(process, capture)
    assert process.wait(timeout=5) == 0
    direct.archive_and_remove(runtime, destination, capture)
    with tarfile.open(destination / "direct-ip-evidence.tar.gz") as bundle:
        assert b"early-startup-failure" in b"".join(bundle.extractfile(m).read() for m in bundle)


class FailedWriter:
    def __init__(self, stream, unexpected=False):
        self.stream = stream
        self.error = (
            RuntimeError("injected unexpected writer failure")
            if unexpected
            else OSError(errno.ENOSPC, "injected diagnostic storage failure")
        )

    def write(self, chunk):
        raise self.error

    def close(self):
        self.stream.close()


@pytest.mark.parametrize(
    "failure", ["write", "unexpected", "budget", "missing", "archive", "corrupt"]
)
def test_incomplete_evidence_blocks_removal(tmp_path, monkeypatch, failure):
    runtime, destination = tmp_path / "runtime", tmp_path / "archive"
    runtime.mkdir()
    destination.mkdir()
    capture = tcg.DiagnosticCapture(runtime)
    if failure in {"write", "unexpected"}:
        original = tcg.os.fdopen
        monkeypatch.setattr(
            tcg.os,
            "fdopen",
            lambda *a, **kw: FailedWriter(original(*a, **kw), failure == "unexpected"),
        )
    if failure == "budget":
        monkeypatch.setattr(tcg.gateway_runtime, "MAX_LOG_TOTAL_BYTES", 8)
    process = spawn("import os; os.write(1,b'evidence-marker')", worker=True)
    capture.attach(process, "worker", "one")
    process.wait(timeout=5)
    if failure in {"missing", "archive", "corrupt"}:
        capture.finish()
        if failure == "missing":
            next(iter(capture.paths)).unlink()
        elif failure == "corrupt":
            monkeypatch.setattr(
                direct.tarfile.TarFile, "extractfile", lambda *a: io.BytesIO(b"bad")
            )
        else:

            def broken_archive(*args, **kwargs):
                raise OSError("injected archive failure")

            monkeypatch.setattr(direct, "archive", broken_archive)
    with pytest.raises((AssertionError, OSError), match="capture|missing|archive"):
        direct.archive_and_remove(runtime, destination, capture)
    assert runtime.exists()


def test_existing_diagnostic_file_is_never_overwritten(tmp_path):
    capture = tcg.DiagnosticCapture(tmp_path)
    path = tmp_path / "worker-one-stdout.log"
    path.write_bytes(b"previous evidence")
    process = spawn("print('new evidence')", worker=True)
    try:
        with pytest.raises(FileExistsError):
            capture.attach(process, "worker", "one")
        with pytest.raises(AssertionError, match="capture"):
            capture.finish()
        assert path.read_bytes() == b"previous evidence"
    finally:
        process.communicate(timeout=5)


def test_oversized_readiness_fails_capture_without_removal(tmp_path):
    runtime, destination = tmp_path / "runtime", tmp_path / "archive"
    runtime.mkdir()
    destination.mkdir()
    capture = tcg.DiagnosticCapture(runtime)
    process = spawn("import os; os.write(1,b'x'*5000)", worker=True)
    capture.attach(process, "worker", "one")
    with pytest.raises(AssertionError, match="readiness"):
        capture.read_ready(process)
    process.wait(timeout=5)
    with pytest.raises(AssertionError, match="capture"):
        direct.archive_and_remove(runtime, destination, capture)
    assert runtime.exists()


def test_unfinished_reader_blocks_removal(tmp_path, monkeypatch):
    runtime, destination = tmp_path / "runtime", tmp_path / "archive"
    runtime.mkdir()
    destination.mkdir()
    capture = tcg.DiagnosticCapture(runtime)
    read_fd, write_fd = os.pipe()
    process = SimpleNamespace(pid=-1, stdout=os.fdopen(read_fd, "rb"), stderr=None)
    capture.attach(process, "worker", "one")
    monkeypatch.setattr(tcg, "PROCESS_STOP_TIMEOUT", 0.02)
    try:
        with pytest.raises(AssertionError, match="capture|drain"):
            direct.archive_and_remove(runtime, destination, capture)
        assert runtime.exists()
    finally:
        os.close(write_fd)
        for threads in capture.threads.values():
            for thread in threads:
                thread.join(1)


def test_fixture_cleanup_finishes_readers_even_after_archive_failure(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    capture = SimpleNamespace(paths=set(), finish=Mock())
    monkeypatch.setattr(direct.os, "getuid", lambda: 1000)
    monkeypatch.setattr(direct.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(direct.tempfile, "mkdtemp", lambda **kw: str(runtime))
    monkeypatch.setattr(direct.os, "readlink", lambda path: "isolated-test-network")
    monkeypatch.setattr(direct.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(direct, "DiagnosticCapture", lambda path: capture)
    monkeypatch.setattr(direct, "PROCESSES", [])
    monkeypatch.setattr(direct, "AUDIT_CONTROLLERS", {})
    monkeypatch.setattr(direct, "LIFETIME_FDS", [])
    monkeypatch.setattr(direct, "origin", Mock(side_effect=RuntimeError("injected setup failure")))
    monkeypatch.setattr(direct, "archive", Mock(side_effect=OSError("injected archive failure")))
    with pytest.raises(OSError, match="archive failure"):
        direct.exercise(
            tmp_path, "parent-network", tmp_path / "unused-image", tmp_path / "unused-worker"
        )
    assert capture.finish.call_count == 2
    assert runtime.exists()


def test_console_drains_trailing_output_after_exit(tmp_path):
    process = spawn("import os; os.write(1,b'console-tail')", worker=True)
    console = direct.Console(process)
    try:
        process.wait(timeout=5)
        console.drain()
        assert console.data == b"console-tail"
    finally:
        console.selector.close()
        process.stdout.close()
