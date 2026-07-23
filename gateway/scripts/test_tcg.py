#!/usr/bin/env python3
"""Required direct-QEMU rootless gateway integration check."""

from __future__ import annotations

import base64
import json
import os
import platform
import select
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROCESSES: list[subprocess.Popen[str]] = []
SERVERS: list[ThreadingHTTPServer] = []
PLACEHOLDER = "SBX_SECRET_package_" + "p" * 43
FAKE_TOKEN = "fake-real-token-task10"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def certificate_server(listener: socket.socket, certificate: bytes, stop: threading.Event) -> None:
    listener.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with conn:
            conn.sendall(certificate)


def echo_server(listener: socket.socket, stop: threading.Event) -> None:
    listener.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)


class PackageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        authorization = self.headers.get("Authorization", "")
        self.server.records.append((self.path, authorization))  # type: ignore[attr-defined]
        expected = "Basic " + base64.b64encode(f"aws:{FAKE_TOKEN}".encode()).decode()
        if authorization != expected:
            self.send_response(401)
            self.end_headers()
            return
        bodies = {
            "/pypi/repo/simple/demo/": b'<a href="/pypi/repo/packages/demo.whl">demo</a>',
            "/pypi/repo/packages/demo.whl": b"fake-wheel-content",
        }
        body = bodies.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def package_server(runtime: Path) -> tuple[ThreadingHTTPServer, Path]:
    certificate = runtime / "upstream-ca.pem"
    key = runtime / "upstream-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost.localdomain",
            "-addext",
            "subjectAltName=DNS:localhost.localdomain",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), PackageHandler)
    server.records = []  # type: ignore[attr-defined]
    SERVERS.append(server)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, certificate


