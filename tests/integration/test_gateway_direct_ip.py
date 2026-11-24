"""Direct IPv4 acceptance: production address checks, isolated origins and real QEMU."""

import hashlib
import json
import os
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_gateway_tcg import (
    AUDIT_CONTROLLERS,
    LIFETIME_FDS,
    PLACEHOLDER,
    PROCESSES,
    DiagnosticCapture,
    child_environment,
    echo_server,
    start_mediator,
    start_worker,
    terminate,
    tool,
)

from sbx import gateway

ROOT = Path(__file__).resolve().parents[2]
PUBLIC, OTHER_PUBLIC = "93.184.216.34", "93.184.216.35"
PRIVATE, OTHER_PRIVATE = "10.20.30.40", "10.20.30.41"


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.records.append((self.path, self.headers.get("Host")))
        body = b"direct-ip-ok" * 4096
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        pass


def origin(runtime, name, *, tls=False, wrong_ip=False):
    server = ThreadingHTTPServer(("0.0.0.0", 0), Origin)
    server.daemon_threads = True
    server.records, server.snis = [], []
    ca = None
    if tls:
        ca, key = runtime / f"{name}.pem", runtime / f"{name}-key.pem"
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
                "/CN=direct-ip-test",
                "-addext",
                f"subjectAltName=IP:{OTHER_PUBLIC if wrong_ip else PUBLIC}",
                "-keyout",
                str(key),
                "-out",
                str(ca),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca, key)
        context.set_servername_callback(lambda _conn, sni, _ctx: server.snis.append(sni))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ca


class Console:
    def __init__(self, process):
        self.process = process
        self.data = bytearray()
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        os.set_blocking(process.stdout.fileno(), False)

    def until(self, marker, timeout=120):
        deadline = time.monotonic() + timeout
        encoded = marker.encode()
        while encoded not in self.data:
            assert time.monotonic() < deadline, (
                f"guest timed out waiting for {marker}: {self.data[-3000:]!r}"
            )
            assert self.process.poll() is None, f"guest exited: {self.data[-3000:]!r}"
            if self.selector.select(0.1):
                self.data.extend(os.read(self.process.stdout.fileno(), 65536))
            assert len(self.data) <= 1024 * 1024, "guest console exceeded bound"

    def drain(self):
        while chunk := os.read(self.process.stdout.fileno(), 65536):
            self.data.extend(chunk)
            assert len(self.data) <= 1024 * 1024, "guest console exceeded bound"

    def send(self, text):
        self.process.stdin.write(text.encode() + b"\n")
        self.process.stdin.flush()


def archive(runtime, destination, required=(), name="direct-ip-evidence.tar.gz"):
    """Archive stopped disks/state before deleting anything, verifying each regular member."""
    paths = [p for p in runtime.rglob("*") if p.is_file() and not p.is_symlink()]
    assert set(required) <= set(paths), "missing required diagnostic capture files"
    hashes = {}
    for path in paths:
        with path.open("rb") as source:
            hashes[str(path.relative_to(runtime))] = hashlib.file_digest(
                source, "sha256"
            ).hexdigest()
    target = destination / name
    with tarfile.open(target, "w:gz") as bundle:
        for path in paths:
            bundle.add(path, arcname=str(path.relative_to(runtime)), recursive=False)
    target.chmod(0o600)
    with tarfile.open(target) as bundle:
        assert set(bundle.getnames()) == set(hashes)
        for member in bundle:
            with bundle.extractfile(member) as source:
                assert hashlib.file_digest(source, "sha256").hexdigest() == hashes[member.name], (
                    "archive member verification failed"
                )
    (destination / "archive-members.json").write_text(json.dumps(hashes, indent=2))
    assert hashes


def archive_and_remove(runtime, destination, capture, name="direct-ip-evidence.tar.gz"):
    try:
        capture.finish()
        archive(runtime, destination, capture.paths, name)
    except BaseException:
        print(f"evidence incomplete; runtime retained at {runtime}", file=sys.stderr)
        raise
    shutil.rmtree(runtime)


