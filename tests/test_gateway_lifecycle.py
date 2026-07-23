import hashlib
import hmac
import json
import os
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sbx import cli, gateway


@pytest.fixture
def lifecycle_config() -> gateway.GatewayConfig:
    config = gateway.parse_config(
        {
            "network": {
                "http": [{"host": "example.com", "ports": [443]}],
                "ingress": [{"listen": "127.0.0.1:0", "guest_port": 3000}],
            }
        },
        platform="linux",
    )
    assert config is not None
    return config


class FakeListener:
    def __init__(self, *args: object, fail: bool = False) -> None:
        del args
        self.fail = fail
        self.closed = False
        self.address: tuple[str, int] = ("127.0.0.1", 45123)

    def bind(self, address: tuple[str, int]) -> None:
        if self.fail:
            raise OSError("bind failed")
        self.address = (address[0], address[1] or 45123)

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
    def __init__(
        self, failure: str | None = None, replacement_failure: str | None = None
    ) -> None:
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
            process = FakeProcess(exited=behavior == "mediator_exit")
            ready_fd = kwargs["pass_fds"][1]
            process.resources.append(os.fdopen(os.dup(kwargs["pass_fds"][2]), "rb", buffering=0))
            if behavior == "mediator_timeout":
                process.resources.append(os.fdopen(os.dup(ready_fd), "wb", buffering=0))
            elif behavior != "mediator_exit":
                payload: object = {
                    "status": "ready",
                    "host": "0.0.0.0" if behavior == "mediator_identity" else "127.0.0.1",
                    "port": 1234,
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
) -> tuple[gateway.GatewayController, FakeRunner, list[FakeListener]]:
    runner = FakeRunner(failure, replacement_failure)
    listeners: list[FakeListener] = []

    def socket_factory(*args: object) -> FakeListener:
        listener = FakeListener(*args, fail=listener_fail)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(
        gateway.GatewayController,
        "_mediator_command",
        lambda self, key, ready, policy: [
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
        "vm1",
        root=tmp_path / "state",
        generation="generation1",
        runner=runner,
        socket_factory=socket_factory,  # type: ignore[arg-type]
        timeout=0.1,
    )
    return instance, runner, listeners


def test_file_backed_mediator_policy_is_loaded_once_and_secret_free_on_disk(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "token"
    secret.write_text("task16-file-secret\n")
    config = gateway.parse_config(
        {
            "network": {
                "http": [{"host": "example.com", "ports": [443]}],
                "secret": [
                    {
                        "name": "TOKEN",
                        "provider": f"file:{secret}",
                        "hosts": ["example.com"],
                        "ports": [443],
                        "header": "Authorization: Bearer {placeholder}",
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    instance = gateway.GatewayController(config, "vm1", root=tmp_path / "state")

    policy = instance._mediator_policy()

    assert b"task16-file-secret" in policy
    instance.directory = gateway.create_state(config, "vm1", "generation1", root=tmp_path / "state")
    assert b"task16-file-secret" not in (instance.directory / "policy.json").read_bytes()
    instance.close()

    secret.write_bytes(b"\xff")
    invalid = gateway.GatewayController(config, "vm1", root=tmp_path / "invalid")
    with pytest.raises(gateway.GatewayLifecycleError, match="provider is unavailable"):
        invalid._mediator_policy()
    secret.unlink()
    secret.mkdir()
    with pytest.raises(gateway.GatewayLifecycleError, match="provider is unavailable"):
        invalid._mediator_policy()


def test_command_and_controller_endpoint_failures_are_closed_and_classified(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway.GatewayController(lifecycle_config, "vm1", root=tmp_path / "state")
    instance.directory = gateway.create_state(
        lifecycle_config, "vm1", "generation1", root=tmp_path / "state"
    )
    monkeypatch.setenv("SBX_GATEWAY_MEDIATOR_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("SBX_GATEWAY_MEDIATOR_LAUNCHER", str(tmp_path / "missing-launcher"))
    monkeypatch.setenv("SBX_GATEWAY_BIN", str(tmp_path / "missing-worker"))
    with pytest.raises(gateway.GatewayLifecycleError, match="mediator runtime is missing"):
        instance._mediator_command(1, 2, 3)
    with pytest.raises(gateway.GatewayLifecycleError, match="worker artifact is missing"):
        instance._worker_command(None, 4)

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


def test_logs_and_artifact_overrides_are_private_bounded_and_safe(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway.GatewayController(lifecycle_config, "vm1", root=tmp_path / "state")
    instance.directory = gateway.create_state(
        lifecycle_config, "vm1", "generation1", root=tmp_path / "state"
    )
    log = instance._open_log("worker.log")
    assert stat.S_IMODE(os.fstat(log.fileno()).st_mode) == 0o600
    log.close()
    (instance.directory / "oversized.log").write_bytes(b"")
    os.truncate(instance.directory / "oversized.log", gateway.MAX_LOG_BYTES)
    with pytest.raises(gateway.GatewayLifecycleError, match="log limit"):
        instance._open_log("next.log")

    unsafe = tmp_path / "unsafe-worker"
    unsafe.write_text("worker")
    unsafe.chmod(0o777)
    monkeypatch.setenv("SBX_GATEWAY_BIN", str(unsafe))
    with pytest.raises(gateway.GatewayLifecycleError, match="missing or unsafe"):
        instance._worker_command(None, 4)
    monkeypatch.setenv("SBX_GATEWAY_BIN", "relative-worker")
    with pytest.raises(gateway.GatewayLifecycleError, match="missing or unsafe"):
        instance._worker_command(None, 4)
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
    monkeypatch.setenv("SBX_GATEWAY_MEDIATOR_PYTHON", str(python))
    monkeypatch.setenv("SBX_GATEWAY_MEDIATOR_LAUNCHER", str(launcher))
    instance = gateway.GatewayController(
        lifecycle_config,
        "vm1",
        root=tmp_path / "state",
        generation="generation1",
    )
    instance.directory = gateway.create_state(
        lifecycle_config, "vm1", "generation1", root=tmp_path / "state"
    )
    instance._reserve_qemu_listener()
    instance._reserve_listeners()
    instance.mediator_address = "127.0.0.1:1234"

    mediator = instance._mediator_command(11, 12, 13)
    worker_command = instance._worker_command(14, 15)

    assert mediator[:2] == [str(python), str(launcher)]
    assert mediator[mediator.index("--incarnation") + 1] == instance.worker_incarnation
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
    instance.close()
    assert directory is not None and not directory.exists()
    assert listeners[0].closed
    assert all(process.poll() is not None for process in runner.processes)
    for kwargs in runner.kwargs:
        assert set(kwargs["env"]) == {"HOME", "LANG", "PATH", "TZ"}
        assert not set(kwargs["env"]) & {"LD_PRELOAD", "PYTHONPATH", "HTTP_PROXY"}
        assert kwargs["close_fds"] is not False if "close_fds" in kwargs else True


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


@pytest.mark.parametrize(
    "replacement_failure", ["worker_stale", "worker_identity", "worker_proof"]
)
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


def test_disabled_recovery_and_mediator_loss_fail_closed(
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

    failed, runner, failed_listeners = controller(lifecycle_config, tmp_path, monkeypatch)
    failed.prepare()
    failed.processes["mediator"].crash()
    with pytest.raises(gateway.GatewayLifecycleError, match="mediator exited"):
        failed.check()
    with pytest.raises(gateway.GatewayLifecycleError, match="mediator exited"):
        failed.check()
    assert failed_listeners[0].closed
    assert not failed.qemu_socket.exists()
    assert runner.worker_starts == 1
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
        self._info = type("Info", (), {"config": config})()

    def stop(self) -> None:
        self.events.append("vm-stop")


class FakeController:
    directory = Path("/fake/state")
    qemu_socket = Path("/fake/state/qemu-net.sock")
    mac = "52:54:00:12:34:56"

    def __init__(self, _config: object, _vm_id: str, events: list[str]) -> None:
        self.events = events

    def prepare(self) -> None:
        self.events.append("prepare")

    def vm_network_ready(self, _pid: int) -> None:
        self.events.append("network-ready")

    def check(self) -> None:
        self.events.append("check")

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
    monkeypatch.setattr(cli.gateway, "GatewayController", lambda *_args: fake)

    def start_vm(*_args: object, **_kwargs: object) -> int:
        events.append("qemu")
        if failure == "qemu":
            raise RuntimeError("qemu failed")
        return 123

    def network_ready(pid: int) -> None:
        assert pid == 123
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

    if failure is None:
        cli._start_gateway_vm(vm, lifecycle_config, 1, holder)  # type: ignore[arg-type]
        assert events == ["prepare", "qemu", "network-ready", "ca", "install", "check"]
        assert holder == [fake]
    else:
        with pytest.raises(RuntimeError, match="failed"):
            cli._start_gateway_vm(vm, lifecycle_config, 1, holder)  # type: ignore[arg-type]
        expected = ["prepare", "qemu"]
        if failure not in {"qemu"}:
            expected.append("network-ready")
        if failure not in {"qemu", "network"}:
            expected.append("ca")
        if failure == "install":
            expected.append("install")
        assert events == [*expected, "vm-stop", "rollback"]
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
    monkeypatch.setattr(cli.gateway, "active", lambda _name: False)
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm",
        lambda _argv: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    assert (
        cli._start_existing_vm_if_needed("vm1", "running", 1, gateway_config=lifecycle_config) == 1
    )
    assert "running without its configured gateway" in capsys.readouterr().err


def test_explicit_stop_cleans_gateway_after_vm_and_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(cli.gateway, "stop", lambda name: events.append(f"gateway:{name}"))
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm",
        lambda argv, **kwargs: events.append(f"vm:{argv[-1]}:{kwargs['timeout']}") or 0,
    )

    assert cli._stop_gateway("vm1") == 0
    assert events == ["vm:vm1:30", "gateway:vm1"]

    events.clear()

    def interrupted(_argv: list[str], **_kwargs: object) -> int:
        events.append("interrupt")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.runtime, "run_smolvm", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli._stop_gateway("vm1")
    assert events == ["interrupt", "gateway:vm1"]


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
) -> gateway.GatewayController:
    monkeypatch.setattr(
        gateway.GatewayController,
        "_mediator_command",
        lambda self, key, ready, policy: [
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
        "vm-direct",
        root=tmp_path / "state",
        generation="generation1",
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
        timeout=0.2,
    )
    instance.prepare()
    pids = [process.pid for process in instance.processes.values()]

    started = time.monotonic()
    with pytest.raises(gateway.GatewayLifecycleError, match="timed out"):
        instance.vm_network_ready(os.getpid())

    assert time.monotonic() - started < 1
    assert all(not gateway.pid_is_alive(pid) for pid in pids)


def test_persisted_stop_terminates_components_and_removes_state(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    instance = direct_controller(lifecycle_config, tmp_path, monkeypatch)
    instance.prepare()
    instance.vm_network_ready(os.getpid())
    pids = [process.pid for process in instance.processes.values()]

    gateway.stop("vm-direct", root=root)

    assert not root.joinpath("vm-direct", "generation1").exists()
    assert all(not gateway._pid_is_running(pid) for pid in pids)
    instance.close()


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

    assert directory is not None and not directory.exists()
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