def start_mediator(
    runtime: Path, upstream_ca: Path, upstream_port: int, key: bytes
) -> tuple[subprocess.Popen[str], dict[str, object], Path]:
    mediator_dir = Path(__file__).parents[1] / "mediator"
    confdir = runtime / "mediator"
    confdir.mkdir(mode=0o700)
    helper = runtime / "token-helper"
    helper.write_text(
        "#!/bin/sh\nprintf '%s%s%s\\n' 'fake-' 'real-token-' 'task10'\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    package_auth = {
        "version": 1,
        "rule_id": "package-index",
        "host": "localhost.localdomain",
        "port": upstream_port,
        "path_prefix": "/pypi/repo/",
        "username": "aws",
        "placeholder": PLACEHOLDER,
        "provider": {"type": "command", "argv": [str(helper)]},
    }
    key_read, key_write = os.pipe()
    ready_read, ready_write = os.pipe()
    auth_read, auth_write = os.pipe()
    os.write(key_write, key)
    os.write(auth_write, json.dumps(package_auth).encode())
    os.close(key_write)
    os.close(auth_write)
    process = subprocess.Popen(
        [
            str(mediator_dir / ".venv/bin/python"),
            str(mediator_dir / "launch.py"),
            "--vm-id",
            "tcg-test",
            "--incarnation",
            "worker1",
            "--key-fd",
            str(key_read),
            "--ready-fd",
            str(ready_write),
            "--package-auth-fd",
            str(auth_read),
            "--confdir",
            str(confdir),
            "--upstream-ca",
            str(upstream_ca),
        ],
        pass_fds=(key_read, ready_write, auth_read),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    PROCESSES.append(process)
    os.close(key_read)
    os.close(ready_write)
    os.close(auth_read)
    readable, _, _ = select.select([ready_read], [], [], 10)
    payload = os.read(ready_read, 4096) if readable else b""
    os.close(ready_read)
    require(bool(payload), "mediator readiness timed out")
    return process, json.loads(payload), confdir


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main() -> int:
    require(platform.system() == "Linux", "required Linux TCG test unavailable")
    qemu = shutil.which("qemu-system-x86_64")
    require(qemu is not None, "qemu-system-x86_64 is required")
    accelerators = subprocess.check_output([qemu, "-accel", "help"], text=True)
    require("tcg" in accelerators.split(), "QEMU TCG accelerator is required")

    gateway = Path(os.environ.get("SBX_GATEWAY_BIN", Path(__file__).parents[1] / "sbx-gateway"))
    require(gateway.is_file(), f"build the worker first: {gateway}")
    manifest_path = Path.home() / ".smolvm/images/rootless-gateway-test/smolvm-image.json"
    require(manifest_path.is_file(), "run gateway/scripts/build_test_guest.sh first")
    manifest = json.loads(manifest_path.read_text())
    kernel = (
        manifest_path.parent / manifest["kernel"]
        if "kernel" in manifest
        else Path(manifest["kernel_path"])
    )
    rootfs = (
        manifest_path.parent / manifest["rootfs"]
        if "rootfs" in manifest
        else Path(manifest["rootfs_path"])
    )
    require(kernel.is_file() and rootfs.is_file(), "test guest artifacts are missing")

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 18080))
    listener.listen()
    stop = threading.Event()
    thread = threading.Thread(target=echo_server, args=(listener, stop), daemon=True)
    thread.start()

    worker = qemu_process = mediator = None
    package = None
    with tempfile.TemporaryDirectory(prefix="sbx-gateway-tcg-") as directory:
        runtime = Path(directory)
        package, upstream_ca = package_server(runtime)
        key = os.urandom(32)
        mediator, mediator_ready, mediator_confdir = start_mediator(
            runtime, upstream_ca, package.server_port, key
        )
        ca = mediator_confdir.joinpath("mitmproxy-ca-cert.pem").read_bytes()
        require(b"PRIVATE KEY" not in ca, "mediator CA export included a private key")
        ca_listener = socket.socket()
        ca_listener.bind(("127.0.0.1", 0))
        ca_listener.listen()
        threading.Thread(
            target=certificate_server,
            args=(ca_listener, PLACEHOLDER.encode() + b"\n" + ca, stop),
            daemon=True,
        ).start()
        ca_port = ca_listener.getsockname()[1]
        raw = runtime / "raw.json"
        config = runtime / "config.json"
        qemu_socket = runtime / "qemu.sock"
        raw.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vm_id": "tcg-test",
                    "generation": "g1",
                    "rules": [
                        {
                            "id": "echo",
                            "protocol": "tcp",
                            "host": "localhost.localdomain",
                            "ports": [18080],
                            "allow_ranges": ["127.0.0.0/8"],
                        },
                        {
                            "id": "package-index",
                            "protocol": "http",
                            "host": "localhost.localdomain",
                            "ports": [package.server_port],
                            "methods": ["GET"],
                            "paths": ["/pypi/repo/"],
                        },
                        {
                            "id": "public-ca-install",
                            "protocol": "tcp",
                            "host": "localhost.localdomain",
                            "ports": [ca_port],
                            "allow_ranges": ["127.0.0.0/8"],
                        },
                    ],
                    "quotas": {
                        "tcp_flows": 512,
                        "flows_per_destination": 64,
                        "dns_mappings": 1024,
                        "open_files": 4096,
                    },
                }
            )
        )
        prepared = subprocess.check_output(
            [str(gateway), "--prepare", "--config", str(raw)], text=True
        )
        config.write_text(prepared)
        digest = json.loads(prepared)["digest"]
        worker_key_read, worker_key_write = os.pipe()
        os.write(worker_key_write, key)
        os.close(worker_key_write)
        worker = subprocess.Popen(
            [
                str(gateway),
                "--socket",
                str(qemu_socket),
                "--config",
                str(config),
                "--vm-id",
                "tcg-test",
                "--generation",
                "g1",
                "--digest",
                digest,
                "--mediator-address",
                f"{mediator_ready['host']}:{mediator_ready['port']}",
                "--capability-key-fd",
                str(worker_key_read),
                "--worker-incarnation",
                "worker1",
            ],
            pass_fds=(worker_key_read,),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        os.close(worker_key_read)
        PROCESSES.append(worker)
        for _ in range(200):
            require(worker.poll() is None, "worker exited before readiness")
            if qemu_socket.is_socket():
                break
            time.sleep(0.025)
        require(qemu_socket.is_socket(), "worker socket readiness timed out")

        boot_args = manifest.get(
            "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
        ) + (
            " smolvm.network=guest"
            f" sbx.package_port={package.server_port}"
            f" sbx.gateway_ca_port={ca_port}"
        )
        command = [
            qemu,
            "-nodefaults",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-nographic",
            "-no-reboot",
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
            boot_args,
            "-drive",
            f"file={rootfs},if=none,format=raw,id=rootdisk",
            "-netdev",
            f"stream,id=net0,server=off,addr.type=unix,addr.path={qemu_socket}",
            "-device",
            "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56",
            "-device",
            "virtio-blk-device,drive=rootdisk",
        ]
        joined = " ".join(command)
        require("accel=tcg" in joined and "stream,id=net0" in joined, "unsafe QEMU command")
        forbidden = ("-net user", "tap", "nft", "sudo", "/dev/kvm")
        require(
            all(value not in joined for value in forbidden),
            "forbidden host networking requested",
        )
        qemu_process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PROCESSES.append(qemu_process)
        transcript: list[str] = []
        deadline = time.monotonic() + 120
        selector = selectors.DefaultSelector()
        assert qemu_process.stdout is not None
        os.set_blocking(qemu_process.stdout.fileno(), False)
        selector.register(qemu_process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            require(qemu_process.poll() is None, "QEMU exited before test completion")
            if not selector.select(timeout=1):
                continue
            chunk = os.read(qemu_process.stdout.fileno(), 65536).decode(errors="replace")
            transcript.append(chunk)
            if "GATEWAY_TEST_PASS" in "".join(transcript):
                output = "".join(transcript)
                expected_auth = "Basic " + base64.b64encode(f"aws:{FAKE_TOKEN}".encode()).decode()
                require(
                    package.records  # type: ignore[attr-defined]
                    == [
                        ("/pypi/repo/simple/demo/", expected_auth),
                        ("/pypi/repo/packages/demo.whl", expected_auth),
                    ],
                    "fake package index did not receive structurally replaced auth: "
                    f"{package.records!r}",  # type: ignore[attr-defined]
                )
                require(PLACEHOLDER not in repr(package.records), "placeholder reached upstream")  # type: ignore[attr-defined]
                process_material = b""
                for process in (worker, mediator, qemu_process):
                    process_material += Path(f"/proc/{process.pid}/cmdline").read_bytes()
                    process_material += Path(f"/proc/{process.pid}/environ").read_bytes()
                state_material = b"".join(
                    path.read_bytes() for path in runtime.rglob("*") if path.is_file()
                )
                forbidden_secrets = (
                    FAKE_TOKEN.encode(),
                    base64.b64encode(f"aws:{FAKE_TOKEN}".encode()),
                )
                status_material = (
                    prepared.encode() + json.dumps(mediator_ready).encode() + output.encode()
                )
                for secret in forbidden_secrets:
                    require(
                        secret not in process_material,
                        "fake token leaked into process metadata",
                    )
                    require(secret not in state_material, "fake token leaked into host state")
                    require(secret not in status_material, "fake token leaked into status output")
                terminate(worker)
                require(worker.poll() is not None, "worker did not stop")
                worker_stdout, _ = worker.communicate()
                require(qemu_process.poll() is None, "QEMU did not survive worker loss")
                terminate(mediator)
                mediator_stdout, mediator_stderr = mediator.communicate()
                logs = (mediator_stdout + mediator_stderr + worker_stdout + output).encode()
                for secret in (*forbidden_secrets, PLACEHOLDER.encode()):
                    require(secret not in logs, "secret leaked into host or guest logs")
                transcript.append("GATEWAY_WORKER_LOSS_PASS\n")
                transcript.append("GATEWAY_FAKE_TOKEN_SCAN_PASS\n")
                stop.set()
                listener.close()
                ca_listener.close()
                print("".join(transcript), end="")
                return 0
        print("".join(transcript), end="", file=sys.stderr)
        raise SystemExit("TCG gateway test timed out")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for process in reversed(PROCESSES):
            terminate(process)
        for server in SERVERS:
            server.shutdown()
            server.server_close()
