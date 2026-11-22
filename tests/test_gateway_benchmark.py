from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest
from gateway.scripts import task19_gate


@pytest.mark.parametrize("guest_ready", [True, False])
def test_readiness_requires_actual_guest_marker_and_samples_before_cleanup(
    tmp_path, monkeypatch, guest_ready
):
    descriptor = SimpleNamespace(fileno=lambda: 7)
    process = SimpleNamespace(stdout=descriptor, poll=lambda: None)
    key = SimpleNamespace(fileobj=descriptor)
    selector = SimpleNamespace(
        register=lambda *args: None, select=lambda *args: [(key, 1)], close=lambda: None
    )
    ticks, reads, cleaned = count(), [], []

    def read(*args):
        reads.append(True)
        return b"TASK19_INGRESS_READY" if guest_ready and len(reads) == 2 else b"worker-ready\n"

    def usage(pids):
        assert pids == [11, 12] and not cleaned
        return (1234, 2, 2, 9)

    monkeypatch.setattr(task19_gate, "PROCESSES", [])
    monkeypatch.setattr(task19_gate.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(task19_gate.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(task19_gate.os, "read", read)
    monkeypatch.setattr(task19_gate.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(task19_gate.time, "perf_counter", lambda: 20.0)
    monkeypatch.setattr(task19_gate, "VM_TIMEOUT", 3)
    monkeypatch.setattr(task19_gate, "terminate", lambda child: cleaned.append(child))
    monkeypatch.setattr(task19_gate, "process_usage", usage)
    artifacts = tmp_path / "boot.log"
    if guest_ready:
        assert task19_gate.boot_ready(["qemu"], artifacts, [11, 12]) == (20.0, 1234)
        assert len(reads) == 2
    else:
        with pytest.raises(RuntimeError, match="actual guest network readiness timed out"):
            task19_gate.boot_ready(["qemu"], artifacts, [11, 12])
    assert cleaned == [process] and artifacts.read_bytes().startswith(b"worker-ready")


@pytest.mark.parametrize("mode", ["gateway", "slirp"])
def test_tcg_benchmark_uses_supported_q35_timers_and_pci_devices(mode, tmp_path):
    command = task19_gate.qemu_command(
        "qemu",
        {"kernel": "kernel", "rootfs": "disk", "boot_args": "console=ttyS0 pci=off root=/dev/vda"},
        tmp_path / "manifest.json",
        mode,
        443,
        8443,
        qemu_socket=tmp_path / "qemu.sock",
        slirp_ingress=3000,
    )
    assert command[command.index("-machine") + 1] == "q35,accel=tcg"
    assert command[command.index("-cpu") + 1] == "max"
    assert "pci=off" not in command[command.index("-append") + 1].split()
    assert "virtio-blk-pci,drive=rootdisk" in command
    assert any(arg.startswith("virtio-net-pci,") for arg in command)


def test_concurrency_requires_shared_overlap_and_distinct_origin_requests(monkeypatch):
    monkeypatch.setattr(task19_gate, "CONCURRENT_REQUESTS", 2)
    monkeypatch.setattr(task19_gate, "CONCURRENT_HOLD_SECONDS", 0.03)
    server = task19_gate.BenchmarkServer(("127.0.0.1", 0))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    server.hold_concurrent, ["/concurrent?run=test&n=1", "/concurrent?run=test&n=2"]
                )
            )
        measured = task19_gate.concurrency_summary(server.concurrency_events)["test"]
        assert measured["starts"] == measured["ends"] == measured["unique_requests"] == 2
        assert measured["overlap_seconds"] >= 0.03
        assert server.maximum_active == 2 and server.active == 0
    finally:
        server.server_close()


def test_benchmark_accept_defers_tls_handshake(monkeypatch, tmp_path):
    captured = {}
    listener = object()
    server = SimpleNamespace(socket=listener, serve_forever=lambda: None)

    class Context:
        def __init__(self, protocol):
            pass

        def load_cert_chain(self, *args):
            pass

        def set_alpn_protocols(self, protocols):
            assert protocols == ["h2", "http/1.1"]

        def wrap_socket(self, sock, **kwargs):
            assert sock is listener
            captured.update(kwargs)
            return listener

    monkeypatch.setattr(task19_gate, "tool", lambda *args: "openssl")
    monkeypatch.setattr(task19_gate.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(task19_gate, "BenchmarkServer", lambda address: server)
    monkeypatch.setattr(task19_gate.ssl, "SSLContext", Context)
    monkeypatch.setattr(
        task19_gate.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(task19_gate, "SERVERS", [])
    task19_gate.make_server(tmp_path)
    assert captured == {"server_side": True, "do_handshake_on_connect": False}


def test_benchmark_mediator_policy_matches_worker_destinations(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    (tmp_path / "benchmark-ca.pem").write_text("fixture CA")

    def mediator(*args, **kwargs):
        captured.update(kwargs)
        captured["upstream_ca"] = args[1]
        raise RuntimeError("captured benchmark policy")

    monkeypatch.setattr(task19_gate, "start_mediator", mediator)
    with pytest.raises(RuntimeError, match="captured benchmark policy"):
        task19_gate.start_pair(tmp_path / "pair", Path("/unused-worker"), 443)
    worker = task19_gate.raw_config(tmp_path, 443, 8443, 3000)
    expected = [
        (rule["id"], rule["host"], rule["ports"])
        for rule in worker["rules"]
        if rule["protocol"] == "http"
    ]
    assert captured["upstream_ca"].parent == tmp_path / "pair"
    assert captured["upstream_ca"].read_text() == "fixture CA"
    policy = captured["policy"]
    assert [(rule["id"], rule["host"], rule["ports"]) for rule in policy["rules"]] == expected
    assert all(rule["methods"] == rule["paths"] == [] for rule in policy["rules"])
    assert policy["credentials"] == []
