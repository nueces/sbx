#!/usr/bin/env python3
"""Required direct-QEMU rootless gateway integration check."""

from __future__ import annotations

import base64
import http.client
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
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sbx import gateway as gateway_runtime

AUDIT_CONTROLLERS: dict[Path, gateway_runtime.GatewayController] = {}
DNS_CONTROLLERS: dict[Path, gateway_runtime.GatewayController] = {}
PROCESSES: list[subprocess.Popen[str]] = []
SERVERS: list[ThreadingHTTPServer] = []
LIFETIME_FDS: list[int] = []
PLACEHOLDER = "p" * 43
VM_ID = "tcg-" + "v" * 124
FAKE_TOKEN = "fake-real-token-task10"
COMMAND_TIMEOUT = 10
PROCESS_STOP_TIMEOUT = 5
INTEGRATION_TIMEOUT = 180
MAX_TRANSCRIPT_BYTES = 1024 * 1024
ROOT = Path(__file__).parents[2]
GATEWAY_DIR = ROOT / "gateway"


def tool(environment: str, *names: str) -> str:
    override = os.environ.get(environment)
    if override:
        path = Path(override).expanduser().absolute()
        target = path.resolve(strict=True)
        require(target.is_file() and os.access(target, os.X_OK), f"unsafe {environment} tool")
        return str(path)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return str(Path(resolved).absolute())
    raise SystemExit(f"required tool is missing: {', '.join(names)}")


def child_environment(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "LANG": "C.UTF-8", "PATH": os.defpath, "TZ": "UTC"}


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
            try:
                while data := conn.recv(65536):
                    conn.sendall(data)
            except OSError:
                continue


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
            tool("SBX_OPENSSL_BIN", "openssl"),
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
        timeout=COMMAND_TIMEOUT,
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
    runtime: Path,
    upstream_ca: Path,
    upstream_port: int,
    key: bytes,
    incarnation: str = "worker1",
    vm_id: str = VM_ID,
    open_files: int = 2048,
) -> tuple[subprocess.Popen[str], dict[str, object], Path]:
    mediator_dir = GATEWAY_DIR / "mediator"
    confdir = runtime / "mediator"
    confdir.mkdir(mode=0o700, exist_ok=True)
    policy = {
        "version": 2,
        "rules": [
            {
                "id": "package-index",
                "host": "localhost.localdomain",
                "ports": [upstream_port],
                "methods": ["GET"],
                "paths": ["/pypi/repo"],
                "hooks": [],
            }
        ],
        "credentials": [
            {
                "credential": "package",
                "rule_id": "package-index",
                "methods": ["GET"],
                "paths": ["/pypi/repo"],
                "header": {"name": "Authorization", "format": "basic-password", "username": "aws"},
                "placeholder": PLACEHOLDER,
            }
        ],
    }
    key_read, key_write = os.pipe()
    ready_read, ready_write = os.pipe()
    policy_read, policy_write = os.pipe()
    lifetime_read, lifetime_write = os.pipe()
    LIFETIME_FDS.append(lifetime_write)
    broker, broker_child = socket.socketpair()
    os.write(key_write, key)
    os.write(policy_write, json.dumps(policy).encode())
    os.close(key_write)
    os.close(policy_write)

    def serve_broker() -> None:
        try:
            while header := broker.recv(4):
                size = int.from_bytes(header, "big")
                request = json.loads(broker.recv(size))
                response = {
                    "version": 1,
                    "request_id": request["request_id"],
                    "incarnation": incarnation,
                    "value": FAKE_TOKEN,
                }
                data = json.dumps(response, separators=(",", ":")).encode()
                broker.sendall(len(data).to_bytes(4, "big") + data)
        finally:
            broker.close()

    threading.Thread(target=serve_broker, daemon=True).start()
    if confdir not in AUDIT_CONTROLLERS:
        config = gateway_runtime.parse_config({"network": {}})
        assert config is not None
        controller = gateway_runtime.GatewayController(config, vm_id, root=runtime)
        controller.directory = confdir
        controller.generation = "tcg-generation"
        controller._audit_stream = controller._open_log("audit.log")
        AUDIT_CONTROLLERS[confdir] = controller
    controller = AUDIT_CONTROLLERS[confdir]
    controller.worker_incarnation = incarnation
    audit_read, audit_write = os.pipe()
    ack_read, ack_write = os.pipe()
    controller._route_audit(
        os.fdopen(audit_read, "rb", buffering=0), controller._audit_stream, ack_write
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(mediator_dir / "fixture_launch.py"),
            str(mediator_dir / "launch.py"),
            "--vm-id",
            vm_id,
            "--incarnation",
            incarnation,
            "--key-fd",
            str(key_read),
            "--ready-fd",
            str(ready_write),
            "--policy-fd",
            str(policy_read),
            "--broker-fd",
            str(broker_child.fileno()),
            "--lifetime-fd",
            str(lifetime_read),
            "--confdir",
            str(confdir),
            "--upstream-ca",
            str(upstream_ca),
            "--open-files",
            str(open_files),
            "--audit-fd",
            str(audit_write),
            "--audit-ack-fd",
            str(ack_read),
            "--generation",
            controller.generation,
        ],
        pass_fds=(
            key_read,
            ready_write,
            policy_read,
            broker_child.fileno(),
            lifetime_read,
            audit_write,
            ack_read,
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=child_environment(runtime) | {"PATH": os.environ.get("PATH", os.defpath)},
    )
    PROCESSES.append(process)
    os.close(audit_write)
    os.close(ack_read)
    os.close(key_read)
    os.close(ready_write)
    os.close(policy_read)
    os.close(lifetime_read)
    broker_child.close()
    readable, _, _ = select.select([ready_read], [], [], COMMAND_TIMEOUT)
    payload = os.read(ready_read, 4096) if readable else b""
    os.close(ready_read)
    if not payload:
        terminate(process)
        stdout, stderr = process.communicate(timeout=PROCESS_STOP_TIMEOUT)
        files = sorted(path.name for path in confdir.iterdir())
        raise SystemExit(f"mediator readiness timed out ({files}): {(stdout + stderr)[-4000:]}")
    return process, json.loads(payload), confdir


