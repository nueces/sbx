#!/usr/bin/env python3
"""Bounded Task 19 TCG performance benchmark."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import platform
import selectors
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import test_tcg as tcg_helpers
from test_multi_vm_tcg import process_usage
from test_tcg import (
    MAX_TRANSCRIPT_BYTES,
    PROCESSES,
    SERVERS,
    certificate_server,
    child_environment,
    require,
    start_mediator,
    start_worker,
    terminate,
    tool,
)

COMMAND_TIMEOUT = 30
VM_TIMEOUT = int(os.environ.get("SBX_TASK19_VM_TIMEOUT", "900"))
READINESS_SAMPLES = 20
RSS_TARGET = 256 * 1024 * 1024
RSS_CEILING = 512 * 1024 * 1024
LARGE_BYTES = 100 * 1024 * 1024


class BenchmarkServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, BenchmarkHandler)
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()


class BenchmarkHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, size: int, *, slow: bool = False) -> None:
        self.send_response(200)
        self.send_header("content-length", str(size))
        self.end_headers()
        chunk = b"x" * 65536
        for offset in range(0, size, len(chunk)):
            self.wfile.write(chunk[: min(len(chunk), size - offset)])
            if slow:
                self.wfile.flush()
                time.sleep(0.001)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/bytes/"):
            self._reply(int(self.path.rsplit("/", 1)[1]))
            return
        if self.path.startswith("/slow-write/"):
            self._reply(int(self.path.rsplit("/", 1)[1]), slow=True)
            return
        if self.path == "/latency":
            self._reply(2)
            return
        if self.path == "/concurrent":
            server = self.server
            with server.lock:  # type: ignore[attr-defined]
                server.active += 1  # type: ignore[attr-defined]
                server.maximum_active = max(  # type: ignore[attr-defined]
                    server.maximum_active, server.active  # type: ignore[attr-defined]
                )
            try:
                time.sleep(0.5)
                body = b"concurrent"
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with server.lock:  # type: ignore[attr-defined]
                    server.active -= 1  # type: ignore[attr-defined]
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/slow-read":
            self.send_error(404)
            return
        remaining = int(self.headers["content-length"])
        total = remaining
        while remaining:
            chunk = self.rfile.read(min(4096, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            time.sleep(0.001)
        body = str(total - remaining).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def percentile(values: list[float] | list[int], fraction: float) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))])


def make_server(runtime: Path) -> tuple[BenchmarkServer, Path]:
    certificate, key = runtime / "benchmark-ca.pem", runtime / "benchmark-key.pem"
    subprocess.run(
        [
            tool("SBX_OPENSSL_BIN", "openssl"),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=a.localhost",
            "-addext",
            "subjectAltName=DNS:a.localhost,DNS:b.localhost",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=COMMAND_TIMEOUT,
    )
    server = BenchmarkServer(("127.0.0.1", 0))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    SERVERS.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, certificate


def free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


def ingress_download(port: int) -> tuple[int, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    started = time.perf_counter()
    connection.request("GET", "/bytes")
    response = connection.getresponse()
    total = 0
    while chunk := response.read(65536):
        total += len(chunk)
    elapsed = time.perf_counter() - started
    connection.close()
    require(response.status == 200 and total == LARGE_BYTES, "ingress transfer failed")
    return total, elapsed


def process_cpu_seconds(pids: list[int]) -> float:
    pending, seen, ticks = list(pids), set(), 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = Path("/proc") / str(pid)
        with suppress(OSError, ValueError):
            fields = process.joinpath("stat").read_text().split()
            ticks += int(fields[13]) + int(fields[14])
            for task in process.joinpath("task").iterdir():
                with suppress(OSError):
                    pending.extend(
                        int(item) for item in task.joinpath("children").read_text().split()
                    )
    return ticks / os.sysconf("SC_CLK_TCK")


def raw_config(path: Path, port: int, ca_port: int, ingress_port: int) -> dict[str, Any]:
    return {
        "version": 1,
        "vm_id": "task19",
        "generation": "g1",
        "rules": [
            {"id": "benchmark-a", "protocol": "http", "host": "a.localhost", "ports": [port]},
            {"id": "benchmark-b", "protocol": "http", "host": "b.localhost", "ports": [port]},
            {
                "id": "benchmark-ca",
                "protocol": "tcp",
                "host": "a.localhost",
                "ports": [ca_port],
                "allow_ranges": ["127.0.0.0/8"],
            },
        ],
        "ingress": [
            {
                "id": "benchmark-ingress",
                "listen_host": "127.0.0.1",
                "host_port": ingress_port,
                "guest_address": "192.0.2.2",
                "guest_port": 3000,
            }
        ],
        "quotas": {
            "tcp_flows": 512,
            "flows_per_destination": 64,
            "ingress_flows": 64,
            "dns_mappings": 1024,
            "open_files": 4096,
        },
    }


def start_pair(runtime: Path, gateway: Path, port: int):
    runtime.mkdir(mode=0o700)
    key = os.urandom(32)
    mediator, mediator_ready, confdir = start_mediator(
        runtime, runtime.parent / "benchmark-ca.pem", port, key, "task19-worker", "task19"
    )
    ca_listener = socket.socket()
    ca_listener.bind(("127.0.0.1", 0))
    ca_listener.listen()
    ca_stop = threading.Event()
    threading.Thread(
        target=certificate_server,
        args=(ca_listener, confdir.joinpath("mitmproxy-ca-cert.pem").read_bytes(), ca_stop),
        daemon=True,
    ).start()
    ca_port = ca_listener.getsockname()[1]
    qemu_socket = runtime / "qemu.sock"
    qemu_listener = socket.socket(socket.AF_UNIX)
    qemu_listener.bind(str(qemu_socket))
    os.chmod(qemu_socket, 0o600)
    qemu_listener.listen()
    ingress_listener = socket.socket()
    ingress_listener.bind(("127.0.0.1", 0))
    ingress_listener.listen()
    raw, config = runtime / "raw.json", runtime / "config.json"
    raw.write_text(
        json.dumps(raw_config(raw, port, ca_port, ingress_listener.getsockname()[1])),
        encoding="utf-8",
    )
    prepared = subprocess.check_output(
        [str(gateway), "--prepare", "--config", str(raw)], text=True, timeout=COMMAND_TIMEOUT
    )
    config.write_text(prepared, encoding="utf-8")
    config.chmod(0o600)
    worker = start_worker(
        gateway,
        qemu_socket,
        qemu_listener,
        config,
        json.loads(prepared)["digest"],
        mediator_ready,
        key,
        "task19-worker",
        [ingress_listener],
        vm_id="task19",
        ingress_ids=("benchmark-ingress",),
    )
    return (
        worker,
        mediator,
        qemu_socket,
        qemu_listener,
        ingress_listener,
        ca_listener,
        ca_stop,
        ca_port,
    )


def qemu_command(
    qemu: str,
    manifest: dict[str, Any],
    manifest_path: Path,
    mode: str,
    port: int,
    ca_port: int,
    *,
    qemu_socket: Path | None = None,
    slirp_ingress: int | None = None,
) -> list[str]:
    kernel = manifest_path.parent / manifest.get("kernel", manifest.get("kernel_path"))
    rootfs = manifest_path.parent / manifest.get("rootfs", manifest.get("rootfs_path"))
    boot = manifest.get(
        "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
    ) + (
        f" smolvm.network=guest sbx.task19_mode={mode}"
        f" sbx.task19_port={port} sbx.task19_ca_port={ca_port}"
    )
    command = [
        qemu,
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-nodefaults",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-nographic",
        "-no-reboot",
        "-snapshot",
        "-smp",
        "1",
        "-m",
        "512",
        "-machine",
        "microvm,accel=tcg",
        "-cpu",
        "qemu64",
        "-kernel",
        str(kernel),
        "-append",
        boot,
        "-drive",
        f"file={rootfs},if=none,format=raw,id=rootdisk",
    ]
    if mode == "gateway":
        assert qemu_socket is not None
        command += [
            "-netdev",
            f"stream,id=net0,server=off,reconnect-ms=100,addr.type=unix,addr.path={qemu_socket}",
        ]
    else:
        assert slirp_ingress is not None
        command += [
            "-netdev",
            f"user,id=net0,hostfwd=tcp:127.0.0.1:{slirp_ingress}-:3000",
        ]
    return command + [
        "-device",
        "virtio-net-device,netdev=net0,mac=52:54:00:19:00:01",
        "-device",
        "virtio-blk-device,drive=rootdisk",
    ]


def run_vm(
    command: list[str], mode: str, runtime: Path, ingress_port: int, owned_pids: list[int]
) -> tuple[dict[str, Any], dict[str, Any], bytes, subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=child_environment(runtime),
    )
    PROCESSES.append(process)
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    transcript = bytearray()
    result = None
    ingress_result = None
    cpu_start = process_cpu_seconds([*owned_pids, process.pid])
    deadline = time.monotonic() + VM_TIMEOUT
    while time.monotonic() < deadline and result is None:
        require(process.poll() is None, f"{mode} QEMU exited")
        for _, _ in selector.select(1):
            chunk = os.read(process.stdout.fileno(), 65536)
            transcript.extend(chunk)
            require(len(transcript) <= MAX_TRANSCRIPT_BYTES, f"{mode} transcript exceeded limit")
        output = transcript.decode(errors="replace")
        if "TASK19_INGRESS_READY" in output and ingress_result is None:
            size, elapsed = ingress_download(ingress_port)
            ingress_result = {
                "bytes": size,
                "seconds": elapsed,
                "mbps": size * 8 / elapsed / 1_000_000,
            }
        for line in output.splitlines():
            if line.startswith("TASK19_RESULT "):
                try:
                    result = json.loads(
                        base64.b64decode(line.removeprefix("TASK19_RESULT "), validate=True)
                    )
                    break
                except (ValueError, json.JSONDecodeError):
                    continue
    if result is None or ingress_result is None:
        print(transcript.decode(errors="replace")[-16000:], file=sys.stderr)
        raise SystemExit(f"{mode} benchmark timed out")
    cpu_seconds = process_cpu_seconds([*owned_pids, process.pid]) - cpu_start
    result["cpu_seconds"] = cpu_seconds
    result["cpu_seconds_per_transferred_gib"] = cpu_seconds / (249 / 1024)
    return result, ingress_result, bytes(transcript), process


def direct_benchmark(server: BenchmarkServer, certificate: Path) -> dict[str, float]:
    context = ssl.create_default_context(cafile=str(certificate))
    connection = http.client.HTTPSConnection(
        "a.localhost", server.server_port, context=context, timeout=180
    )
    started = time.perf_counter()
    connection.request("GET", f"/bytes/{LARGE_BYTES}")
    response = connection.getresponse()
    total = 0
    while chunk := response.read(65536):
        total += len(chunk)
    elapsed = time.perf_counter() - started
    connection.close()
    require(total == LARGE_BYTES, "direct benchmark transfer failed")
    return {"bytes": total, "seconds": elapsed, "mbps": total * 8 / elapsed / 1_000_000}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def environment(qemu: str, manifest_path: Path) -> dict[str, Any]:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uname": " ".join(platform.uname()),
        "accelerator": "tcg",
        "qemu_version": subprocess.check_output([qemu, "--version"], text=True).splitlines()[0],
        "host_cpus": os.cpu_count(),
        "guest_cpus": 1,
        "guest_ram_mib": 512,
        "host_memory_bytes": int(Path("/proc/meminfo").read_text().split()[1]) * 1024,
        "open_file_limit": os.sysconf("SC_OPEN_MAX"),
        "dev_kvm": Path("/dev/kvm").exists(),
        "guest_manifest": str(manifest_path),
        "guest_manifest_sha256": file_sha256(manifest_path),
        "guest_files": {
            path.name: {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
            for path in manifest_path.parent.iterdir()
            if path.is_file() and path.name != "rootfs.ext4"
        }
        | {
            "rootfs.ext4": {
                "bytes": manifest_path.parent.joinpath("rootfs.ext4").stat().st_size,
                "sha256": file_sha256(manifest_path.parent / "rootfs.ext4"),
            }
        },
    }


def main() -> int:
    require(platform.system() == "Linux", "Task 19 gate requires Linux")
    tcg_helpers.COMMAND_TIMEOUT = COMMAND_TIMEOUT
    artifact_dir = Path(os.environ.get("SBX_TASK19_ARTIFACTS", "task19-results")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    qemu = tool("SBX_QEMU_BIN", "qemu-system-x86_64")
    require(
        "tcg" in subprocess.check_output([qemu, "-accel", "help"], text=True).split(),
        "TCG missing",
    )
    require(not Path("/dev/kvm").exists(), "Task 19 must use constrained no-KVM environment")
    gateway = Path(tool("SBX_GATEWAY_BIN", str(Path(__file__).parents[1] / "sbx-gateway")))
    manifest_path = Path(
        os.environ.get(
            "SBX_GATEWAY_TEST_IMAGE",
            Path.home() / ".smolvm/images/rootless-gateway-test/smolvm-image.json",
        )
    ).expanduser()
    require(manifest_path.is_file(), "Task 19 guest image missing")
    manifest = json.loads(manifest_path.read_text())
    output: dict[str, Any] = {"environment": environment(qemu, manifest_path)}

    with tempfile.TemporaryDirectory(prefix="sbx-task19-") as raw_runtime:
        runtime = Path(raw_runtime)
        server, certificate = make_server(runtime)
        ca_listener = socket.socket()
        ca_listener.bind(("127.0.0.1", 0))
        ca_listener.listen()
        ca_stop = threading.Event()
        threading.Thread(
            target=certificate_server,
            args=(ca_listener, certificate.read_bytes(), ca_stop),
            daemon=True,
        ).start()
        ca_port = ca_listener.getsockname()[1]
        output["direct"] = direct_benchmark(server, certificate)

        readiness, rss = [], []
        for index in range(READINESS_SAMPLES):
            sample_runtime = runtime / f"readiness-{index}"
            started = time.perf_counter()
            (
                worker,
                mediator,
                _,
                qemu_listener,
                ingress_listener,
                pair_ca_listener,
                pair_ca_stop,
                _,
            ) = start_pair(sample_runtime, gateway, server.server_port)
            readiness.append(time.perf_counter() - started)
            rss.append(process_usage([worker.pid, mediator.pid])[0])
            terminate(worker)
            terminate(mediator)
            qemu_listener.close()
            ingress_listener.close()
            pair_ca_stop.set()
            pair_ca_listener.close()
        output["readiness_seconds"] = readiness
        output["idle_rss_bytes"] = rss

        gateway_runtime = runtime / "gateway"
        (
            worker,
            mediator,
            qemu_socket,
            qemu_listener,
            ingress_listener,
            pair_ca_listener,
            pair_ca_stop,
            pair_ca_port,
        ) = start_pair(gateway_runtime, gateway, server.server_port)
        gateway_command = qemu_command(
            qemu,
            manifest,
            manifest_path,
            "gateway",
            server.server_port,
            pair_ca_port,
            qemu_socket=qemu_socket,
        )
        gateway_result, gateway_ingress, gateway_log, gateway_qemu = run_vm(
            gateway_command,
            "gateway",
            gateway_runtime,
            ingress_listener.getsockname()[1],
            [worker.pid, mediator.pid],
        )
        gateway_usage = process_usage([worker.pid, mediator.pid])
        output["gateway_upstream_maximum_concurrency"] = server.maximum_active
        terminate(gateway_qemu)
        terminate(worker)
        terminate(mediator)
        worker_output, _ = worker.communicate(timeout=5)
        mediator_output, mediator_error = mediator.communicate(timeout=5)
        qemu_listener.close()
        ingress_listener.close()
        pair_ca_stop.set()
        pair_ca_listener.close()
        output["gateway"] = gateway_result
        output["gateway_ingress"] = gateway_ingress
        output["gateway_usage"] = {
            "rss_bytes": gateway_usage[0],
            "fds": gateway_usage[1],
            "processes": gateway_usage[2],
            "threads": gateway_usage[3],
        }
        output["gateway_log_bytes"] = sum(
            map(len, (gateway_log, worker_output, mediator_output, mediator_error))
        )

        slirp_ingress = free_port()
        slirp_runtime = runtime / "slirp"
        server.maximum_active = 0
        slirp_runtime.mkdir(mode=0o700)
        slirp_command = qemu_command(
            qemu,
            manifest,
            manifest_path,
            "slirp",
            server.server_port,
            ca_port,
            slirp_ingress=slirp_ingress,
        )
        slirp_result, slirp_ingress_result, slirp_log, slirp_qemu = run_vm(
            slirp_command, "slirp", slirp_runtime, slirp_ingress, []
        )
        terminate(slirp_qemu)
        output["slirp"] = slirp_result
        output["slirp_ingress"] = slirp_ingress_result
        output["slirp_log_bytes"] = len(slirp_log)
        output["slirp_upstream_maximum_concurrency"] = server.maximum_active
        ca_stop.set()
        ca_listener.close()

    readiness_p95 = percentile(readiness, 0.95)
    rss_p95 = percentile(rss, 0.95)
    gateway_mbps = gateway_result[f"throughput_{LARGE_BYTES}_mbps"]
    slirp_mbps = slirp_result[f"throughput_{LARGE_BYTES}_mbps"]
    added_latency = max(
        0.0, gateway_result["latency_ms"]["p95"] - slirp_result["latency_ms"]["p95"]
    )
    gates = {
        "readiness_p95_at_most_1s": readiness_p95 <= 1.0,
        "idle_rss_p95_at_most_256mib": rss_p95 <= RSS_TARGET,
        "idle_rss_hard_ceiling_512mib": max(rss) <= RSS_CEILING,
        "large_https_at_least_100mbps": gateway_mbps >= 100,
        "large_https_at_least_half_slirp": gateway_mbps >= slirp_mbps * 0.5,
        "added_latency_p95_at_most_10ms": added_latency <= 10,
        "concurrency_128": gateway_result["concurrent_requests"] == 128,
        "concurrency_reached_upstream": output["gateway_upstream_maximum_concurrency"] >= 128,
        "backpressure_completed": all(
            gateway_result["backpressure"][name] == 8 * 1024 * 1024
            for name in ("upload_bytes", "download_bytes")
        ),
        "logs_bounded": output["gateway_log_bytes"] <= MAX_TRANSCRIPT_BYTES,
    }
    output["summary"] = {
        "readiness_p95_seconds": readiness_p95,
        "idle_rss_p95_bytes": rss_p95,
        "gateway_large_mbps": gateway_mbps,
        "slirp_large_mbps": slirp_mbps,
        "throughput_ratio": gateway_mbps / slirp_mbps,
        "added_latency_p95_ms": added_latency,
        "gates": gates,
        "status": "PASS" if all(gates.values()) else "FAIL",
        "scope": "QEMU TCG regression and functional data only; not a KVM or production claim",
    }
    result_path = artifact_dir / "performance-raw.json"
    result_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], indent=2, sort_keys=True))
    require(all(gates.values()), f"Task 19 performance gate failed; raw results: {result_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for process in reversed(PROCESSES):
            terminate(process)
        for server in SERVERS:
            server.shutdown()
            server.server_close()
