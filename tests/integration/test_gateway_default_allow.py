"""Default-allow acceptance: isolated namespace origins, production worker/mediator, real QEMU."""

import hashlib
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_gateway_tcg import (
    AUDIT_CONTROLLERS,
    FAKE_TOKEN,
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
from test_gateway_direct_ip import Console, archive_and_remove

ROOT = Path(__file__).resolve().parents[2]
PUBLIC, PRIVATE = "93.184.216.34", "10.20.30.40"
PROTECTED = ("169.254.169.254", "100.100.100.200")
EVIDENCE = "default-allow-evidence.tar.gz"
VM = "default-allow"
HOSTS = f"""127.0.0.1 localhost
{PUBLIC} api.example open.example git.example
{PRIVATE} lan.example
169.254.169.254 meta.example
"""


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.records.append(
            (self.command, self.path, self.headers.get("Host"), self.headers.get("Authorization"))
        )
        body = b"default-allow-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        pass


def origin(runtime, name, *, tls=False):
    server = ThreadingHTTPServer(("0.0.0.0", 0), Origin)
    server.daemon_threads = True
    server.records = []
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
                "/CN=default-allow-test",
                "-addext",
                "subjectAltName=DNS:api.example",
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
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ca


def nxdomain_server(stop):
    """Answer every host-side DNS query with NXDOMAIN; /etc/hosts supplies fixture names."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 53))
    server.settimeout(0.2)

    def serve():
        while not stop.is_set():
            try:
                packet, peer = server.recvfrom(1232)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(packet) >= 12:
                reply = bytearray(packet)
                reply[2] = 0x81 | (packet[2] & 0x01)
                reply[3] = 0x83
                reply[6:12] = b"\x00" * 6
                with suppress(OSError):
                    server.sendto(bytes(reply), peer)

    threading.Thread(target=serve, daemon=True).start()
    return server


def journal_hosts(path):
    lines = path.read_text().splitlines()[1:]
    return sorted(json.loads(line)["host"] for line in lines if line.strip())


def boot_guest(runtime, image, qemu_socket, share):
    manifest = json.loads(image.read_text())
    kernel = (
        image.parent / manifest["kernel"] if "kernel" in manifest else Path(manifest["kernel_path"])
    )
    rootfs = (
        image.parent / manifest["rootfs"] if "rootfs" in manifest else Path(manifest["rootfs_path"])
    )
    (runtime / "image-reference.json").write_text(
        json.dumps(
            {"manifest": str(image), "rootfs": str(rootfs), "rootfs_size": rootfs.stat().st_size}
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
        "virtio-9p-pci,fsdev=share,mount_tag=defaultallow",
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
    return qemu


def exercise(destination, parent_namespace, image, worker):
    assert os.getuid() != 0 and os.geteuid() == os.getuid()
    assert os.readlink("/proc/self/ns/net") != parent_namespace
    assert Path("/etc/hosts").read_text() == HOSTS, "fixture hosts file not mounted"
    runtime = Path(tempfile.mkdtemp(prefix="gw-allow-"))
    capture = DiagnosticCapture(runtime)
    stop = threading.Event()
    servers, console, listener, dns = [], None, None, None
    echo = socket.socket()
    echo.bind(("0.0.0.0", 0))
    echo.listen()
    threading.Thread(target=echo_server, args=(echo, stop), daemon=True).start()
    try:
        dns = nxdomain_server(stop)
        plain, _ = origin(runtime, "plain")
        servers.append(plain)
        secure, secure_ca = origin(runtime, "secure", tls=True)
        servers.append(secure)
        echo_port = echo.getsockname()[1]
        config = gateway.parse_config(
            {
                "network": {
                    "default": "allow",
                    "policy": [
                        {
                            "id": "api",
                            "mode": "http-mediated",
                            "hosts": ["api.example"],
                            "ports": [secure.server_port],
                            "methods": ["GET"],
                            "path_prefixes": ["/v1/"],
                        },
                        {
                            "id": "git",
                            "mode": "tcp-forward",
                            "hosts": ["git.example"],
                            "ports": [9],
                        },
                    ],
                    "credential": [
                        {
                            "id": "api-token",
                            "environment": "API_TOKEN",
                            "method": "command",
                            "options": {"argv": ["unused-fixture-provider"]},
                            "bindings": [
                                {
                                    "policies": ["api"],
                                    "header": "Authorization: Bearer {placeholder}",
                                }
                            ],
                        }
                    ],
                }
            },
            platform="linux",
        )
        assert config is not None and config.default == "allow"
        raw = runtime / "raw.json"
        worker_config = config.worker_config(VM, "g1")
        assert worker_config["default"] == "allow"
        raw.write_text(json.dumps(worker_config))
        prepared = subprocess.check_output(
            [str(worker), "--prepare", "--config", str(raw)], text=True, timeout=10
        )
        prepared_config = json.loads(prepared)
        assert prepared_config["default"] == "allow"
        assert prepared_config["digest"] == worker_config["digest"], "Go/Python digest mismatch"
        policy_path = runtime / "policy.json"
        policy_path.write_text(prepared)
        policy_path.chmod(0o600)
        digest = prepared_config["digest"]
        mediator_policy = {
            "version": 2,
            "rules": [
                {
                    "id": "api",
                    "host": "api.example",
                    "ports": [secure.server_port],
                    "methods": ["GET"],
                    "paths": ["/v1"],
                    "hooks": [],
                }
            ],
            "credentials": [
                {
                    "credential": "api-token",
                    "rule_id": "api",
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
                secure_ca,
                secure.server_port,
                key,
                incarnation,
                VM,
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
                vm_id=VM,
                ingress_ids=(),
                capture=capture,
            )
            assert process.sbx_ready["egress_default"] == "allow"
            assert process.sbx_ready["direct_ip_routes"] == 0
            assert "landlock" in process.sbx_ready["hardening"].split(",")
            for child in (process, mediator):
                status = Path(f"/proc/{child.pid}/status").read_text()
                assert "CapEff:\t0000000000000000" in status
                assert "NoNewPrivs:\t1" in status
            return process, mediator, confdir

        process, mediator, confdir = pair("worker1")
        share = runtime / "guest"
        share.mkdir()
        shutil.copyfile(Path(__file__).with_name("default_allow_guest.py"), share / "guest.py")
        shutil.copyfile(confdir / "mitmproxy-ca-cert.pem", share / "gateway-ca.pem")
        (share / "config.json").write_text(
            json.dumps(
                {
                    "public": PUBLIC,
                    "private": PRIVATE,
                    "echo_port": echo_port,
                    "http_port": plain.server_port,
                    "tls_port": secure.server_port,
                    "placeholder": PLACEHOLDER,
                }
            )
        )
        qemu = boot_guest(runtime, image, qemu_socket, share)
        console = Console(qemu)
        console.until("job control turned off")
        console.send(
            "mount -t proc proc /proc; mount -t sysfs sysfs /sys; "
            "mount -t devtmpfs devtmpfs /dev; mount -t tmpfs tmpfs /run; "
            "mkdir -p /run/defaultallow; "
            "mount -t 9p -o trans=virtio,version=9p2000.L,ro defaultallow /run/defaultallow; "
            "ip link set eth0 up; dhclient -1 eth0; "
            "python3 /run/defaultallow/guest.py /run/defaultallow/config.json"
        )
        console.until("DEFAULT_ALLOW_INITIAL_OK", timeout=180)
        journal = runtime / "dns-mappings.jsonl"
        before = journal.read_bytes()
        assert journal_hosts(journal) == ["api.example", "git.example"], journal_hosts(journal)
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
        console.until("DEFAULT_ALLOW_RECOVERY_OK", timeout=180)
        assert journal.read_bytes() == before, "unmatched names reached the DNS journal"

        # Default route: the placeholder reached the origin unchanged, never a real token.
        assert [record[:2] for record in plain.records] == [("GET", "/raw/literal")] * 2
        assert all(record[3] == "Bearer " + PLACEHOLDER for record in plain.records)
        # Explicit policy: only allowed requests reach the origin, with the injected token.
        assert [record[:2] for record in secure.records] == [("GET", "/v1/allowed")] * 2
        assert all(record[3] == "Bearer " + FAKE_TOKEN for record in secure.records)
        assert all(FAKE_TOKEN not in str(record) for record in plain.records)
        (runtime / "results.json").write_text(
            json.dumps(
                {
                    "uid": os.getuid(),
                    "namespace": os.readlink("/proc/self/ns/net"),
                    "egress_default": process.sbx_ready["egress_default"],
                    "hardening": process.sbx_ready["hardening"],
                    "recovery": True,
                    "default_route_http": [record[:3] for record in plain.records],
                    "mediated_https": [record[:3] for record in secure.records],
                    "dns_journal_hosts": journal_hosts(journal),
                    "dns_sha256": hashlib.sha256(before).hexdigest(),
                    "diagnostic_files": sorted(path.name for path in capture.paths),
                    "gateway_module": str(Path(gateway.__file__).resolve()),
                    "worker": str(worker),
                    "mediator_directory": str(gateway.GATEWAY_MEDIATOR_DIR),
                },
                indent=2,
            )
        )
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
            if dns is not None:
                dns.close()
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
            archive_and_remove(runtime, destination, capture, EVIDENCE)
        except BaseException:
            print(f"VM fixture cleanup failed; runtime retained at {runtime}", file=sys.stderr)
            capture.finish()
            raise


def test_default_allow_guest_with_scoped_credentials_and_replacement(tmp_path):
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
    fixture = tmp_path / "fixture-etc"
    fixture.mkdir()
    (fixture / "hosts").write_text(HOSTS)
    (fixture / "resolv.conf").write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")
    (fixture / "nsswitch.conf").write_text("hosts: files dns\n")
    # Setup is confined to fresh network and mount namespaces; the bind mounts and addresses are
    # invisible to the host. UID, groups and all capabilities are dropped before any test code.
    setup = (
        "set -eu; mount --make-rprivate /; ip link set lo up; "
        + "; ".join(
            f"ip address add {address}/32 dev lo" for address in (PUBLIC, PRIVATE, *PROTECTED)
        )
        + "; sysctl -qw net.ipv4.ip_unprivileged_port_start=53; "
        + "; ".join(
            f"mount --bind {fixture / name} /etc/{name}"
            for name in ("hosts", "resolv.conf", "nsswitch.conf")
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
        "--mount",
        "--",
        "/bin/sh",
        "-c",
        setup,
        "default-allow-fixture",
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
        output, _ = process.communicate(timeout=480)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["sudo", "-n", "kill", "-TERM", "--", f"-{process.pid}"], timeout=10, check=False
        )
        output, _ = process.communicate(timeout=30)
        raise AssertionError(
            f"default-allow namespace test timed out: {output[-6000:]!r}"
        ) from None
    (tmp_path / "namespace-driver.log").write_bytes(output)
    assert process.returncode == 0, output[-8000:].decode(errors="replace")
    assert (tmp_path / EVIDENCE).is_file()


if __name__ == "__main__":
    assert sys.argv[1] == "--inside"

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt("namespace fixture interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    exercise(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