def start_worker(
    gateway: Path,
    qemu_socket: Path,
    qemu_listener: socket.socket,
    config: Path,
    digest: str,
    mediator_ready: dict[str, object],
    key: bytes,
    incarnation: str,
    ingress_listeners: list[socket.socket],
    *,
    vm_id: str = VM_ID,
    generation: str = "g1",
    ingress_ids: tuple[str, ...] = ("dev-server", "oauth-callback"),
) -> subprocess.Popen[str]:
    key_read, key_write = os.pipe()
    token_read, token_write = os.pipe()
    lifetime_read, lifetime_write = os.pipe()
    os.write(key_write, key)
    os.write(token_write, os.urandom(32))
    os.close(key_write)
    os.close(token_write)
    try:
        if config.parent not in DNS_CONTROLLERS:
            journal_config = gateway_runtime.parse_config({"network": {}})
            assert journal_config is not None
            journal_owner = gateway_runtime.GatewayController(
                journal_config, vm_id, generation=generation
            )
            journal_owner.directory = config.parent
            DNS_CONTROLLERS[config.parent] = journal_owner
        dns_fd = DNS_CONTROLLERS[config.parent]._open_dns_state({"digest": digest})
    except BaseException:
        # Journal validation failed before spawn; release staging FDs and propagate.
        for fd in (key_read, token_read, lifetime_read, lifetime_write):
            os.close(fd)
        raise
    command = [
        str(gateway),
        "--socket",
        str(qemu_socket),
        "--config",
        str(config),
        "--vm-id",
        vm_id,
        "--generation",
        generation,
        "--digest",
        digest,
        "--qemu-listener-fd",
        str(qemu_listener.fileno()),
        "--incarnation-token-fd",
        str(token_read),
        "--mediator-address",
        f"{mediator_ready['host']}:{mediator_ready['port']}",
        "--capability-key-fd",
        str(key_read),
        "--worker-incarnation",
        incarnation,
        "--supervisor-lifetime-fd",
        str(lifetime_read),
        "--dns-state-fd",
        str(dns_fd),
    ]
    require(len(ingress_ids) == len(ingress_listeners), "ingress listener identity mismatch")
    for ingress_id, listener in zip(ingress_ids, ingress_listeners, strict=True):
        command.extend(("--ingress-fd", f"{vm_id}/{generation}/{ingress_id}={listener.fileno()}"))
    try:
        process = subprocess.Popen(
            command,
            pass_fds=(
                qemu_listener.fileno(),
                token_read,
                key_read,
                lifetime_read,
                dns_fd,
                *(item.fileno() for item in ingress_listeners),
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_environment(config.parent),
        )
    except BaseException:
        os.close(lifetime_write)
        raise
    finally:
        os.close(dns_fd)
        os.close(token_read)
        os.close(key_read)
        os.close(lifetime_read)
    process.sbx_lifetime_write = lifetime_write  # type: ignore[attr-defined]
    PROCESSES.append(process)
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], COMMAND_TIMEOUT)
    payload = process.stdout.readline() if readable else ""
    require(bool(payload), "worker authenticated readiness timed out")
    ready = json.loads(payload)
    require(
        ready.get("status") == "ready"
        and ready.get("vm_id") == vm_id
        and ready.get("generation") == generation
        and ready.get("digest") == digest
        and ready.get("incarnation") == incarnation
        and len(str(ready.get("incarnation_proof", ""))) == 64,
        "worker authenticated readiness mismatch",
    )
    return process


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    lifetime_write = getattr(process, "sbx_lifetime_write", None)
    if lifetime_write is not None:
        with suppress(OSError):
            os.close(lifetime_write)
        del process.sbx_lifetime_write  # type: ignore[attr-defined]
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(PROCESS_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(PROCESS_STOP_TIMEOUT)


def run_gateway_tcg() -> int:
    require(platform.system() == "Linux", "required Linux TCG test unavailable")
    qemu = tool("SBX_QEMU_BIN", "qemu-system-x86_64")
    accelerators = subprocess.check_output(
        [qemu, "-accel", "help"], text=True, timeout=COMMAND_TIMEOUT
    )
    require("tcg" in accelerators.split(), "QEMU TCG accelerator is required")

    gateway = Path(
        tool(
            "SBX_GATEWAY_BIN",
            str(ROOT / "src/sbx/gateway_runtime/bin/sbx-gateway-linux-amd64"),
            str(GATEWAY_DIR / "sbx-gateway"),
            "sbx-gateway",
        )
    )
    manifest_path = Path(
        os.environ.get(
            "SBX_GATEWAY_TEST_IMAGE",
            Path.home() / ".smolvm/images/rootless-gateway-test/smolvm-image.json",
        )
    ).expanduser()
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
        qemu_listener = socket.socket(socket.AF_UNIX)
        qemu_listener.bind(str(qemu_socket))
        os.chmod(qemu_socket, 0o600)
        qemu_listener.listen()
        ingress_listeners = []
        for _ in range(2):
            ingress_listener = socket.socket()
            ingress_listener.bind(("127.0.0.1", 0))
            ingress_listener.listen()
            ingress_listeners.append(ingress_listener)
        for ingress_listener in ingress_listeners:
            contender = socket.socket()
            try:
                try:
                    contender.bind(ingress_listener.getsockname())
                except OSError:
                    pass
                else:
                    raise SystemExit("controller-released port 0 ingress reservation")
            finally:
                contender.close()
        raw.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vm_id": VM_ID,
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
                            "paths": ["/pypi/repo"],
                        },
                        {
                            "id": "public-ca-install",
                            "protocol": "tcp",
                            "host": "localhost.localdomain",
                            "ports": [ca_port],
                            "allow_ranges": ["127.0.0.0/8"],
                        },
                    ],
                    "ingress": [
                        {
                            "id": "dev-server",
                            "listen_host": "127.0.0.1",
                            "host_port": 0,
                            "guest_address": "192.0.2.2",
                            "guest_port": 3000,
                        },
                        {
                            "id": "oauth-callback",
                            "listen_host": "127.0.0.1",
                            "host_port": 0,
                            "guest_address": "192.0.2.2",
                            "guest_port": 4180,
                        },
                    ],
                    "quotas": {
                        "tcp_flows": 512,
                        "flows_per_destination": 64,
                        "ingress_flows": 64,
                        "dns_mappings": 1024,
                        "open_files": 4096,
                    },
                }
            )
        )
        prepared = subprocess.check_output(
            [str(gateway), "--prepare", "--config", str(raw)],
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        config.write_text(prepared)
        config.chmod(0o600)
        digest = json.loads(prepared)["digest"]
        worker = start_worker(
            gateway,
            qemu_socket,
            qemu_listener,
            config,
            digest,
            mediator_ready,
            key,
            "worker1",
            ingress_listeners,
        )
        require(qemu_socket.is_socket(), "controller QEMU socket disappeared")

        boot_args = manifest.get(
            "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
        ) + (
            " smolvm.network=guest"
            f" sbx.package_port={package.server_port}"
            f" sbx.gateway_ca_port={ca_port}"
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
            boot_args,
            "-drive",
            f"file={rootfs},if=none,format=raw,id=rootdisk",
            "-netdev",
            f"stream,id=net0,server=off,reconnect-ms=100,addr.type=unix,addr.path={qemu_socket}",
            "-device",
            "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56",
            "-device",
            "virtio-blk-device,drive=rootdisk",
        ]
        joined = " ".join(command)
        require(
            "-snapshot" in command and "accel=tcg" in joined and "stream,id=net0" in joined,
            "unsafe QEMU command",
        )
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
            env=child_environment(runtime),
        )
        PROCESSES.append(qemu_process)
        transcript: list[str] = []
        deadline = time.monotonic() + INTEGRATION_TIMEOUT
        selector = selectors.DefaultSelector()
        assert qemu_process.stdout is not None
        os.set_blocking(qemu_process.stdout.fileno(), False)
        selector.register(qemu_process.stdout, selectors.EVENT_READ)
        ingress_complete = False
        initial_complete = False
        recovery_ingress_complete = False
        old_logs = ""
        forbidden_secrets = (
            FAKE_TOKEN.encode(),
            base64.b64encode(f"aws:{FAKE_TOKEN}".encode()),
        )
        while time.monotonic() < deadline:
            returncode = qemu_process.poll()
            if returncode is not None:
                with suppress(BlockingIOError):
                    transcript.append(
                        os.read(qemu_process.stdout.fileno(), 65536).decode(errors="replace")
                    )
                raise SystemExit(
                    f"QEMU exited before test completion ({returncode}): "
                    f"{''.join(transcript)[-4000:]}"
                )
            if not selector.select(timeout=1):
                continue
            chunk = os.read(qemu_process.stdout.fileno(), 65536).decode(errors="replace")
            require(
                sum(len(value.encode()) for value in transcript) + len(chunk.encode())
                <= MAX_TRANSCRIPT_BYTES,
                "QEMU transcript exceeded its bounded log limit",
            )
            transcript.append(chunk)
            output = "".join(transcript)
            if "GATEWAY_INGRESS_READY" in output and not ingress_complete:
                workloads = (
                    (0, "/dev", b"guest-dev-server"),
                    (1, "/oauth/callback?code=visible-to-guest", b"oauth-ok"),
                )
                for index, path, expected in workloads:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", ingress_listeners[index].getsockname()[1], timeout=10
                    )
                    connection.request("GET", path)
                    response = connection.getresponse()
                    require(
                        response.status == 200 and response.read() == expected,
                        f"ingress workload failed: {path}",
                    )
                    connection.close()
                ingress_complete = True
                transcript.append(
                    "GATEWAY_DEV_SERVER_PASS GATEWAY_OAUTH_CALLBACK_PASS GATEWAY_PORT0_PASS\n"
                )
            if "GATEWAY_TEST_PASS" in output and not initial_complete:
                require(ingress_complete, "guest completed without ingress workloads")
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
                protected_metadata = 0
                for process in (worker, mediator, qemu_process):
                    for name in ("cmdline", "environ"):
                        try:
                            process_material += Path(f"/proc/{process.pid}/{name}").read_bytes()
                        except PermissionError:
                            protected_metadata += 1
                require(protected_metadata >= 1, "hardened process metadata remained readable")
                state_material = b"".join(
                    path.read_bytes() for path in runtime.rglob("*") if path.is_file()
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
                for ingress_listener in ingress_listeners:
                    probe = socket.create_connection(ingress_listener.getsockname(), timeout=1)
                    probe.settimeout(0.2)
                    probe.sendall(b"GET /dev HTTP/1.0\r\n\r\n")
                    try:
                        require(not probe.recv(1), "worker loss left ingress forwarding")
                    except TimeoutError:
                        pass
                    finally:
                        probe.close()
                worker_stdout, _ = worker.communicate(timeout=PROCESS_STOP_TIMEOUT)
                require(qemu_process.poll() is None, "QEMU did not survive worker loss")
                terminate(mediator)
                mediator_stdout, mediator_stderr = mediator.communicate(
                    timeout=PROCESS_STOP_TIMEOUT
                )
                old_logs = mediator_stdout + mediator_stderr + worker_stdout
                transcript.append("GATEWAY_INGRESS_WORKER_LOSS_PASS GATEWAY_WORKER_LOSS_PASS\n")

                replacement_key = os.urandom(32)
                mediator, mediator_ready, _ = start_mediator(
                    runtime, upstream_ca, package.server_port, replacement_key, "worker2"
                )
                worker = start_worker(
                    gateway,
                    qemu_socket,
                    qemu_listener,
                    config,
                    digest,
                    mediator_ready,
                    replacement_key,
                    "worker2",
                    ingress_listeners,
                )
                assert worker.stdout is not None
                readable, _, _ = select.select([worker.stdout], [], [], COMMAND_TIMEOUT)
                network_ready = worker.stdout.readline() if readable else ""
                require(
                    json.loads(network_ready).get("status") == "network-ready",
                    "replacement worker network readiness timed out",
                )
                transcript.append("GATEWAY_AUTHENTICATED_REPLACEMENT_READY\n")
                initial_complete = True

            if "GATEWAY_RECOVERY_INGRESS_READY" in output and not recovery_ingress_complete:
                retry_deadline = time.monotonic() + COMMAND_TIMEOUT
                while time.monotonic() < retry_deadline:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", ingress_listeners[0].getsockname()[1], timeout=1
                    )
                    try:
                        connection.request("GET", "/recovered")
                        response = connection.getresponse()
                        if response.status == 200 and response.read() == b"recovered-ingress":
                            recovery_ingress_complete = True
                            break
                    except OSError:
                        time.sleep(0.1)
                    finally:
                        connection.close()
                if not recovery_ingress_complete:
                    if worker.stdout is not None and select.select([worker.stdout], [], [], 0)[0]:
                        print(os.read(worker.stdout.fileno(), 65536), file=sys.stderr)
                    print("".join(transcript), end="", file=sys.stderr)
                    raise SystemExit("replacement ingress flow failed")
                transcript.append("GATEWAY_RECOVERY_INGRESS_PASS\n")

            if "GATEWAY_RECOVERY_PASS" in output:
                require(initial_complete, "guest reported recovery before replacement")
                require(recovery_ingress_complete, "guest recovery omitted new ingress flow")
                terminate(worker)
                terminate(mediator)
                worker_stdout, _ = worker.communicate(timeout=PROCESS_STOP_TIMEOUT)
                mediator_stdout, mediator_stderr = mediator.communicate(
                    timeout=PROCESS_STOP_TIMEOUT
                )
                logs = (
                    old_logs + mediator_stdout + mediator_stderr + worker_stdout + output
                ).encode()
                for secret in (*forbidden_secrets, PLACEHOLDER.encode()):
                    require(secret not in logs, "secret leaked into host or guest logs")
                transcript.append(
                    "GATEWAY_NEW_FLOW_PASS GATEWAY_OLD_FLOW_DISCARD_PASS "
                    "GATEWAY_FAKE_TOKEN_SCAN_PASS\n"
                )
                stop.set()
                listener.close()
                ca_listener.close()
                qemu_listener.close()
                for ingress_listener in ingress_listeners:
                    ingress_listener.close()
                print("".join(transcript), end="")
                return 0
        print("".join(transcript), end="", file=sys.stderr)
        raise SystemExit("TCG gateway test timed out")
    return 1


def cleanup() -> None:
    for fd in LIFETIME_FDS:
        with suppress(OSError):
            os.close(fd)
    LIFETIME_FDS.clear()
    for process in reversed(PROCESSES):
        terminate(process)
    PROCESSES.clear()
    audit_failed = False
    for controller in AUDIT_CONTROLLERS.values():
        for thread in controller._log_threads:
            thread.join(timeout=PROCESS_STOP_TIMEOUT)
            audit_failed |= thread.is_alive()
        for stream in controller._logs:
            stream.close()
        audit_failed |= controller._audit_failure is not None
    AUDIT_CONTROLLERS.clear()
    DNS_CONTROLLERS.clear()
    for server in SERVERS:
        server.shutdown()
        server.server_close()
    SERVERS.clear()
    require(not audit_failed, "TCG audit persistence or cleanup failed")


def test_gateway_tcg() -> None:
    try:
        assert run_gateway_tcg() == 0
    finally:
        cleanup()