def exercise(destination, parent_namespace, image, worker):
    assert os.getuid() != 0 and os.geteuid() == os.getuid()
    assert os.readlink("/proc/self/ns/net") != parent_namespace
    runtime = Path(tempfile.mkdtemp(prefix="gw-ip-"))
    capture = DiagnosticCapture(runtime)
    stop = threading.Event()
    servers, console, qemu, listener = [], None, None, None
    echo = socket.socket()
    echo.bind(("0.0.0.0", 0))
    echo.listen()
    threading.Thread(target=echo_server, args=(echo, stop), daemon=True).start()
    try:
        plain, _ = origin(runtime, "plain")
        servers.append(plain)
        secure, secure_ca = origin(runtime, "secure", tls=True)
        servers.append(secure)
        wrong, wrong_ca = origin(runtime, "wrong", tls=True, wrong_ip=True)
        servers.append(wrong)
        denied, _ = origin(runtime, "denied")
        servers.append(denied)
        trust = runtime / "upstream-trust.pem"
        trust.write_bytes(secure_ca.read_bytes() + wrong_ca.read_bytes())
        ports = [plain.server_port, secure.server_port, wrong.server_port]
        echo_port = echo.getsockname()[1]
        config = gateway.parse_config(
            {
                "network": {
                    "policy": [
                        {
                            "id": "ip-http",
                            "mode": "http-mediated",
                            "hosts": [PUBLIC],
                            "ports": ports,
                            "methods": ["GET"],
                            "path_prefixes": ["/ip/"],
                        },
                        {
                            "id": "ip-echo",
                            "mode": "tcp-forward",
                            "hosts": [PUBLIC],
                            "ports": [echo_port],
                        },
                        {
                            "id": "private-echo",
                            "mode": "tcp-forward",
                            "hosts": [PRIVATE],
                            "ports": [echo_port],
                            "allow_ranges": ["10.20.30.0/24"],
                        },
                        {
                            "id": "cached",
                            "mode": "tcp-forward",
                            "hosts": ["cached.example"],
                            "ports": [9],
                        },
                        {
                            "id": "dns-auth",
                            "mode": "http-mediated",
                            "hosts": ["auth.example"],
                            "ports": [443],
                        },
                    ]
                }
            }
        )
        assert config is not None
        raw = runtime / "raw.json"
        raw.write_text(json.dumps(config.worker_config("direct-ip", "g1")))
        prepared = subprocess.check_output(
            [str(worker), "--prepare", "--config", str(raw)], text=True, timeout=10
        )
        policy_path = runtime / "policy.json"
        policy_path.write_text(prepared)
        policy_path.chmod(0o600)
        digest = json.loads(prepared)["digest"]
        mediator_policy = {
            "version": 2,
            "rules": [
                {
                    "id": "ip-http",
                    "host": PUBLIC,
                    "ports": ports,
                    "methods": ["GET"],
                    "paths": ["/ip"],
                    "hooks": [],
                },
                {
                    "id": "dns-auth",
                    "host": "auth.example",
                    "ports": [443],
                    "methods": ["GET"],
                    "paths": [],
                    "hooks": [],
                },
            ],
            "credentials": [
                {
                    "credential": "dns-token",
                    "rule_id": "dns-auth",
                    "methods": [],
                    "paths": [],
                    "header": {
                        "name": "Authorization",
                        "format": "template",
                        "value": "Bearer {placeholder}",
                    },
                    "placeholder": PLACEHOLDER,
                }
            ],
        }
        key = os.urandom(32)
        listener = socket.socket(socket.AF_UNIX)
        qemu_socket = runtime / "qemu.sock"
        listener.bind(str(qemu_socket))
        qemu_socket.chmod(0o600)
        listener.listen()

        def pair(incarnation):
            mediator, ready, confdir = start_mediator(
                runtime,
                trust,
                secure.server_port,
                key,
                incarnation,
                "direct-ip",
                policy=mediator_policy,
                allow_fixture_loopback=False,
                mediator_dir=gateway.GATEWAY_MEDIATOR_DIR,
                capture=capture,
            )
            process = start_worker(
                worker,
                qemu_socket,
                listener,
                policy_path,
                digest,
                ready,
                key,
                incarnation,
                [],
                vm_id="direct-ip",
                ingress_ids=(),
                capture=capture,
            )
            assert process.sbx_ready["direct_ip_routes"] == 2
            for child in (process, mediator):
                status = Path(f"/proc/{child.pid}/status").read_text()
                assert "CapEff:\t0000000000000000" in status
                assert "NoNewPrivs:\t1" in status
            return process, mediator, confdir

        process, mediator, confdir = pair("worker1")
        share = runtime / "guest"
        share.mkdir()
        shutil.copyfile(Path(__file__).with_name("direct_ip_guest.py"), share / "guest.py")
        shutil.copyfile(confdir / "mitmproxy-ca-cert.pem", share / "gateway-ca.pem")
        (share / "config.json").write_text(
            json.dumps(
                {
                    "public": PUBLIC,
                    "other_public": OTHER_PUBLIC,
                    "private": PRIVATE,
                    "other_private": OTHER_PRIVATE,
                    "echo_port": echo_port,
                    "http_port": plain.server_port,
                    "tls_port": secure.server_port,
                    "bad_tls_port": wrong.server_port,
                    "denied_port": denied.server_port,
                }
            )
        )
        manifest = json.loads(image.read_text())
        kernel = (
            image.parent / manifest["kernel"]
            if "kernel" in manifest
            else Path(manifest["kernel_path"])
        )
        rootfs = (
            image.parent / manifest["rootfs"]
            if "rootfs" in manifest
            else Path(manifest["rootfs_path"])
        )
        (runtime / "image-reference.json").write_text(
            json.dumps(
                {
                    "manifest": str(image),
                    "rootfs": str(rootfs),
                    "rootfs_size": rootfs.stat().st_size,
                }
            )
        )
        overlay = runtime / "guest.qcow2"
        subprocess.run(
            [
                tool("SBX_QEMU_IMG_BIN", "qemu-img"),
                "create",
                "-f",
                "qcow2",
                "-F",
                "raw",
                "-b",
                str(rootfs),
                str(overlay),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=10,
        )
        command = [
            tool("SBX_QEMU_BIN", "qemu-system-x86_64"),
            "-sandbox",
            "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-nodefaults",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-nographic",
            "-no-reboot",
            "-machine",
            "q35,accel=tcg",
            "-cpu",
            "max",
            "-smp",
            "1",
            "-m",
            "512",
            "-kernel",
            str(kernel),
            "-append",
            "console=ttyS0 reboot=k panic=1 root=/dev/vda rw init=/bin/sh",
            "-drive",
            f"file={overlay},if=none,format=qcow2,id=rootdisk",
            "-device",
            "virtio-blk-pci,drive=rootdisk",
            "-netdev",
            f"stream,id=net0,server=off,reconnect-ms=100,addr.type=unix,addr.path={qemu_socket}",
            "-device",
            "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56",
            "-fsdev",
            f"local,id=share,path={share},security_model=none,readonly=on",
            "-device",
            "virtio-9p-pci,fsdev=share,mount_tag=directip",
        ]
        (runtime / "qemu-command.json").write_text(json.dumps(command))
        qemu = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_environment(runtime),
        )
        PROCESSES.append(qemu)
        console = Console(qemu)
        console.until("job control turned off")
        console.send(
            "mount -t proc proc /proc; mount -t sysfs sysfs /sys; "
            "mount -t devtmpfs devtmpfs /dev; mount -t tmpfs tmpfs /run; "
            "mkdir -p /run/directip; "
            "mount -t 9p -o trans=virtio,version=9p2000.L,ro directip /run/directip; "
            "ip link set eth0 up; dhclient -1 eth0; "
            "python3 /run/directip/guest.py /run/directip/config.json"
        )
        console.until("DIRECT_IP_INITIAL_OK")
        journal = runtime / "dns-mappings.jsonl"
        before = journal.read_bytes()
        terminate(process)
        terminate(mediator)
        capture.finish(process)
        capture.finish(mediator)
        for thread in AUDIT_CONTROLLERS[confdir]._log_threads:
            thread.join(5)
            assert not thread.is_alive(), "audit drain unfinished before replacement"
        process, mediator, _ = pair("worker2")
        assert journal.read_bytes() == before
        console.send("continue")
        console.until("DIRECT_IP_RECOVERY_OK", timeout=60)
        assert journal.read_bytes() == before
        assert [path for path, _ in plain.records] == ["/ip/plain", "/ip/plain"]
        assert [path for path, _ in secure.records] == ["/ip/tls", "/ip/tls"]
        assert not wrong.records and not denied.records
        assert secure.snis and set(secure.snis) == {None}
        (runtime / "results.json").write_text(
            json.dumps(
                {
                    "uid": os.getuid(),
                    "namespace": os.readlink("/proc/self/ns/net"),
                    "direct_ip_routes": process.sbx_ready["direct_ip_routes"],
                    "recovery": True,
                    "http": plain.records,
                    "https": secure.records,
                    "wire_sni": secure.snis,
                    "dns_sha256": hashlib.sha256(before).hexdigest(),
                    "diagnostic_files": sorted(path.name for path in capture.paths),
                    "gateway_module": str(Path(gateway.__file__).resolve()),
                    "worker": str(worker),
                    "mediator_directory": str(gateway.GATEWAY_MEDIATOR_DIR),
                },
                indent=2,
            )
        )
        assert len(capture.paths) == 6, "missing incarnation diagnostics"
    finally:
        try:
            stop.set()
            for child in reversed(PROCESSES):
                terminate(child)
            for fd in LIFETIME_FDS:
                with suppress(OSError):
                    os.close(fd)
            for controller in AUDIT_CONTROLLERS.values():
                for thread in controller._log_threads:
                    thread.join(5)
                    assert not thread.is_alive(), "audit drain unfinished"
                if controller._audit_stream is not None:
                    controller._audit_stream.close()
            echo.close()
            if listener is not None:
                listener.close()
            for server in servers:
                server.shutdown()
                server.server_close()
            if console is not None:
                try:
                    console.drain()
                finally:
                    (runtime / "guest-console.log").write_bytes(console.data)
                    console.selector.close()
            # Neither partial capture nor a failed archive can authorize removal.
            archive_and_remove(runtime, destination, capture)
        except BaseException:
            print(f"VM fixture cleanup failed; runtime retained at {runtime}", file=sys.stderr)
            capture.finish()
            raise


