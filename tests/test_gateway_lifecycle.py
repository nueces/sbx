import errno
import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sbx import cli, gateway, gateway_supervisor


@pytest.fixture
def lifecycle_config() -> gateway.GatewayConfig:
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {"id": "web", "mode": "http-mediated", "hosts": ["example.com"], "ports": [443]}
                ],
                "ingress": [{"listen": "127.0.0.1:0", "guest_port": 3000}],
            }
        },
        platform="linux",
    )
    assert config is not None
    return config


class FakeListener:
    def __init__(self, *args: object, fail: bool = False, port: int = 45123) -> None:
        del args
        self.fail = fail
        self.port = port
        self.closed = False
        self.address: tuple[str, int] = ("127.0.0.1", port)

    def bind(self, address: tuple[str, int]) -> None:
        if self.fail:
            raise OSError("bind failed")
        self.address = (address[0], address[1] or self.port)

    def listen(self) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return self.address

    def fileno(self) -> int:
        return 900_100

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    next_pid = 900_000

    def __init__(
        self, payload: bytes = b"", *, exited: bool = False, hold_stdout: bool = False
    ) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        self.resources: list[Any] = []
        if hold_stdout:
            self.resources.append(os.fdopen(write_fd, "wb", buffering=0))
        else:
            os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.returncode = 1 if exited else None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self._close_resources()

    def kill(self) -> None:
        self.returncode = -9
        self._close_resources()

    def wait(self, timeout: float = 0) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", 0)
        return self.returncode

    def crash(self) -> None:
        self.returncode = 1
        self._close_resources()

    def _close_resources(self) -> None:
        for resource in self.resources:
            resource.close()
        self.resources.clear()


class FakeRunner:
    def __init__(self, failure: str | None = None, replacement_failure: str | None = None) -> None:
        self.failure = failure
        self.replacement_failure = replacement_failure
        self.calls: list[list[str]] = []
        self.passed_fds: list[int] = []
        self.processes: list[FakeProcess] = []
        self.kwargs: list[dict[str, Any]] = []
        self.worker_starts = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        self.passed_fds.extend(kwargs["pass_fds"])
        component = "mediator" if argv[0] == "fake-mediator" else "worker"
        if component == "worker":
            self.worker_starts += 1
        behavior = (
            self.replacement_failure
            if self.worker_starts > 1 or component == "mediator" and self.worker_starts > 0
            else self.failure
        )
        if behavior == f"{component}_spawn":
            raise OSError(f"{component} spawn failed")
        if component == "mediator":
            process = FakeProcess(
                b"mediator boom\n" if behavior == "mediator_log" else b"",
                exited=behavior in {"mediator_exit", "mediator_log"},
            )
            ready_fd = kwargs["pass_fds"][1]
            process.resources.append(os.fdopen(os.dup(kwargs["pass_fds"][2]), "rb", buffering=0))
            if behavior == "mediator_timeout":
                process.resources.append(os.fdopen(os.dup(ready_fd), "wb", buffering=0))
            elif behavior not in {"mediator_exit", "mediator_log"}:
                payload: object = {
                    "status": "ready",
                    "host": "0.0.0.0" if behavior == "mediator_identity" else "127.0.0.1",
                    "port": 1234,
                    "web_port": False if behavior == "mediator_web" else 2345,
                    "pid": process.pid,
                    "vm_id": argv[-2],
                    "incarnation": argv[-1],
                    "hardening": "no_new_privs,seccomp,landlock-unavailable,rlimits",
                }
                os.write(ready_fd, json.dumps(payload).encode() + b"\n")
        else:
            policy = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
            token_fd = int(argv[argv.index("--incarnation-token-fd") + 1])
            token = os.read(token_fd, 32)
            vm_id = argv[argv.index("--vm-id") + 1]
            generation = argv[argv.index("--generation") + 1]
            incarnation = argv[argv.index("--worker-incarnation") + 1]
            message = "\0".join((vm_id, generation, policy["digest"], incarnation)).encode()
            ready = {
                "status": "ready",
                "version": 1,
                "vm_id": "wrong-vm" if behavior == "worker_identity" else vm_id,
                "generation": generation,
                "digest": policy["digest"],
                "incarnation": incarnation,
                "incarnation_proof": hmac.new(token, message, hashlib.sha256).hexdigest(),
                "mediator": True,
                "ingress": 1,
                "hardening": "no_new_privs,seccomp,landlock-unavailable,rlimits",
            }
            network_ready = b'\n{"status":"network-ready","qemu_pid":12345}\n'
            payload = json.dumps(ready).encode() + network_ready
            if behavior == "worker_stale":
                ready["incarnation"] = "stale-worker"
                payload = json.dumps(ready).encode() + network_ready
            elif behavior == "worker_proof":
                ready["incarnation_proof"] = "0" * 64
                payload = json.dumps(ready).encode() + b'\n{"status":"network-ready"}\n'
            elif behavior == "worker_timeout":
                payload = b""
            elif behavior == "worker_status":
                payload = b'{"status":"wrong"}\n'
            process = FakeProcess(
                payload,
                exited=behavior == "worker_exit",
                hold_stdout=behavior == "worker_timeout",
            )
            if behavior == "worker_socket":
                Path(argv[argv.index("--socket") + 1]).unlink()
        self.processes.append(process)
        return process


def controller(
    config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None = None,
    replacement_failure: str | None = None,
    listener_fail: bool = False,
    vm_id: str = "vm1",
    generation: str = "generation1",
    listener_port: int = 45123,
) -> tuple[gateway.GatewayController, FakeRunner, list[FakeListener]]:
    runner = FakeRunner(failure, replacement_failure)
    listeners: list[FakeListener] = []

    def socket_factory(*args: object) -> FakeListener:
        listener = FakeListener(*args, fail=listener_fail, port=listener_port)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(
        gateway.GatewayController,
        "_mediator_command",
        lambda self, key, ready, policy, broker: [
            "fake-mediator",
            str(key),
            str(ready),
            str(policy),
            self.vm_id,
            self.worker_incarnation,
        ],
    )
    monkeypatch.setattr(
        gateway.GatewayController,
        "_worker_command",
        lambda self, key, token: [
            "fake-sbx-gateway",
            "--socket",
            str(self.qemu_socket),
            "--config",
            str(self.directory / "policy.json"),
            "--vm-id",
            self.vm_id,
            "--generation",
            self.generation,
            "--worker-incarnation",
            self.worker_incarnation,
            "--incarnation-token-fd",
            str(token),
            "--capability-key-fd",
            str(key),
            "--mediator-address",
            str(self.mediator_address),
        ],
    )
    instance = gateway.GatewayController(
        config,
        vm_id,
        root=tmp_path / "state",
        runtime_root=Path("/tmp")
        / f"sbx-test-{hashlib.sha256(os.fsencode(tmp_path)).hexdigest()[:16]}",
        generation=generation,
        runner=runner,
        socket_factory=socket_factory,  # type: ignore[arg-type]
        timeout=0.1,
    )
    return instance, runner, listeners


