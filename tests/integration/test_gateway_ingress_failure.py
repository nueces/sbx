"""A real ingress accept failure must terminate the worker, without QEMU."""

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from test_gateway_tcg import (
    COMMAND_TIMEOUT,
    PROCESS_STOP_TIMEOUT,
    cleanup,
    start_worker,
    tool,
    worker_readiness,
)

from sbx import gateway


def test_ingress_accept_failure_exits_worker() -> None:
    assert sys.platform == "linux", "this acceptance test requires Linux"
    binary = Path(
        tool(
            "SBX_GATEWAY_BIN",
            str(gateway.GATEWAY_WORKER),
            str(gateway.GATEWAY_DEV_WORKER),
            "sbx-gateway",
        )
    )
    with (
        tempfile.TemporaryDirectory(prefix="gw-accept-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as qemu_listener,
        socket.socket() as ingress,
        socket.socket(socket.AF_UNIX) as peer,
    ):
        runtime = Path(directory)
        qemu_socket = runtime / "qemu.sock"
        qemu_listener.bind(str(qemu_socket))
        qemu_socket.chmod(0o600)
        qemu_listener.listen()
        ingress.bind(("127.0.0.1", 0))
        ingress.listen()
        config = runtime / "config.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vm_id": "ingress-accept-failure",
                    "generation": "g1",
                    "rules": [],
                    "ingress": [
                        {
                            "id": "ssh",
                            "listen_host": "127.0.0.1",
                            "host_port": ingress.getsockname()[1],
                            "guest_address": "192.0.2.2",
                            "guest_port": 22,
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
            )
        )
        config.chmod(0o600)
        prepared = subprocess.check_output(
            [str(binary), "--prepare", "--config", str(config)],
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        digest = json.loads(prepared)["digest"]
        config.write_text(prepared)
        worker = None
        try:
            worker = start_worker(
                binary,
                qemu_socket,
                qemu_listener,
                config,
                digest,
                {"host": "127.0.0.1", "port": 9},
                os.urandom(32),
                "worker1",
                [ingress],
                vm_id="ingress-accept-failure",
                ingress_ids=("ssh",),
            )
            peer.settimeout(COMMAND_TIMEOUT)
            peer.connect(str(qemu_socket))
            # Guest ARP request for the gateway, with QEMU's big-endian frame length.
            frame = bytes.fromhex(
                "ffffffffffff5254001234020806"
                "0001080006040001525400123402c0000202000000000000c0000201"
            )
            peer.sendall(len(frame).to_bytes(4, "big") + frame)
            assert worker_readiness(worker)["status"] == "network-ready"
            assert worker.poll() is None
            # Both descriptors share the kernel listener; this breaks the worker's Accept.
            ingress.shutdown(socket.SHUT_RDWR)
            assert worker.wait(timeout=PROCESS_STOP_TIMEOUT) == 1
        finally:
            cleanup()
            if worker is not None:
                output, _ = worker.communicate(timeout=PROCESS_STOP_TIMEOUT)
                print(output)