def test_direct_ip_guest_without_dns_and_after_replacement(tmp_path):
    assert sys.platform == "linux" and os.getuid() != 0
    image = Path(
        os.environ.get(
            "SBX_GATEWAY_TEST_IMAGE",
            Path.home() / ".smolvm/images/rootless-gateway-test/smolvm-image.json",
        )
    ).resolve()
    assert image.is_file(), "build the required test guest first"
    worker = Path(os.environ.get("SBX_GATEWAY_BIN", gateway.GATEWAY_WORKER)).resolve()
    assert worker.is_file()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    # Setup is confined to a fresh namespace; drop UID, groups and all capabilities before tests.
    setup = (
        "set -eu; ip link set lo up; "
        + "; ".join(
            f"ip address add {address}/32 dev lo"
            for address in (PUBLIC, OTHER_PUBLIC, PRIVATE, OTHER_PRIVATE)
        )
        + '; uid=$1; gid=$2; shift 2; exec setpriv --reuid="$uid" --regid="$gid" '
        "--clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all "
        '--no-new-privs /usr/bin/env "$@"'
    )
    command = [
        "sudo",
        "-n",
        "unshare",
        "--net",
        "--",
        "/bin/sh",
        "-c",
        setup,
        "direct-ip-fixture",
        str(os.getuid()),
        str(os.getgid()),
        f"PATH={Path(sys.executable).parent}:{os.defpath}",
        f"HOME={home}",
        f"PYTHONPATH={os.environ.get('PYTHONPATH', '')}",
        sys.executable,
        str(Path(__file__).resolve()),
        "--inside",
        str(tmp_path),
        os.readlink("/proc/self/ns/net"),
        str(image),
        str(worker),
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT
    )
    try:
        output, _ = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["sudo", "-n", "kill", "-TERM", "--", f"-{process.pid}"], timeout=10, check=False
        )
        output, _ = process.communicate(timeout=30)
        raise AssertionError(f"direct-IP namespace test timed out: {output[-6000:]!r}") from None
    (tmp_path / "namespace-driver.log").write_bytes(output)
    assert process.returncode == 0, output[-8000:].decode(errors="replace")
    assert (tmp_path / "direct-ip-evidence.tar.gz").is_file()


if __name__ == "__main__":
    assert sys.argv[1] == "--inside"

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt("namespace fixture interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    exercise(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
