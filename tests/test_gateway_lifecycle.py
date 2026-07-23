import json
import os
import socket
import subprocess
import sys
import time
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

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    next_pid = 900_000

    def __init__(self, payload: bytes = b"", *, exited: bool = False) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.returncode = 1 if exited else None
        self.resources: list[Any] = []

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

    def _close_resources(self) -> None:
        for resource in self.resources:
            resource.close()
        self.resources.clear()


class FakeRunner:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[list[str]] = []
        self.passed_fds: list[int] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append(argv)
        self.passed_fds.extend(kwargs["pass_fds"])
        component = "mediator" if len(self.calls) == 1 else "worker"
        if self.failure == f"{component}_spawn":
            raise OSError(f"{component} spawn failed")
        if component == "mediator":
            process = FakeProcess(exited=self.failure == "mediator_exit")
            ready_fd = kwargs["pass_fds"][1]
            process.resources.append(os.fdopen(os.dup(kwargs["pass_fds"][2]), "rb", buffering=0))
            if self.failure == "mediator_timeout":
                process.resources.append(os.fdopen(os.dup(ready_fd), "wb", buffering=0))
            elif self.failure != "mediator_exit":
                payload: object = {
                    "status": "ready",
                    "host": "0.0.0.0" if self.failure == "mediator_identity" else "127.0.0.1",
                    "port": 1234,
                    "pid": process.pid,
                    "vm_id": argv[-2],
                    "incarnation": argv[-1],
                }
                os.write(ready_fd, json.dumps(payload).encode() + b"\n")
        else:
            policy = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
            ready = {
                "status": "ready",
                "version": 1,
                "vm_id": "wrong-vm" if self.failure == "worker_identity" else argv[-2],
                "generation": argv[-1],
                "digest": policy["digest"],
                "mediator": True,
            }
            payload = json.dumps(ready).encode() + b'\n{"status":"network-ready"}\n'
            if self.failure == "worker_status":
                payload = b'{"status":"wrong"}\n'
            process = FakeProcess(payload, exited=self.failure == "worker_exit")
            if self.failure != "worker_socket":
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(argv[argv.index("--socket") + 1])
                process.resources.append(listener)
        self.processes.append(process)
        return process


def controller(
    config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None = None,
    listener_fail: bool = False,
) -> tuple[gateway.GatewayController, FakeRunner, list[FakeListener]]:
    runner = FakeRunner(failure)
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
        lambda self, key: [
            "fake-sbx-gateway",
            "--socket",
            str(self.qemu_socket),
            "--config",
            str(self.directory / "policy.json"),
            "--capability-key-fd",
            str(key),
            "--mediator-address",
            str(self.mediator_address),
            self.vm_id,
            self.generation,
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


def test_fake_runner_and_socket_normal_lifecycle(
    lifecycle_config: gateway.GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, runner, listeners = controller(lifecycle_config, tmp_path, monkeypatch)

    instance.prepare()
    state = gateway.read_state(instance.directory)  # type: ignore[arg-type]
    assert [call[0] for call in runner.calls] == ["fake-mediator", "fake-sbx-gateway"]
    assert state["status"] == "starting"
    assert state["listeners"][0]["host_port"] == 45123
    assert instance.qemu_socket.is_socket()
    with pytest.raises(OSError):
        os.fstat(runner.passed_fds[-1])

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
        ("worker_socket", "did not create"),
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
         'vm_id':sys.argv[2],'incarnation':sys.argv[3]}
os.write(int(sys.argv[1]), (json.dumps(ready)+'\\n').encode())
signal.pause()
"""
WORKER = """
import json, signal, socket, sys
listener=socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[1])
policy=json.load(open(sys.argv[2]))
print(json.dumps({'status':'ready','version':1,'vm_id':'vm-direct',
                  'generation':'generation1','digest':policy['digest'],'mediator':True}),
      flush=True)
print('{"status":"network-ready"}', flush=True)
signal.pause()
"""
WORKER_STATS_ONLY = """
import json, socket, sys, time
listener=socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[1])
policy=json.load(open(sys.argv[2]))
print(json.dumps({'status':'ready','version':1,'vm_id':'vm-direct',
                  'generation':'generation1','digest':policy['digest'],'mediator':True}),
      flush=True)
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
        lambda self, key: [
            sys.executable,
            "-c",
            worker,
            str(self.qemu_socket),
            str(self.directory / "policy.json"),
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
