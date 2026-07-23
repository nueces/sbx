#!/usr/bin/env python3
"""Required direct-QEMU rootless gateway integration check."""

from __future__ import annotations

import json
import os
import platform
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROCESSES: list[subprocess.Popen[str]] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def echo_server(listener: socket.socket, stop: threading.Event) -> None:
    listener.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)


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

    worker = qemu_process = None
    with tempfile.TemporaryDirectory(prefix="sbx-gateway-tcg-") as directory:
        runtime = Path(directory)
        raw = runtime / "raw.json"
        config = runtime / "config.json"
        qemu_socket = runtime / "qemu.sock"
        raw.write_text(json.dumps({
            "version": 1,
            "vm_id": "tcg-test",
            "generation": "g1",
            "rules": [{
                "id": "echo",
                "protocol": "tcp",
                "host": "localhost.localdomain",
                "ports": [18080],
                "allow_ranges": ["127.0.0.0/8"],
            }],
            "quotas": {
                "tcp_flows": 512,
                "flows_per_destination": 64,
                "dns_mappings": 1024,
                "open_files": 4096,
            },
        }))
        prepared = subprocess.check_output(
            [str(gateway), "--prepare", "--config", str(raw)], text=True
        )
        config.write_text(prepared)
        digest = json.loads(prepared)["digest"]
        worker = subprocess.Popen(
            [str(gateway), "--socket", str(qemu_socket), "--config", str(config),
             "--vm-id", "tcg-test", "--generation", "g1", "--digest", digest],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PROCESSES.append(worker)
        for _ in range(200):
            require(worker.poll() is None, "worker exited before readiness")
            if qemu_socket.is_socket():
                break
            time.sleep(0.025)
        require(qemu_socket.is_socket(), "worker socket readiness timed out")

        boot_args = manifest.get(
            "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
        ) + " smolvm.network=guest"
        command = [
            qemu, "-nodefaults", "-serial", "stdio", "-monitor", "none", "-nographic",
            "-no-reboot", "-smp", "1", "-m", "512", "-machine", "microvm,accel=tcg",
            "-cpu", "qemu64", "-kernel", str(kernel), "-append", boot_args,
            "-drive", f"file={rootfs},if=none,format=raw,id=rootdisk",
            "-netdev", f"stream,id=net0,server=off,addr.type=unix,addr.path={qemu_socket}",
            "-device", "virtio-net-device,netdev=net0,mac=52:54:00:12:34:56",
            "-device", "virtio-blk-device,drive=rootdisk",
        ]
        joined = " ".join(command)
        require("accel=tcg" in joined and "stream,id=net0" in joined, "unsafe QEMU command")
        forbidden = ("-net user", "tap", "nft", "sudo", "/dev/kvm")
        require(
            all(value not in joined for value in forbidden),
            "forbidden host networking requested",
        )
        qemu_process = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PROCESSES.append(qemu_process)
        transcript: list[str] = []
        deadline = time.monotonic() + 120
        selector = selectors.DefaultSelector()
        assert qemu_process.stdout is not None
        selector.register(qemu_process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            require(qemu_process.poll() is None, "QEMU exited before test completion")
            if not selector.select(timeout=1):
                continue
            line = qemu_process.stdout.readline()
            transcript.append(line)
            if "GATEWAY_TEST_PASS" in line:
                terminate(worker)
                require(worker.poll() is not None, "worker did not stop")
                require(qemu_process.poll() is None, "QEMU did not survive worker loss")
                transcript.append("GATEWAY_WORKER_LOSS_PASS\n")
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
