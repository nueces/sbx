"""Default-allow acceptance: isolated namespace origins, production worker/mediator, real QEMU."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from test_gateway_tcg import (
    FAKE_TOKEN,
    PLACEHOLDER,
    DiagnosticCapture,
    echo_server,
)

from sbx import gateway
from test_gateway_direct_ip import (
    boot_guest,
    namespace_main,
    origin,
    run_namespace_fixture,
    start_pair,
    stop_pair,
    teardown,
)

PUBLIC, PRIVATE = "93.184.216.34", "10.20.30.40"
PROTECTED = ("169.254.169.254", "100.100.100.200")
EVIDENCE = "default-allow-evidence.tar.gz"
BODY = b"default-allow-ok"
VM = "default-allow"
HOSTS = f"""127.0.0.1 localhost
{PUBLIC} api.example open.example git.example
{PRIVATE} lan.example
169.254.169.254 meta.example
"""


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
        plain, _ = origin(runtime, "plain", body=BODY)
        servers.append(plain)
        secure, secure_ca = origin(runtime, "secure", body=BODY, tls=True, san="DNS:api.example")
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
            process, mediator, confdir = start_pair(
                incarnation,
                runtime=runtime,
                worker=worker,
                qemu_socket=qemu_socket,
                listener=listener,
                policy_path=policy_path,
                digest=digest,
                key=key,
                vm_id=VM,
                trust=secure_ca,
                upstream_port=secure.server_port,
                mediator_policy=mediator_policy,
                capture=capture,
            )
            assert process.sbx_ready["egress_default"] == "allow"
            assert process.sbx_ready["direct_ip_routes"] == 0
            assert "landlock" in process.sbx_ready["hardening"].split(",")
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
        console = boot_guest(runtime, image, qemu_socket, share, "defaultallow")
        console.until("DEFAULT_ALLOW_INITIAL_OK", timeout=180)
        journal = runtime / "dns-mappings.jsonl"
        before = journal.read_bytes()
        assert journal_hosts(journal) == ["api.example", "git.example"], journal_hosts(journal)
        stop_pair(process, mediator, confdir, capture)
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
        stop.set()
        teardown(
            runtime,
            destination,
            capture,
            console,
            closers=[echo, dns, listener],
            servers=servers,
            evidence=EVIDENCE,
        )


def test_default_allow_guest_with_scoped_credentials_and_replacement(tmp_path):
    fixture = tmp_path / "fixture-etc"
    fixture.mkdir()
    (fixture / "hosts").write_text(HOSTS)
    (fixture / "resolv.conf").write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")
    (fixture / "nsswitch.conf").write_text("hosts: files dns\n")
    # Setup is confined to fresh network and mount namespaces; the bind mounts and addresses are
    # invisible to the host. UID, groups and all capabilities are dropped before any test code.
    run_namespace_fixture(
        tmp_path,
        label="default-allow",
        setup="set -eu; mount --make-rprivate /; ip link set lo up; "
        + "; ".join(
            f"ip address add {address}/32 dev lo" for address in (PUBLIC, PRIVATE, *PROTECTED)
        )
        + "; sysctl -qw net.ipv4.ip_unprivileged_port_start=53; "
        + "; ".join(
            f"mount --bind {fixture / name} /etc/{name}"
            for name in ("hosts", "resolv.conf", "nsswitch.conf")
        ),
        unshare_flags=("--net", "--mount"),
        timeout=480,
        evidence=EVIDENCE,
        script=Path(__file__).resolve(),
    )


if __name__ == "__main__":
    namespace_main(exercise)
