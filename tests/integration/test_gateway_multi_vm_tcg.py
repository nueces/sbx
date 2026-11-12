#!/usr/bin/env python3
"""Mandatory two-VM isolation gate; four-VM capacity remains an explicit deferred check."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import selectors
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

from test_gateway_tcg import (
    MAX_TRANSCRIPT_BYTES,
    PROCESSES,
    ROOT,
    child_environment,
    cleanup,
    echo_server,
    package_server,
    require,
    start_mediator,
    start_worker,
    terminate,
    tool,
)

VM_COUNT = int(os.environ.get("SBX_GATEWAY_VM_COUNT", "2"))
TIMEOUT = 300
PAIR_RSS_LIMIT = 512 * 1024 * 1024
PAIR_FD_LIMIT = 128
PAIR_PROCESS_LIMIT = 16
PAIR_THREAD_LIMIT = 256


def process_usage(pids: list[int]) -> tuple[int, int, int, int]:
    pending = list(pids)
    seen: set[int] = set()
    rss = descriptors = threads = 0
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
            descriptors += sum(1 for _ in process.joinpath("fd").iterdir())
        for task in process.joinpath("task").iterdir():
            with suppress(OSError):
                children = task.joinpath("children").read_text().split()
                pending.extend(int(value) for value in children)
    return rss, descriptors, len(seen), threads


def open_file_limit(pid: int) -> int:
    for line in (Path("/proc") / str(pid) / "limits").read_text().splitlines():
        if line.startswith("Max open files"):
            return int(line.split()[3])
    raise RuntimeError("process open-file limit is unavailable")


def ingress(port: int, vm_id: str, timeout: float = 10) -> bytes:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", f"/{vm_id}")
        response = connection.getresponse()
        body = response.read()
        require(response.status == 200, f"VM {vm_id} ingress returned {response.status}")
        return body
    finally:
        connection.close()


def run_gateway_multi_vm_tcg() -> int:
    require(platform.system() == "Linux", "required Linux multi-VM TCG test unavailable")
    require(VM_COUNT in {2, 4}, "multi-VM TCG test requires exactly 2 or 4 VMs")
    qemu = tool("SBX_QEMU_BIN", "qemu-system-x86_64")
    require(
        "tcg" in subprocess.check_output([qemu, "-accel", "help"], text=True).split(),
        "QEMU TCG accelerator is required",
    )
    gateway = Path(
        tool(
            "SBX_GATEWAY_BIN",
            str(ROOT / "src/sbx/gateway_runtime/bin/sbx-gateway-linux-amd64"),
            str(ROOT / "gateway/sbx-gateway"),
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

    echo = socket.socket()
    echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    stop = threading.Event()
    threading.Thread(target=echo_server, args=(echo, stop), daemon=True).start()
    echo_port = echo.getsockname()[1]

    workers: list[subprocess.Popen[str]] = []
    mediators: list[subprocess.Popen[str]] = []
    qemus: list[subprocess.Popen[str]] = []
    qemu_listeners: list[socket.socket] = []
    ingress_listeners: list[socket.socket] = []
    transcripts = [bytearray() for _ in range(VM_COUNT)]
    with tempfile.TemporaryDirectory(prefix="sbx-gateway-multi-tcg-") as raw_runtime:
        runtime = Path(raw_runtime)
        package, upstream_ca = package_server(runtime)
        mediator_dirs: list[Path] = []
        mediator_ports: list[int] = []
        socket_paths: list[Path] = []
        for index in range(VM_COUNT):
            vm_id, generation, incarnation = f"tcg-vm-{index}", f"g-{index}", f"worker-{index}"
            vm_runtime = runtime / vm_id
            vm_runtime.mkdir(mode=0o700)
            key = os.urandom(32)
            mediator, ready, confdir = start_mediator(
                vm_runtime,
                upstream_ca,
                package.server_port,
                key,
                incarnation,
                vm_id,
                PAIR_FD_LIMIT // 2,
            )
            mediators.append(mediator)
            mediator_dirs.append(confdir)
            mediator_ports.append(int(ready["port"]))

            qemu_socket = vm_runtime / "qemu.sock"
            qemu_listener = socket.socket(socket.AF_UNIX)
            qemu_listener.bind(str(qemu_socket))
            os.chmod(qemu_socket, 0o600)
            qemu_listener.listen()
            qemu_listeners.append(qemu_listener)
            socket_paths.append(qemu_socket)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            ingress_listeners.append(listener)

            raw = vm_runtime / "raw.json"
            config = vm_runtime / "config.json"
            raw.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "vm_id": vm_id,
                        "generation": generation,
                        "rules": [
                            {
                                "id": "echo",
                                "protocol": "tcp",
                                "host": "localhost.localdomain",
                                "ports": [echo_port],
                                "allow_ranges": ["127.0.0.0/8"],
                            },
                            {
                                "id": "mediator",
                                "protocol": "http",
                                "host": "localhost.localdomain",
                                "ports": [package.server_port],
                            },
                        ],
                        "ingress": [
                            {
                                "id": "capacity",
                                "listen_host": "127.0.0.1",
                                "host_port": listener.getsockname()[1],
                                "guest_address": "192.0.2.2",
                                "guest_port": 3000,
                            }
                        ],
                        "quotas": {
                            "tcp_flows": 4,
                            "flows_per_destination": 2,
                            "ingress_flows": 1,
                            "dns_mappings": 2,
                            "open_files": PAIR_FD_LIMIT,
                        },
                    }
                )
            )
            prepared = subprocess.check_output(
                [str(gateway), "--prepare", "--config", str(raw)], text=True, timeout=10
            )
            config.write_text(prepared)
            config.chmod(0o600)
            worker = start_worker(
                gateway,
                qemu_socket,
                qemu_listener,
                config,
                json.loads(prepared)["digest"],
                ready,
                key,
                incarnation,
                [listener],
                vm_id=vm_id,
                generation=generation,
                ingress_ids=("capacity",),
            )
            workers.append(worker)

            requests = 2 if index == 1 else 1
            boot_args = manifest.get(
                "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
            ) + (
                " smolvm.network=guest"
                f" sbx.capacity_id={index}"
                f" sbx.capacity_echo_port={echo_port}"
                f" sbx.capacity_requests={requests}"
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
                "384",
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
                f"virtio-net-device,netdev=net0,mac=52:54:00:18:{index:02x}:01",
                "-device",
                "virtio-blk-device,drive=rootdisk",
            ]
            joined = " ".join(command).lower()
            require(
                joined.count("stream,id=net0") == 1
                and all(value not in joined for value in ("-net user", "tap", "sudo", "/dev/kvm")),
                "multi-VM QEMU command contained an alternate data plane",
            )
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_environment(vm_runtime),
            )
            PROCESSES.append(process)
            qemus.append(process)

        require(len(set(socket_paths)) == VM_COUNT, "QEMU sockets were shared")
        require(len(set(mediator_ports)) == VM_COUNT, "mediator listeners were shared")
        require(
            len({listener.getsockname()[1] for listener in ingress_listeners}) == VM_COUNT,
            "ingress ports were shared",
        )
        require(
            len({process.pid for process in [*workers, *mediators]}) == VM_COUNT * 2
            and all(os.getpgid(process.pid) == process.pid for process in [*workers, *mediators]),
            "worker and mediator process groups were shared",
        )
        ca_certs = [
            directory.joinpath("mitmproxy-ca-cert.pem").read_bytes() for directory in mediator_dirs
        ]
        ca_keys = [
            directory.joinpath("mitmproxy-ca.pem").read_bytes() for directory in mediator_dirs
        ]
        require(
            len({hashlib.sha256(value).digest() for value in ca_certs}) == VM_COUNT,
            "CA certificates were shared",
        )
        require(
            len({hashlib.sha256(value).digest() for value in ca_keys}) == VM_COUNT,
            "CA keys were shared",
        )

        selector = selectors.DefaultSelector()
        for index, process in enumerate(qemus):
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, index)
        ready: set[int] = set()
        passed: set[int] = set()
        first_sent: set[int] = set()
        isolated = False
        usage: list[tuple[int, int, int, int]] = []
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            require(all(process.poll() is None for process in qemus), "a capacity QEMU exited")
            for key, _ in selector.select(1):
                index = key.data
                chunk = os.read(key.fd, 65536)
                transcripts[index].extend(chunk)
                require(
                    len(transcripts[index]) <= MAX_TRANSCRIPT_BYTES,
                    "capacity transcript exceeded limit",
                )
                output = transcripts[index].decode(errors="replace")
                if f"GATEWAY_CAPACITY_READY {index}" in output:
                    ready.add(index)
                if f"GATEWAY_CAPACITY_PASS {index}" in output:
                    passed.add(index)
            for index in ready - first_sent:
                require(
                    ingress(ingress_listeners[index].getsockname()[1], str(index))
                    == f"capacity-{index}".encode(),
                    f"VM {index} ingress crossed ownership",
                )
                first_sent.add(index)
            if len(first_sent) == VM_COUNT and not usage:
                usage = [process_usage([workers[i].pid, mediators[i].pid]) for i in range(VM_COUNT)]
                for index, (rss, descriptors, processes, threads) in enumerate(usage):
                    require(rss <= PAIR_RSS_LIMIT, f"VM {index} exceeded RSS ceiling: {rss}")
                    fd_ceiling = open_file_limit(workers[index].pid) + open_file_limit(
                        mediators[index].pid
                    )
                    require(
                        fd_ceiling <= PAIR_FD_LIMIT,
                        f"VM {index} FD limits were weakened",
                    )
                    require(descriptors <= fd_ceiling, f"VM {index} exceeded FD ceiling")
                    require(processes <= PAIR_PROCESS_LIMIT, f"VM {index} exceeded process ceiling")
                    require(threads <= PAIR_THREAD_LIMIT, f"VM {index} exceeded thread ceiling")
            if set(range(VM_COUNT)) - {1} <= passed and usage and not isolated:
                terminate(workers[0])
                require(qemus[0].poll() is None, "VM 0 QEMU did not survive its worker crash")
                probe = socket.create_connection(ingress_listeners[0].getsockname(), timeout=1)
                try:
                    probe.settimeout(0.25)
                    probe.sendall(b"GET /after-crash HTTP/1.0\r\n\r\n")
                    with suppress(TimeoutError):
                        require(not probe.recv(1), "crashed VM retained ingress data flow")
                finally:
                    probe.close()
                require(
                    ingress(ingress_listeners[1].getsockname()[1], "1") == b"capacity-1",
                    "VM 0 crash affected VM 1 ingress",
                )
                require(
                    all(process.poll() is None for process in workers[1:]),
                    "VM 0 crash affected another worker",
                )
                require(
                    all(process.poll() is None for process in mediators[1:]),
                    "VM 0 crash affected another mediator",
                )
                isolated = True
            if isolated and len(passed) == VM_COUNT:
                print(
                    json.dumps(
                        {
                            "status": "PASS",
                            "vms": VM_COUNT,
                            "accelerator": "tcg",
                            "pair_usage": [
                                {
                                    "rss_bytes": item[0],
                                    "fds": item[1],
                                    "processes": item[2],
                                    "threads": item[3],
                                }
                                for item in usage
                            ],
                            "markers": [
                                transcript.decode(errors="replace")[-1000:]
                                for transcript in transcripts
                            ],
                        },
                        sort_keys=True,
                    )
                )
                return 0
        print(
            json.dumps(
                {
                    "ready": sorted(ready),
                    "passed": sorted(passed),
                    "first_sent": sorted(first_sent),
                    "isolated": isolated,
                    "markers": [
                        transcript.decode(errors="replace")[-4000:] for transcript in transcripts
                    ],
                },
                sort_keys=True,
            )
        )
        raise SystemExit("multi-VM TCG workload timed out")


def test_gateway_multi_vm_tcg() -> None:
    try:
        assert run_gateway_multi_vm_tcg() == 0
    finally:
        cleanup()