def test_mediator_policy_contains_no_credential_value_on_disk(tmp_path: Path) -> None:
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {"id": "web", "mode": "http-mediated", "hosts": ["example.com"], "ports": [443]}
                ],
                "credential": [
                    {
                        "id": "token",
                        "environment": "TOKEN",
                        "method": "command",
                        "options": {"argv": ["get-token"]},
                        "bindings": [
                            {"policies": ["web"], "header": "Authorization: Bearer {placeholder}"}
                        ],
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    instance = gateway.GatewayController(config, "vm1", root=tmp_path / "state")
    policy = instance._mediator_policy()
    assert b'"value":"Bearer {placeholder}"' in policy
    assert b"real-token" not in policy
    instance.directory = gateway.create_state(config, "vm1", "generation1", root=tmp_path / "state")
    placeholder = next(iter(instance._placeholders.values())).encode()
    assert placeholder not in (instance.directory / "policy.json").read_bytes()
    instance.close()


def test_command_and_controller_endpoint_failures_are_closed_and_classified(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway.GatewayController(lifecycle_config, "vm1", root=tmp_path / "state")
    instance.runtime_directory = gateway._create_runtime_directory(
        "generation1", root=tmp_path / "runtime"
    )
    instance.directory = gateway.create_state(
        lifecycle_config,
        "vm1",
        "generation1",
        root=tmp_path / "state",
        runtime_directory=instance.runtime_directory,
    )
    source = tmp_path / "source"
    source.joinpath("mediator").mkdir(parents=True)
    launcher = source / "mediator" / "launch.py"
    launcher.touch()
    monkeypatch.setattr(gateway, "GATEWAY_MEDIATOR_LAUNCHER", tmp_path / "missing-launcher")
    monkeypatch.setattr(gateway, "GATEWAY_SOURCE_DIR", source)
    monkeypatch.setenv("SBX_GATEWAY_BIN", str(tmp_path / "missing-worker"))
    assert instance._mediator_command(1, 2, 3, 4)[:2] == [sys.executable, str(launcher)]
    assert instance._worker_command(None, 4)[0] == str(tmp_path / "missing-worker")

    existing = instance.qemu_socket
    existing.touch()
    with pytest.raises(OSError):
        instance._reserve_qemu_listener()
    existing.unlink()

    monkeypatch.setattr(
        gateway.os,
        "write",
        lambda *_args: (_ for _ in ()).throw(OSError("pipe write failed")),
    )
    with pytest.raises(OSError, match="pipe write failed"):
        instance._pipe_with(b"token")
    instance.close()


@pytest.mark.parametrize("failure", [None, "mediator_web"])
def test_web_readiness_and_persistent_audit(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    instance, _, _ = controller(
        replace(lifecycle_config, mitmweb=True), tmp_path, monkeypatch, failure=failure
    )
    try:
        if failure:
            with pytest.raises(gateway.GatewayAuthenticationError, match="web readiness"):
                instance.prepare()
        else:
            instance.prepare()
            assert instance.directory is not None
            assert gateway._read_json(instance.directory / "web.json") == {"port": 2345}
            assert (instance.directory / "audit.log").stat().st_mode & 0o777 == 0o600
            original_stream = instance._audit_stream
            instance.recover()
            assert instance._audit_stream is original_stream
    finally:
        instance.close()


def test_readiness_rejects_invalid_exited_closed_and_oversized_channels(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
) -> None:
    instance = gateway.GatewayController(
        lifecycle_config, "vm1", root=tmp_path / "state", timeout=0.1
    )
    cases = (
        (FakeProcess(b"not-json\n"), "invalid readiness"),
        (FakeProcess(exited=True), "exited before readiness"),
        (FakeProcess(), "closed its readiness"),
        (FakeProcess(b"x" * 4097), "oversized readiness"),
    )
    for process, message in cases:
        with pytest.raises(gateway.GatewayLifecycleError, match=message):
            instance._read_json_fd(process.stdout.fileno(), process, "worker")
        process.terminate()


def test_logs_are_private_bounded_and_artifact_overrides_use_native_paths(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway.GatewayController(lifecycle_config, "vm1", root=tmp_path / "state")
    instance.runtime_directory = tmp_path / "runtime" / "generation1"
    instance.directory = gateway.create_state(
        lifecycle_config,
        "vm1",
        "generation1",
        root=tmp_path / "state",
        runtime_directory=instance.runtime_directory,
    )
    log = instance._open_log("worker.log")
    assert stat.S_IMODE(os.fstat(log.fileno()).st_mode) == 0o600
    instance._placeholders = {"TOKEN": "secret-placeholder"}
    instance._credential_broker = type("Broker", (), {"values": {"secret-value"}})()
    instance._emit_log(log, b"secret-value secret-placeholder")
    assert (instance.directory / "worker.log").read_bytes() == b"[redacted] [redacted]\n"
    instance._log_last = 1
    monkeypatch.setattr(gateway.time, "monotonic", lambda: 1)
    for _ in range(gateway.MAX_LOG_EVENT_BURST + 10):
        instance._emit_log(log, b"event")
    assert (instance.directory / "worker.log").read_bytes().count(b"\n") == (
        gateway.MAX_LOG_EVENT_BURST
    )
    log.close()
    (instance.directory / "oversized.log").write_bytes(b"")
    os.truncate(instance.directory / "oversized.log", gateway.MAX_LOG_BYTES)
    with pytest.raises(gateway.GatewayLifecycleError, match="log limit"):
        instance._open_log("next.log")

    worker = tmp_path / "worker"
    worker.write_text("worker")
    worker.chmod(0o777)
    monkeypatch.setenv("SBX_GATEWAY_BIN", str(worker))
    assert instance._worker_command(None, 4)[0] == str(worker)
    monkeypatch.setenv("SBX_GATEWAY_BIN", "relative-worker")
    assert instance._worker_command(None, 4)[0] == "relative-worker"
    instance.close()


def test_real_commands_bind_fresh_authentication_and_controller_endpoints(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = tmp_path / "sbx-gateway"
    python = tmp_path / "python"
    launcher = tmp_path / "launch.py"
    for artifact in (worker, python, launcher):
        artifact.touch()
    worker.chmod(0o700)
    python.chmod(0o700)
    launcher.chmod(0o600)
    monkeypatch.setenv("SBX_GATEWAY_BIN", str(worker))
    monkeypatch.setattr(gateway.sys, "executable", str(python))
    monkeypatch.setattr(gateway, "GATEWAY_MEDIATOR_LAUNCHER", launcher)
    instance = gateway.GatewayController(
        lifecycle_config,
        "vm1",
        root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
        generation="generation1",
    )
    instance.runtime_directory = gateway._create_runtime_directory(
        "generation1", root=tmp_path / "runtime"
    )
    instance.directory = gateway.create_state(
        lifecycle_config,
        "vm1",
        "generation1",
        root=tmp_path / "state",
        runtime_directory=instance.runtime_directory,
    )
    instance._reserve_qemu_listener()
    instance._reserve_listeners()
    instance.mediator_address = "127.0.0.1:1234"

    mediator = instance._mediator_command(11, 12, 13, 14)
    worker_command = instance._worker_command(14, 15)

    assert mediator[:2] == [str(python), str(launcher)]
    assert mediator[mediator.index("--incarnation") + 1] == instance.worker_incarnation
    assert mediator[mediator.index("--broker-fd") + 1] == "14"
    assert worker_command[worker_command.index("--qemu-listener-fd") + 1] == str(
        instance.qemu_listener.fileno()  # type: ignore[union-attr]
    )
    assert worker_command[worker_command.index("--incarnation-token-fd") + 1] == "15"
    assert worker_command[worker_command.index("--digest") + 1]
    assert "vm1/generation1/ingress-1=" in " ".join(worker_command)
    assert all(
        value not in " ".join(worker_command).lower()
        for value in ("slirp", "hostfwd", "-netdev user", "tap")
    )
    instance.close()


def test_fake_runner_and_socket_normal_lifecycle(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)

    instance.prepare()
    with pytest.raises(gateway.GatewayLifecycleError, match="already prepared"):
        instance.prepare()
    state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
    assert [call[0] for call in runner.calls] == ["fake-mediator", "fake-sbx-gateway"]
    assert state["status"] == "starting"
    assert state["listeners"][0]["host_port"] == 45123
    assert instance.qemu_socket.is_socket()
    assert listeners[0].fileno() in runner.passed_fds
    with pytest.raises(OSError):
        os.fstat(int(runner.calls[1][runner.calls[1].index("--incarnation-token-fd") + 1]))

    instance.vm_network_ready(12345)
    state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
    assert state["status"] == "ready"
    assert state["pids"]["qemu"] == 12345
    command = " ".join(runner.calls[1]).lower()
    assert "--mediator-address 127.0.0.1:1234" in command
    assert all(value not in command for value in ("slirp", "hostfwd", "-netdev user", "tap"))

    directory = instance.directory
    assert directory is not None
    evidence = b'{"event":"admitted","request_id":"evidence"}\n'
    (directory / "audit.log").write_bytes(evidence)
    (directory / "mitmproxy-ca.pem").write_text("test private CA")
    (directory / "web-auth.json").write_text("test UI token")
    instance.close()
    assert directory.exists()
    assert (directory / "audit.log").read_bytes() == evidence
    assert gateway.read_state(directory)["status"] == "stopped"
    assert not (directory / "mitmproxy-ca.pem").exists()
    assert not (directory / "web-auth.json").exists()
    assert listeners[0].closed
    assert all(process.poll() is not None for process in runner.processes)
    assert runner.kwargs[0]["env"]["PATH"] == os.environ.get("PATH", os.defpath)
    assert runner.kwargs[1]["env"]["PATH"] == os.defpath
    for kwargs in runner.kwargs:
        assert set(kwargs["env"]) == {"HOME", "LANG", "PATH", "TZ"}
        assert not set(kwargs["env"]) & {"LD_PRELOAD", "PYTHONPATH", "HTTP_PROXY"}
        assert kwargs["close_fds"] is not False if "close_fds" in kwargs else True


def test_two_vm_resources_and_quota_failure_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VM_A_TOKEN", "token-a")
    monkeypatch.setenv("VM_B_TOKEN", "token-b")

    def isolated_config(host: str, provider: str) -> gateway.GatewayConfig:
        parsed = gateway.parse_config(
            {
                "network": {
                    "policy": [
                        {
                            "id": "api",
                            "mode": "http-mediated",
                            "hosts": [host],
                            "ports": [443],
                            "hooks": ["package-audit"],
                        }
                    ],
                    "credential": [
                        {
                            "id": "token",
                            "environment": "TOKEN",
                            "method": "command",
                            "options": {"argv": ["get-token"]},
                            "bindings": [
                                {
                                    "policies": ["api"],
                                    "header": "Authorization: Bearer {placeholder}",
                                }
                            ],
                        }
                    ],
                    "ingress": [{"guest_port": 3000}],
                    "quotas": {"tcp_flows": 2, "ingress_flows": 1, "open_files": 64},
                }
            },
            platform="linux",
        )
        assert parsed is not None
        return parsed

    a, runner_a, listeners_a = controller(
        isolated_config("a.example", "env:VM_A_TOKEN"),
        tmp_path,
        monkeypatch,
        vm_id="vm-a",
        generation="generation-a",
        listener_port=45123,
    )
    b, runner_b, listeners_b = controller(
        isolated_config("b.example", "env:VM_B_TOKEN"),
        tmp_path,
        monkeypatch,
        vm_id="vm-b",
        generation="generation-b",
        listener_port=45124,
    )
    a.prepare()
    b.prepare()
    a._read_worker_status("network-ready")
    b._read_worker_status("network-ready")
    gateway.update_state(a.directory, status="ready")
    gateway.update_state(b.directory, status="ready")

    assert a.directory != b.directory
    assert a.qemu_socket != b.qemu_socket
    assert a.worker_incarnation != b.worker_incarnation
    assert a.mac != b.mac
    assert a.credential_placeholders["TOKEN"] != b.credential_placeholders["TOKEN"]
    assert a.listener_rules[0].host_port != b.listener_rules[0].host_port
    assert set(a.processes.values()).isdisjoint(b.processes.values())
    assert a._mediator_policy_payload is not None and b._mediator_policy_payload is not None
    assert a._immutable_policy is not None and b._immutable_policy is not None
    assert b"token-a" not in a._mediator_policy_payload
    assert b"token-b" not in b._mediator_policy_payload
    assert b"a.example" in a._immutable_policy and b"b.example" not in a._immutable_policy
    assert b"b.example" in b._immutable_policy and b"a.example" not in b._immutable_policy
    assert all(path.parent == a.directory for path in a.directory.glob("*.log"))
    assert all(path.parent == b.directory for path in b.directory.glob("*.log"))
    with pytest.raises(gateway.GatewayStateError, match="cross-VM"):
        gateway.read_state(a.directory, vm_id="vm-b")

    b._worker_buffer.extend(
        b'{"status":"stats","tcp_active":0,"dns_rejected":1,"dns_mappings":2}\n'
    )
    b._drain_worker_stats()
    b_state = gateway.read_state(b.directory)
    assert b_state["counters"]["dns_rejected"] == 1
    assert b_state["last_failure"]["code"] == "quota_exhausted"
    assert gateway.read_state(a.directory)["last_failure"] is None

    a_pids = {process.pid for process in a.processes.values()}

    def usage(pids: list[int]) -> tuple[int, int, int, int]:
        if a_pids.intersection(pids):
            return gateway.MAX_DATA_PLANE_RSS + 1, 1, 2, 2
        return 1, 1, 2, 2

    monkeypatch.setattr(gateway, "_process_tree_usage", usage)
    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="resource ceiling"):
        a.check()
    assert time.monotonic() - started < 1
    assert gateway.read_state(a.directory)["last_failure"]["code"] == "quota_exhausted"
    assert listeners_a[0].closed
    assert all(process.poll() is not None for process in runner_a.processes)
    b.check()
    assert b.qemu_socket.exists() and not listeners_b[0].closed
    assert all(process.poll() is None for process in runner_b.processes)
    a.close()
    b.close()


def test_authenticated_replacement_preserves_endpoints_and_discards_every_old_flow(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    old_incarnation = instance.worker_incarnation
    qemu_inode = instance.qemu_socket.stat().st_ino
    qemu_fd = instance.qemu_listener.fileno()  # type: ignore[union-attr]
    ingress_fd = listeners[0].fileno()
    peers = {}
    for flow, component in {
        "tcp": "worker",
        "ingress": "worker",
        "http": "mediator",
        "websocket": "mediator",
    }.items():
        owned, peer = socket.socketpair()
        instance.processes[component].resources.append(owned)
        peer.settimeout(0.2)
        peers[flow] = peer

    instance.recover()

    assert instance.worker_incarnation != old_incarnation
    assert instance.qemu_socket.stat().st_ino == qemu_inode
    assert instance.qemu_listener.fileno() == qemu_fd  # type: ignore[union-attr]
    assert listeners[0].fileno() == ingress_fd
    assert gateway.read_state(instance.directory)["status"] == "ready"  # type: ignore[arg-type]
    assert runner.worker_starts == 2
    for flow, peer in peers.items():
        assert peer.recv(1) == b"", f"stale {flow} flow survived replacement"
        peer.close()
    qemu = socket.socket(socket.AF_UNIX)
    qemu.settimeout(0.2)
    qemu.connect(str(instance.qemu_socket))
    qemu.close()
    commands = " ".join(" ".join(call) for call in runner.calls).lower()
    assert all(value not in commands for value in ("slirp", "hostfwd", "-netdev user", "tap"))
    instance.close()


@pytest.mark.parametrize("replacement_failure", ["worker_stale", "worker_identity", "worker_proof"])
def test_stale_worker_wrong_identity_and_wrong_token_hard_fail_without_retry(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_failure: str,
) -> None:
    config = replace(
        lifecycle_config, reconnection=gateway.Reconnection(attempts=3, timeout_seconds=0.3)
    )
    instance, runner, listeners = controller(
        config, tmp_path, monkeypatch, replacement_failure=replacement_failure
    )
    instance.prepare()
    old_worker = runner.processes[-1]

    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="reconnection failed"):
        instance.recover()

    assert time.monotonic() - started < 1
    assert runner.worker_starts == 2
    assert old_worker.poll() is not None
    assert not instance.qemu_socket.exists()
    assert listeners[0].closed
    state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
    assert state["status"] == "failed"
    assert state["last_failure"]["code"] == "reconnection_failed"
    instance.close()


def test_changed_immutable_config_hard_fails_before_replacement_spawn(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    policy = instance.directory / "policy.json"  # type: ignore[operator]
    policy.write_bytes(policy.read_bytes() + b" ")

    with pytest.raises(gateway.GatewayLifecycleError, match="config mismatch"):
        instance.recover()

    assert runner.worker_starts == 1
    assert not instance.qemu_socket.exists()
    instance.close()


def test_replacement_retries_and_timeout_are_globally_bounded(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_config = replace(
        lifecycle_config, reconnection=gateway.Reconnection(attempts=2, timeout_seconds=0.3)
    )
    retrying, retry_runner, _ = controller(
        retry_config, tmp_path, monkeypatch, replacement_failure="worker_exit"
    )
    retrying.prepare()
    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="reconnection failed"):
        retrying.recover()
    assert time.monotonic() - started < 1
    assert retry_runner.worker_starts == 3
    retrying.close()

    timing_out, timeout_runner, _ = controller(
        replace(
            lifecycle_config,
            reconnection=gateway.Reconnection(attempts=3, timeout_seconds=0.15),
        ),
        tmp_path / "timeout",
        monkeypatch,
        replacement_failure="worker_timeout",
    )
    timing_out.prepare()
    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="timed out"):
        timing_out.recover()
    assert 0.1 <= time.monotonic() - started < 0.8
    assert timeout_runner.worker_starts <= 4
    timing_out.close()


def test_recovery_reuses_generation_dns_journal(
    lifecycle_config: gateway.GatewayConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, runner, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    journal = instance.directory / "dns-mappings.jsonl"
    mapping = b'{"host":"example.com","ip":"198.18.0.123"}\n'
    with journal.open("ab") as stream:
        stream.write(mapping)
    before = journal.read_bytes()
    assert instance._dns_state_fd is None
    instance.processes["worker"].crash()
    instance.recover()
    assert runner.worker_starts == 2
    assert journal.read_bytes() == before
    assert instance._dns_state_fd is None
    instance.close()
    assert journal.read_bytes() == before
    gateway.remove(instance.vm_id, root=instance.root)
    assert not journal.exists()


@pytest.mark.parametrize("failure", ["permissions", "io", "missing", "empty"])
def test_dns_state_failure_during_recovery_stops_mediation(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    path = instance.directory / "dns-mappings.jsonl"
    before = path.read_bytes()
    if failure == "permissions":
        path.chmod(0o644)
    elif failure == "missing":
        path.unlink()
    elif failure == "empty":
        path.write_bytes(b"")
    else:

        def fail_open(_policy):
            raise OSError(errno.EIO, "simulated journal I/O failure")

        monkeypatch.setattr(instance, "_open_dns_state", fail_open)
    instance.processes["worker"].crash()
    with pytest.raises(gateway.GatewayLifecycleError) as failed:
        instance.recover()
    assert failed.value.__cause__ is not None
    assert instance.processes == {}
    assert all(process.poll() is not None for process in runner.processes)
    assert listeners[0].closed
    if failure == "missing":
        assert not path.exists()
    else:
        assert path.read_bytes() == (b"" if failure == "empty" else before)
    assert gateway.read_state(instance.directory)["status"] == "failed"
    instance.close()


def test_disabled_recovery_and_mediator_loss_replaces_pair(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(
        lifecycle_config,
        reconnection=gateway.Reconnection(enabled=False, attempts=0, timeout_seconds=0.1),
    )
    instance, _, listeners = controller(disabled, tmp_path, monkeypatch)
    instance.prepare()
    with pytest.raises(gateway.GatewayLifecycleError, match="disabled"):
        instance.recover()
    with pytest.raises(gateway.GatewayLifecycleError, match="not recoverable"):
        instance.recover()
    assert listeners[0].closed
    assert not instance.qemu_socket.exists()
    instance.close()

    failed, runner, failed_listeners = controller(
        lifecycle_config, tmp_path, monkeypatch, generation="generation2"
    )
    failed.prepare()
    failed.processes["mediator"].crash()
    failed.check()
    assert not failed_listeners[0].closed
    assert failed.qemu_socket.exists()
    assert runner.worker_starts == 2
    failed.close()


def test_check_synchronously_recovers_worker_before_supervisor_start(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    runner.processes[-1].crash()

    instance.check()

    assert runner.worker_starts == 2
    assert gateway.read_state(instance.directory)["status"] == "ready"  # type: ignore[arg-type]
    instance.close()


def test_supervisor_automatically_replaces_crashed_worker_with_bounded_wait(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    instance.vm_network_ready(12345)
    runner.processes[-1].crash()

    deadline = time.monotonic() + 1
    state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
    while (runner.worker_starts < 2 or state["status"] != "ready") and time.monotonic() < deadline:
        time.sleep(0.01)
        state = gateway.read_state(instance.directory)  # type: ignore[arg-type]

    assert runner.worker_starts == 2
    assert state["status"] == "ready"
    instance.close()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("mediator_spawn", "spawn failed"),
        ("mediator_exit", "exited before readiness"),
        ("mediator_timeout", "timed out"),
        ("mediator_identity", "identity mismatch"),
        ("worker_spawn", "spawn failed"),
        ("worker_exit", "exited before readiness"),
        ("worker_status", "unexpected readiness status"),
        ("worker_identity", "readiness identity mismatch"),
    ],
)
def test_every_child_startup_failure_rolls_back_fake_resources(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    instance, runner, listeners = controller(
        lifecycle_config, tmp_path, monkeypatch, failure=failure
    )

    with pytest.raises((gateway.GatewayLifecycleError, OSError), match=message):
        instance.prepare()

    assert instance.directory is not None and not instance.directory.exists()
    assert listeners[0].closed
    assert all(process.poll() is not None for process in runner.processes)


def test_mediator_startup_failure_includes_bounded_log_detail(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _ = controller(lifecycle_config, tmp_path, monkeypatch, failure="mediator_log")

    with pytest.raises(gateway.GatewayLifecycleError, match="mediator boom"):
        instance.prepare()


def test_port_zero_listener_is_reserved_without_probe_release(tmp_path: Path) -> None:
    config = gateway.parse_config(
        {"network": {"ingress": [{"guest_port": 3000}]}}, platform="linux"
    )
    assert config is not None
    instance = gateway.GatewayController(config, "vm1", root=tmp_path / "state")
    instance._reserve_listeners()
    try:
        host, port = instance.listener_rules[0].listen_host, instance.listener_rules[0].host_port
        assert host == "127.0.0.1" and port != 0
        competing = socket.socket()
        try:
            with pytest.raises(OSError):
                competing.bind((host, port))
        finally:
            competing.close()
    finally:
        instance.drain()


def test_explicit_non_loopback_bind_warns_and_is_exact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = gateway.parse_config(
        {"network": {"ingress": [{"listen": "0.0.0.0:0", "guest_port": 4180}]}},
        platform="linux",
    )
    assert config is not None
    instance = gateway.GatewayController(config, "vm1", root=tmp_path / "state")
    instance._reserve_listeners()
    try:
        assert instance.listener_rules[0].listen_host == "0.0.0.0"
        warning = capsys.readouterr().err
        assert "warning" in warning and "reachable" in warning and "0.0.0.0" in warning
    finally:
        instance.drain()


def test_listener_startup_failure_prevents_children_and_removes_state(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(
        lifecycle_config, tmp_path, monkeypatch, listener_fail=True
    )

    with pytest.raises(OSError, match="bind failed"):
        instance.prepare()

    assert runner.calls == []
    assert listeners[0].closed
    assert instance.directory is not None and not instance.directory.exists()


def test_state_startup_failure_opens_no_socket_or_process(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)

    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        instance.prepare()

    assert runner.calls == []
    assert listeners == []


def test_unprepared_network_readiness_and_stubborn_process_termination_fail_bounded(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
) -> None:
    instance = gateway.GatewayController(lifecycle_config, "vm1", root=tmp_path / "state")
    with pytest.raises(gateway.GatewayLifecycleError, match="has not been prepared"):
        instance.vm_network_ready(1)

    class StubbornProcess:
        pid = 987_654
        returncode: int | None = None
        terminated = False
        killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float = 0) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("stubborn", timeout)
            return self.returncode

    process = StubbornProcess()
    instance._terminate(process)
    assert process.terminated and process.killed and process.poll() == -9


def test_qemu_peer_process_identity_mismatch_fails_closed(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()

    with pytest.raises(gateway.GatewayAuthenticationError, match="peer process identity"):
        instance.vm_network_ready(54321)

    assert instance.directory is not None and not instance.directory.exists()
    assert listeners[0].closed
    assert all(process.poll() is not None for process in runner.processes)


def test_post_qemu_readiness_failure_rolls_back_everything(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    runner.processes[1].stdout.close()

    with pytest.raises((gateway.GatewayLifecycleError, ValueError, OSError)):
        instance.vm_network_ready(12345)

    assert instance.directory is not None and not instance.directory.exists()
    assert listeners[0].closed
    assert all(process.poll() is not None for process in runner.processes)


class FakeVM:
    vm_id = "vm1"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        config = type("Config", (), {"workspace_mounts": ()})()
        network = type("Network", (), {"ssh_host_port": 2200})()
        self._info = type("Info", (), {"config": config, "network": network})()

    def wait_for_ssh(self, *, timeout: float) -> None:
        assert timeout == 1
        self.events.append("ssh-ready")

    def stop(self) -> None:
        self.events.append("vm-stop")


class FakeController:
    directory = Path("/fake/state")
    qemu_socket = Path("/fake/state/qemu-net.sock")
    mac = "52:54:00:12:34:56"
    credential_placeholders: dict[str, str] = {}

    def __init__(self, _config: object, _vm_id: str, events: list[str]) -> None:
        self.events = events

    def prepare(self) -> None:
        self.events.append("prepare")

    def vm_network_ready(self, _pid: int) -> None:
        self.events.append("network-ready")

    def check(self) -> None:
        self.events.append("check")

    def drain(self) -> None:
        self.events.append("drain")

    def rollback(self, _code: str) -> None:
        self.events.append("rollback")


@pytest.mark.parametrize("failure", [None, "qemu", "network", "ca", "install"])
def test_cli_startup_order_and_every_post_worker_failure_rolls_back(
    lifecycle_config: gateway.GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    events: list[str] = []
    fake = FakeController(lifecycle_config, "vm1", events)
    vm = FakeVM(events)
    holder: list[gateway.GatewayController] = []
    monkeypatch.setattr(cli.gateway, "PersistentGatewayController", lambda *_args: fake)

    def start_vm(*_args: object, **_kwargs: object) -> int:
        events.append("qemu")
        if failure == "qemu":
            raise RuntimeError("qemu failed")
        return 123

    def network_ready(pid: int, timeout: float) -> None:
        assert pid == 123
        assert timeout == 1
        events.append("network-ready")
        if failure == "network":
            raise RuntimeError("network failed")

    def finalize(_directory: Path) -> object:
        events.append("ca")
        if failure == "ca":
            raise RuntimeError("ca failed")
        return object()

    def install(_vm_id: str, _ca: object) -> None:
        events.append("install")
        if failure == "install":
            raise RuntimeError("install failed")

    fake.vm_network_ready = network_ready  # type: ignore[method-assign]
    monkeypatch.setattr(cli.smolvm_network, "start_vm", start_vm)
    monkeypatch.setattr(cli.gateway_ca, "finalize_ca_state", finalize)
    monkeypatch.setattr(cli.gateway_ca, "install_guest_ca", install)
    monkeypatch.setattr(
        cli.guest_setup,
        "sync_network_credentials",
        lambda *_args: events.append("credentials"),
    )

    if failure is None:
        cli._start_gateway_vm(vm, lifecycle_config, 1, holder)  # type: ignore[arg-type]
        assert events == [
            "prepare",
            "qemu",
            "network-ready",
            "ssh-ready",
            "credentials",
            "ca",
            "install",
            "check",
        ]
        assert holder == [fake]
    else:
        with pytest.raises(RuntimeError, match="failed"):
            cli._start_gateway_vm(vm, lifecycle_config, 1, holder)  # type: ignore[arg-type]
        expected = ["prepare", "qemu"]
        if failure not in {"qemu"}:
            expected.append("network-ready")
        if failure not in {"qemu", "network"}:
            expected.extend(("ssh-ready", "credentials", "ca"))
        if failure == "install":
            expected.append("install")
        assert events == [*expected, "drain", "vm-stop", "rollback"]
        assert holder == []


def test_existing_gateway_start_uses_lifecycle_without_smolvm_slirp_command(
    lifecycle_config: gateway.GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ExistingVM:
        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(cli.smolvm.SmolVM, "from_id", lambda _name: ExistingVM())
    monkeypatch.setattr(
        cli,
        "_start_gateway_vm",
        lambda vm, config, timeout, holder: events.append("gateway-start"),
    )
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm",
        lambda _argv: (_ for _ in ()).throw(AssertionError("SLIRP start must not run")),
    )

    assert (
        cli._start_existing_vm_if_needed(
            "vm1", "stopped", 1, gateway_config=lifecycle_config, gateway_holder=[]
        )
        == 0
    )
    assert events == ["gateway-start", "close"]


def test_running_vm_without_live_gateway_is_rejected_without_fallback(
    lifecycle_config: gateway.GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.gateway,
        "control_request",
        lambda *_args: (_ for _ in ()).throw(gateway.GatewayLifecycleError("inactive")),
    )
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm",
        lambda _argv: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    assert (
        cli._start_existing_vm_if_needed("vm1", "running", 1, gateway_config=lifecycle_config) == 1
    )
    assert "running without its configured gateway" in capsys.readouterr().err


def test_running_vm_rejects_live_configuration_drift(
    lifecycle_config: gateway.GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.gateway,
        "control_request",
        lambda *_args: {
            "status": "ready",
            "generation": "generation1",
            "config_digest": "0" * 64,
        },
    )

    assert (
        cli._start_existing_vm_if_needed("vm1", "running", 1, gateway_config=lifecycle_config) == 1
    )
    assert "stop/start required" in capsys.readouterr().err


def test_running_vm_rejects_removed_gateway_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.gateway, "active", lambda _name: True)

    assert cli._start_existing_vm_if_needed("vm1", "running", 1) == 1
    assert "configuration for VM 'vm1' was removed" in capsys.readouterr().err


def test_explicit_stop_cleans_gateway_after_vm_and_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(cli.gateway, "drain", lambda name: events.append(f"drain:{name}"))
    monkeypatch.setattr(cli.gateway, "stop", lambda name: events.append(f"gateway:{name}"))
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm",
        lambda argv, **kwargs: events.append(f"vm:{argv[-1]}:{kwargs['timeout']}") or 0,
    )

    assert cli._stop_gateway("vm1") == 0
    assert events == ["drain:vm1", "vm:vm1:30", "gateway:vm1"]

    events.clear()

    def interrupted(_argv: list[str], **_kwargs: object) -> int:
        events.append("interrupt")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.runtime, "run_smolvm", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli._stop_gateway("vm1")
    assert events == ["drain:vm1", "interrupt", "gateway:vm1"]


MEDIATOR = """
import json, os, signal, sys
ready = {'status':'ready','host':'127.0.0.1','port':34567,'pid':os.getpid(),
         'vm_id':sys.argv[2],'incarnation':sys.argv[3],
         'hardening':'no_new_privs,seccomp,landlock-unavailable,rlimits'}
os.write(int(sys.argv[1]), (json.dumps(ready)+'\\n').encode())
signal.pause()
"""
WORKER = """
import hashlib, hmac, json, os, signal, sys
policy=json.load(open(sys.argv[2]))
token=os.read(int(sys.argv[6]), 32)
message='\\0'.join((sys.argv[3], sys.argv[4], policy['digest'], sys.argv[5])).encode()
print(json.dumps({'status':'ready','version':1,'vm_id':sys.argv[3],
                  'generation':sys.argv[4],'digest':policy['digest'],
                  'incarnation':sys.argv[5],
                  'incarnation_proof':hmac.new(token,message,hashlib.sha256).hexdigest(),
                  'mediator':True,'ingress':1,
                  'hardening':'no_new_privs,seccomp,landlock-unavailable,rlimits'}), flush=True)
print(json.dumps({'status':'network-ready','qemu_pid':os.getppid()}), flush=True)
signal.pause()
"""
WORKER_STATS_ONLY = """
import hashlib, hmac, json, os, sys, time
policy=json.load(open(sys.argv[2]))
token=os.read(int(sys.argv[6]), 32)
message='\\0'.join((sys.argv[3], sys.argv[4], policy['digest'], sys.argv[5])).encode()
print(json.dumps({'status':'ready','version':1,'vm_id':sys.argv[3],
                  'generation':sys.argv[4],'digest':policy['digest'],
                  'incarnation':sys.argv[5],
                  'incarnation_proof':hmac.new(token,message,hashlib.sha256).hexdigest(),
                  'mediator':True,'ingress':1,
                  'hardening':'no_new_privs,seccomp,landlock-unavailable,rlimits'}), flush=True)
while True:
    print('{"status":"stats"}', flush=True)
    time.sleep(0.01)
"""


def direct_controller(
    config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mediator: str = MEDIATOR,
    worker: str = WORKER,
    timeout: float = 2,
    vm_id: str = "vm-direct",
    generation: str = "generation1",
) -> gateway.GatewayController:
    monkeypatch.setattr(
        gateway.GatewayController,
        "_mediator_command",
        lambda self, key, ready, policy, broker: [
            sys.executable,
            "-c",
            mediator,
            str(ready),
            self.vm_id,
            self.worker_incarnation,
            "sbx_gateway_mediator.py",
            str(self.directory),
        ],
    )
    monkeypatch.setattr(
        gateway.GatewayController,
        "_worker_command",
        lambda self, key, token: [
            sys.executable,
            "-c",
            worker,
            str(self.qemu_socket),
            str(self.directory / "policy.json"),
            self.vm_id,
            self.generation,
            self.worker_incarnation,
            str(token),
            "sbx-gateway",
            str(self.directory),
        ],
    )
    return gateway.GatewayController(
        config,
        vm_id,
        root=tmp_path / "state",
        runtime_root=Path("/tmp")
        / f"sbx-test-{hashlib.sha256(os.fsencode(tmp_path)).hexdigest()[:16]}",
        generation=generation,
        timeout=timeout,
    )


def test_worker_stats_cannot_extend_the_readiness_deadline(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = direct_controller(
        lifecycle_config,
        tmp_path,
        monkeypatch,
        worker=WORKER_STATS_ONLY,
        timeout=0.5,
    )
    instance.prepare()
    pids = [process.pid for process in instance.processes.values()]

    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="timed out"):
        instance.vm_network_ready(os.getpid())

    assert time.monotonic() - started < 1
    assert all(not gateway.pid_is_alive(pid) for pid in pids)


def test_persisted_stop_terminates_components_and_preserves_evidence(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    instance = direct_controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    instance.vm_network_ready(os.getpid())
    pids = [process.pid for process in instance.processes.values()]
    evidence = b'{"event":"admitted","request_id":"stop-evidence"}\n'
    audit = instance.directory / "audit.log"
    audit.write_bytes(evidence)
    instance.drain()

    gateway.stop("vm-direct", root=root)

    assert gateway.read_state(instance.directory)["status"] == "stopped"
    assert audit.read_bytes() == evidence
    assert all(not gateway._pid_is_running(pid) for pid in pids)
    instance.close()
    assert audit.read_bytes() == evidence
    gateway.remove("vm-direct", root=root)
    assert not audit.parent.exists()


def test_failed_start_with_audit_evidence_is_not_destructive(
    lifecycle_config: gateway.GatewayConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    directory = instance.directory
    evidence = b'{"event":"admitted","request_id":"startup-evidence"}\n'
    (directory / "audit.log").write_bytes(evidence)
    instance.rollback("startup_failed")
    parent = gateway.PersistentGatewayController(lifecycle_config, "vm1", root=instance.root)
    parent.directory = directory
    parent.rollback("startup_failed")
    assert (directory / "audit.log").read_bytes() == evidence
    assert gateway.read_state(directory)["status"] == "stopped"
    assert gateway.read_state(directory)["last_failure"]["code"] == "startup_failed"
    gateway.remove("vm1", root=instance.root)
    assert not directory.exists()


def test_restart_retains_audit_until_explicit_remove(
    lifecycle_config: gateway.GatewayConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, _ = controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    directory = instance.directory
    assert directory is not None
    evidence = b'{"event":"admitted","request_id":"previous-generation"}\n'
    (directory / "audit.log").write_bytes(evidence)
    instance.close()
    replacement = gateway.PersistentGatewayController(
        lifecycle_config, "vm1", root=instance.root, generation="generation2"
    )
    lock_fd = replacement._acquire_lock()
    try:
        assert (directory / "audit.log").read_bytes() == evidence
        with pytest.raises(gateway.GatewayStateError):
            gateway.remove("vm1", root=instance.root)
        assert directory.exists()
    finally:
        os.close(lock_fd)
    second = gateway.create_state(lifecycle_config, "vm1", "generation2", root=instance.root)
    (second / "audit.log").write_bytes(evidence)
    peer = gateway.create_state(lifecycle_config, "vm2", "peer-generation", root=instance.root)
    gateway.remove("vm1", root=instance.root)
    assert not directory.exists() and not second.exists()
    assert peer.exists()


def test_two_vm_process_groups_and_stop_are_isolated(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = direct_controller(lifecycle_config, tmp_path, monkeypatch, vm_id="vm-direct-a")
    b = direct_controller(
        lifecycle_config,
        tmp_path,
        monkeypatch,
        vm_id="vm-direct-b",
        generation="generation2",
    )
    a.prepare()
    b.prepare()
    a.vm_network_ready(os.getpid())
    b.vm_network_ready(os.getpid())
    processes = [*a.processes.values(), *b.processes.values()]

    assert len({process.pid for process in processes}) == 4
    assert all(os.getpgid(process.pid) == process.pid for process in processes)
    a_pids = [process.pid for process in a.processes.values()]
    b_pids = [process.pid for process in b.processes.values()]
    a.close()
    assert all(not gateway._pid_is_running(pid) for pid in a_pids)
    assert all(gateway._pid_is_running(pid) for pid in b_pids)
    b.check()
    b.close()


def test_direct_subprocess_normal_lifecycle(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = direct_controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    instance.vm_network_ready(os.getpid())
    directory = instance.directory
    pids = [process.pid for process in instance.processes.values()]

    instance.close()

    assert directory is not None and directory.exists()
    assert gateway.read_state(directory)["status"] == "stopped"
    assert all(not gateway.pid_is_alive(pid) for pid in pids)


@pytest.mark.parametrize(
    ("mediator", "worker", "component"),
    [
        ("raise SystemExit(7)", WORKER, "mediator"),
        (MEDIATOR, "raise SystemExit(8)", "worker"),
    ],
)
def test_direct_subprocess_early_exit_fails_closed(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mediator: str,
    worker: str,
    component: str,
) -> None:
    instance = direct_controller(
        lifecycle_config, tmp_path, monkeypatch, mediator=mediator, worker=worker
    )

    with pytest.raises(gateway.GatewayLifecycleError, match=component):
        instance.prepare()

    assert instance.directory is not None and not instance.directory.exists()


def test_control_framing_rejects_truncated_malformed_and_oversized() -> None:
    for payload in (
        b"\x00\x00\x00\x05{}",
        b"\x00\x01\x00\x01",
        b"\x00\x00\x00\x01x",
        b"\x00\x00\x00\x02[]",
    ):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(payload)
            left.shutdown(socket.SHUT_WR)
            with pytest.raises(gateway.GatewayLifecycleError):
                gateway_supervisor.recv_frame(right, 0.1)
    left, right = socket.socketpair()
    with left, right, pytest.raises(gateway.GatewayLifecycleError, match="too large"):
        gateway_supervisor.send_frame(left, {"value": "x" * gateway_supervisor.MAX_FRAME_BYTES})
    if sys.platform == "linux":
        left, right = socket.socketpair()
        with left, right:
            assert gateway_supervisor._peer_uid(right) == os.getuid()


def test_shutdown_reply_failure_propagates_after_preserving_close(monkeypatch):
    client, peer = socket.socketpair()
    closed = []
    controller = SimpleNamespace(
        generation="generation1",
        timeout=1,
        close=lambda: closed.append(True),
    )
    server = SimpleNamespace(accept=lambda: (peer, None))
    with client, peer:
        gateway_supervisor.send_frame(
            client, {"version": 1, "generation": "generation1", "operation": "shutdown"}
        )
        monkeypatch.setattr(gateway_supervisor, "_peer_uid", lambda _: os.getuid())

        def failed_reply(*args):
            raise BrokenPipeError()

        monkeypatch.setattr(gateway_supervisor, "send_frame", failed_reply)
        with pytest.raises(BrokenPipeError):
            gateway_supervisor._serve(controller, server)
    assert closed == [True]


def test_control_rejects_wrong_uid_and_remains_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    uids = iter((os.getuid() + 1, os.getuid(), os.getuid(), os.getuid()))
    monkeypatch.setattr(gateway_supervisor, "_peer_uid", lambda _client: next(uids))
    monkeypatch.setattr(
        gateway_supervisor.gateway,
        "read_state",
        lambda _directory: {
            "status": "ready",
            "config_digest": "0" * 64,
            "counters": {"frames_read": 1},
            "last_failure": None,
        },
    )

    class Controller:
        timeout = 1.0
        generation = "generation1"
        directory = tmp_path

        def close(self) -> None:
            pass

    thread = threading.Thread(target=gateway_supervisor._serve, args=(Controller(), server))
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            try:
                gateway_supervisor.send_frame(
                    client,
                    {"version": 1, "generation": "generation1", "operation": "status"},
                )
                assert "UID mismatch" in gateway_supervisor.recv_frame(client, 1)["error"]
            except (BrokenPipeError, gateway.GatewayLifecycleError):
                pass  # Closing an unauthorized peer before reading is also fail-closed.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            gateway_supervisor.send_frame(
                client,
                {"version": 1, "generation": "generation1", "operation": "status"},
            )
            assert gateway_supervisor.recv_frame(client, 1)["counters"] == {"frames_read": 1}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            gateway_supervisor.send_frame(
                client,
                {"version": 1, "generation": "generation1", "operation": "invalid"},
            )
            assert "unknown" in gateway_supervisor.recv_frame(client, 1)["error"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            gateway_supervisor.send_frame(
                client,
                {"version": 1, "generation": "generation1", "operation": "shutdown"},
            )
            assert gateway_supervisor.recv_frame(client, 1)["status"] == "stopped"
    finally:
        thread.join(2)
        server.close()
    assert not thread.is_alive()


def test_supervisor_run_completes_two_phase_bootstrap(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_directory = gateway._create_runtime_directory("generation1", root=tmp_path / "runtime")
    directory = gateway.create_state(
        lifecycle_config,
        "vm1",
        "generation1",
        root=tmp_path / "state",
        runtime_directory=runtime_directory,
    )
    qemu = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    qemu.bind(str(runtime_directory / "qemu-net.sock"))
    qemu.listen()
    metadata = {
        "config": gateway_supervisor.config_payload(lifecycle_config),
        "requested_config_digest": lifecycle_config.digest("vm1", "generation1"),
        "vm_id": "vm1",
        "generation": "generation1",
        "root": str(tmp_path / "state"),
        "directory": str(directory),
        "runtime_directory": str(runtime_directory),
        "timeout": 1,
        "mac": "52:54:00:12:34:56",
        "placeholders": {},
        "listener_rules": [],
        "qemu_fd": os.dup(qemu.fileno()),
        "listener_fds": [],
    }
    config_read, config_write = os.pipe()
    provider_read, provider_write = os.pipe()
    os.write(config_write, json.dumps(metadata).encode())
    os.write(
        provider_write, b'{"baseline":{"HOME":"/tmp","PATH":"/bin","TZ":"UTC"},"inherited":{}}'
    )
    os.close(config_write)
    os.close(provider_write)
    parent, child = socket.socketpair()
    instances: list[Any] = []

    class Controller:
        def __init__(self, config: object, vm_id: str, **kwargs: object) -> None:
            self.config = config
            self.vm_id = vm_id
            self.generation = str(kwargs["generation"])
            self.timeout = float(kwargs["timeout"])
            self.directory: Path | None = None
            self.runtime_directory: Path | None = None
            self.listener_rules: tuple[gateway.IngressRule, ...] = ()
            self.qemu_listener: socket.socket | None = None
            self.listeners: list[socket.socket] = []
            self.qemu_socket = runtime_directory / "qemu-net.sock"
            instances.append(self)

        def _mediator_policy(self) -> bytes:
            return b'{"version":2,"rules":[],"credentials":[]}'

        def _start_data_plane(self, deadline: float) -> None:
            assert deadline > time.monotonic()

        def _process_ids(self) -> dict[str, int]:
            return {"worker": os.getpid(), "mediator": os.getpid()}

        def vm_network_ready(self, qemu_pid: int, timeout: float) -> None:
            assert (qemu_pid, timeout) == (123, 1)

        def rollback(self, _code: str = "cleanup_failed") -> None:
            if self.qemu_listener is not None:
                self.qemu_listener.close()
            for listener in self.listeners:
                listener.close()

    monkeypatch.setattr(gateway_supervisor.gateway, "GatewayController", Controller)
    monkeypatch.setattr(gateway_supervisor.gateway, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gateway_supervisor, "_serve", lambda controller, _server: controller.rollback()
    )
    monkeypatch.setattr(gateway_supervisor.os, "chdir", lambda _path: None)
    result: list[int] = []
    lock_fd = os.open("/dev/null", os.O_RDONLY)
    thread = threading.Thread(
        target=lambda: result.append(
            gateway_supervisor.run(os.dup(child.fileno()), config_read, provider_read, lock_fd)
        )
    )
    thread.start()
    try:
        assert gateway_supervisor.recv_frame(parent, 2)["status"] == "prepared"
        gateway_supervisor.send_frame(
            parent,
            {
                "version": 1,
                "generation": "generation1",
                "operation": "commit",
                "qemu_pid": 123,
                "timeout": 1,
            },
        )
        assert gateway_supervisor.recv_frame(parent, 2)["status"] == "ready"
        thread.join(2)
        assert result == [0]
        assert instances[0]._mediator_policy_payload.startswith(b'{"version":2')
    finally:
        parent.close()
        child.close()
        qemu.close()
        os.close(lock_fd)
        thread.join(2)
        for name in ("qemu-net.sock", "control.sock"):
            with suppress(FileNotFoundError):
                (runtime_directory / name).unlink()
        with suppress(OSError):
            runtime_directory.rmdir()


def test_supervisor_run_rejects_invalid_inherited_payload(tmp_path: Path) -> None:
    parent, child = socket.socketpair()
    config_read, config_write = os.pipe()
    secret_read, secret_write = os.pipe()
    os.write(config_write, b"[]")
    os.close(config_write)
    os.close(secret_write)
    lock_fd = os.open("/dev/null", os.O_RDONLY)
    try:
        assert (
            gateway_supervisor.run(os.dup(child.fileno()), config_read, secret_read, lock_fd) == 1
        )
        assert "invalid inherited" in gateway_supervisor.recv_frame(parent, 1)["error"]
    finally:
        parent.close()
        child.close()
        os.close(lock_fd)


def test_persistent_bootstrap_uses_fresh_subprocess_and_inherited_provider_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TASK33_TOKEN", "task33-source-value")
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {"id": "web", "mode": "http-mediated", "hosts": ["example.com"], "ports": [443]}
                ],
                "credential": [
                    {
                        "id": "token",
                        "environment": "TOKEN",
                        "method": "command",
                        "options": {"argv": ["get-token"], "inherit_environment": ["TASK33_TOKEN"]},
                        "bindings": [
                            {"policies": ["web"], "header": "Authorization: Bearer {placeholder}"}
                        ],
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    captured: dict[str, Any] = {}

    class BootstrapProcess:
        pid = 999_999
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def wait(self, timeout: float = 0) -> int:
            del timeout
            return self.returncode or 0

    def runner(argv: list[str], **kwargs: Any) -> BootstrapProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        config_fd = int(argv[argv.index("--config-fd") + 1])
        provider_fd = int(argv[argv.index("--provider-fd") + 1])
        captured["config"] = os.pread(config_fd, gateway.MAX_CONFIG_BYTES, 0)
        captured["provider"] = os.pread(provider_fd, gateway.MAX_CONFIG_BYTES, 0)
        bootstrap_fd = int(argv[argv.index("--bootstrap-fd") + 1])
        with socket.socket(fileno=os.dup(bootstrap_fd)) as bootstrap:
            gateway_supervisor.send_frame(
                bootstrap,
                {"version": 1, "generation": "generation1", "status": "prepared"},
            )
        return BootstrapProcess()

    instance = gateway.PersistentGatewayController(
        config,
        "vm1",
        root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
        generation="generation1",
        runner=runner,
        timeout=0.2,
    )
    instance.prepare()
    try:
        assert captured["argv"][:3] == [sys.executable, "-m", "sbx.gateway_supervisor"]
        assert b"task33-source-value" not in captured["config"]
        assert b"task33-source-value" in captured["provider"]
        assert "task33-source-value" not in " ".join(captured["argv"])
        assert "task33-source-value" not in repr(captured["env"])
        assert captured["env"] == {
            "HOME": str(instance.directory),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", os.defpath),
            "TZ": "UTC",
        }
        assert not hasattr(os, "fork") or "fork" not in " ".join(captured["argv"])
    finally:
        instance.rollback()


def test_persistent_spawn_failure_releases_lock_descriptors_and_state(
    lifecycle_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    root = tmp_path / "state"
    before = len(list(Path("/proc/self/fd").iterdir())) if sys.platform == "linux" else 0

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("supervisor spawn failed")

    instance = gateway.PersistentGatewayController(
        lifecycle_config,
        "vm1",
        root=root,
        runtime_root=tmp_path / "runtime",
        generation="generation1",
        runner=fail_spawn,
    )
    with pytest.raises(OSError, match="supervisor spawn failed"):
        instance.prepare()

    abandoned = gateway.create_state(lifecycle_config, "vm1", "abandoned", root=root)
    replacement = gateway.PersistentGatewayController(lifecycle_config, "vm1", root=root)
    lock_fd = replacement._acquire_lock()
    os.close(lock_fd)
    assert not root.joinpath("vm1", "generation1").exists()
    assert not abandoned.exists()
    if sys.platform == "linux":
        assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_persistent_commit_failure_rolls_back_bootstrap(
    lifecycle_config: gateway.GatewayConfig,
) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        gateway.PersistentGatewayController(lifecycle_config, "vm1", timeout=0)
    instance = gateway.PersistentGatewayController(lifecycle_config, "vm1", timeout=1)
    instance._bootstrap, child = socket.socketpair()

    def reject() -> None:
        with child:
            gateway_supervisor.recv_frame(child, 1)
            gateway_supervisor.send_frame(
                child,
                {"version": 1, "generation": "generation1", "error": "network failed"},
            )

    thread = threading.Thread(target=reject)
    thread.start()
    with pytest.raises(gateway.GatewayLifecycleError, match="network failed"):
        instance.vm_network_ready(123, 1)
    thread.join(1)
    assert instance._bootstrap is None


def test_configuration_drift_uses_live_generation_digest(
    lifecycle_config: gateway.GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway,
        "control_request",
        lambda *_args, **_kwargs: {
            "generation": "generation1",
            "config_digest": lifecycle_config.digest("vm1", "generation1"),
        },
    )
    assert not gateway.configuration_drift("vm1", lifecycle_config)
    with_controller_ingress = replace(
        lifecycle_config,
        ingress=(
            *lifecycle_config.ingress,
            gateway.IngressRule(
                "sbx-ssh", "127.0.0.1", 2200, gateway.GUEST_ADDRESS, 22, "controller"
            ),
        ),
    )
    assert not gateway.configuration_drift("vm1", with_controller_ingress)
    changed = replace(lifecycle_config, reconnection=gateway.Reconnection(attempts=2))
    assert gateway.configuration_drift("vm1", changed)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux gateway worker acceptance")
def test_persistent_supervisor_survives_bootstrap_and_drains_data_plane(tmp_path: Path) -> None:
    del tmp_path
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {"id": "discard", "mode": "tcp-forward", "hosts": ["127.0.0.1"], "ports": [9]},
                    {
                        "id": "wildcard",
                        "mode": "tcp-forward",
                        "hosts": ["*.example.com"],
                        "ports": [443],
                    },
                ]
            }
        },
        platform="linux",
    )
    assert config is not None
    with tempfile.TemporaryDirectory(prefix="gw-", dir="/tmp") as raw_root:
        root = Path(raw_root) / "state"
        instance = gateway.PersistentGatewayController(
            config,
            "vm-persistent",
            root=root,
            runtime_root=Path(raw_root) / "runtime",
            generation="generation1",
            timeout=10,
        )
        peer = socket.socket(socket.AF_UNIX)
        try:
            instance.prepare()
            peer.settimeout(10)
            peer.connect(str(instance.qemu_socket))
            peer.sendall((60).to_bytes(4, "big") + bytes(60))
            instance.vm_network_ready(os.getpid(), 10)
            assert gateway.active("vm-persistent", root=root)
            state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
            assert state["pids"]["supervisor"] == instance._process.pid

            for request in (
                {"version": 2, "generation": "generation1", "operation": "status"},
                {"version": 1, "generation": "stale", "operation": "status"},
            ):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                    control.connect(str(instance.runtime_directory / "control.sock"))  # type: ignore[operator]
                    gateway_supervisor.send_frame(control, request)
                    assert "error" in gateway_supervisor.recv_frame(control, 10)
            assert gateway.active("vm-persistent", root=root)

            responses: list[dict[str, Any]] = []
            threads = [
                threading.Thread(
                    target=lambda: responses.append(
                        gateway.control_request("vm-persistent", "status", root=root)
                    )
                )
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            assert len(responses) == len(threads)
            assert {response["generation"] for response in responses} == {"generation1"}

            journal = instance.directory / "dns-mappings.jsonl"
            cached_mapping = {"host": "cached.example.com", "ip": "198.18.0.123"}
            with journal.open("a") as saved:
                saved.write(json.dumps(cached_mapping) + "\n")
                saved.flush()
                os.fsync(saved.fileno())
            saved_dns_state = journal.read_bytes()
            old_mediator = state["pids"]["mediator"]
            os.kill(old_mediator, signal.SIGTERM)
            peer.close()
            peer = socket.socket(socket.AF_UNIX)
            peer.settimeout(10)
            deadline = time.monotonic() + 10
            while True:
                try:
                    peer.connect(str(instance.qemu_socket))
                    peer.sendall((60).to_bytes(4, "big") + bytes(60))
                    break
                except OSError:
                    peer.close()
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
                    peer = socket.socket(socket.AF_UNIX)
                    peer.settimeout(10)
            while time.monotonic() < deadline:
                state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
                if state["status"] == "ready" and state["pids"]["mediator"] != old_mediator:
                    break
                time.sleep(0.05)
            assert state["status"] == "ready"
            assert state["pids"]["mediator"] != old_mediator
            assert state["pids"]["supervisor"] == instance._process.pid
            assert journal.read_bytes() == saved_dns_state

            evidence = b'{"event":"admitted","request_id":"persistent-stop"}\n'
            (instance.directory / "audit.log").write_bytes(evidence)
            instance.drain()
            instance.drain()
            assert peer.recv(1) == b""
        finally:
            peer.close()
            instance.close()
        assert gateway.read_state(root / "vm-persistent" / "generation1")["status"] == "stopped"
        assert (root / "vm-persistent" / "generation1" / "audit.log").read_bytes() == evidence
        gateway.remove("vm-persistent", root=root)
        assert not (root / "vm-persistent" / "generation1").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd lifecycle acceptance")
def test_supervisor_death_closes_data_plane_and_fallback_reclaims_state(tmp_path: Path) -> None:
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {"id": "discard", "mode": "tcp-forward", "hosts": ["127.0.0.1"], "ports": [9]}
                ]
            }
        },
        platform="linux",
    )
    assert config is not None
    del tmp_path
    temporary = Path(tempfile.mkdtemp(prefix="gw-death-", dir="/tmp"))
    root = temporary / "state"
    instance = gateway.PersistentGatewayController(
        config,
        "vm-supervisor-death",
        root=root,
        runtime_root=temporary / "runtime",
        generation="generation1",
        timeout=10,
    )
    peer = socket.socket(socket.AF_UNIX)
    try:
        instance.prepare()
        peer.settimeout(10)
        peer.connect(str(instance.qemu_socket))
        peer.sendall((60).to_bytes(4, "big") + bytes(60))
        instance.vm_network_ready(os.getpid(), 10)
        state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
        owned = [state["pids"][name] for name in ("supervisor", "worker", "mediator")]

        os.kill(state["pids"]["supervisor"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(gateway._pid_is_running(pid) for pid in owned):
            time.sleep(0.05)
        assert all(not gateway._pid_is_running(pid) for pid in owned)
        assert peer.recv(1) == b""
        assert "Gateway state: stale" in gateway.diagnostic_lines(
            "vm-supervisor-death", configured=config, root=root
        )
    finally:
        peer.close()
        instance.close()
    assert gateway.read_state(root / "vm-supervisor-death" / "generation1")["status"] == "stopped"
    gateway.remove("vm-supervisor-death", root=root)
    assert not root.joinpath("vm-supervisor-death", "generation1").exists()
    shutil.rmtree(temporary)


def test_darwin_orphan_cleanup_refuses_before_signaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    with pytest.raises(gateway.GatewayStateError, match="manual cleanup"):
        gateway._pidfd_for_owned_process(os.getpid(), "worker", tmp_path)
