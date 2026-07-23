import os
from types import SimpleNamespace

import pytest
from smolvm.types import PortForwardConfig

from sbx import smolvm_preset


class FakeConfig:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.updates: dict[str, object] = {}

    def model_copy(self, *, update: dict[str, object]) -> "FakeConfig":
        self.events.append("update")
        self.updates = update
        return self


class FakeVM:
    vm_id = "demo"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self, *, boot_timeout: float) -> None:
        self.events.append(f"start:{boot_timeout:g}")

    def wait_for_ssh(self, *, timeout: float) -> None:
        self.events.append(f"wait:{timeout:g}")

    def _ensure_ssh_for_env(self) -> str:
        self.events.append("channel")
        return "channel"

    def close(self) -> None:
        self.events.append("close")


def create_preset(**overrides: object) -> object:
    options = {
        "preset_name": "pi",
        "vm_name": None,
        "guest_os": "ubuntu",
        "cpus": None,
        "memory_mib": None,
        "disk_size_mib": None,
        "mounts": [],
        "writable_mounts": False,
        "port_forwards": [],
        "boot_timeout": 60,
        "install_timeout": 600,
        "host_env": {"HOME": "/isolated"},
    }
    return smolvm_preset.create_preset(**(options | overrides))


@pytest.mark.parametrize(
    ("memory", "disk", "cpus", "expected_memory", "expected_disk"),
    [(None, None, None, 2048, 8192), (4096, 16384, 4, 4096, 16384)],
)
def test_create_preset(
    monkeypatch: pytest.MonkeyPatch,
    memory: int | None,
    disk: int | None,
    cpus: int | None,
    expected_memory: int,
    expected_disk: int,
) -> None:
    events: list[str] = []
    captured: dict[str, object] = {}
    config = FakeConfig(events)
    vm = FakeVM(events)
    monkeypatch.setenv("ORIGINAL", "kept")

    def get_preset(name: str) -> SimpleNamespace:
        captured["preset_name"] = name
        return SimpleNamespace(name=name, default_mem_mib=2048, default_disk_mib=8192)

    def build_auto_config(**kwargs: object) -> tuple[FakeConfig, str]:
        events.append("build")
        captured["build"] = kwargs
        captured["build_env"] = dict(os.environ)
        return config, "private-key"

    def construct_vm(*args: object, **kwargs: object) -> FakeVM:
        events.append("construct")
        captured["construct"] = (args, kwargs)
        return vm

    def apply_preset(channel: object, preset: object, *, install_timeout: int) -> None:
        events.append(f"apply:{install_timeout}")
        captured["apply"] = (channel, preset)
        captured["apply_env"] = dict(os.environ)

    monkeypatch.setattr(smolvm_preset, "get_preset", get_preset)
    monkeypatch.setattr(smolvm_preset, "_build_auto_config", build_auto_config)
    monkeypatch.setattr(smolvm_preset, "SmolVM", construct_vm)
    monkeypatch.setattr(smolvm_preset, "apply_preset", apply_preset)

    result = create_preset(
        preset_name="claude",
        vm_name="demo",
        cpus=cpus,
        memory_mib=memory,
        disk_size_mib=disk,
        mounts=["/host:/guest"],
        writable_mounts=True,
        port_forwards=[
            {"host_address": "0.0.0.0", "host_port": 3000, "guest_port": 30}
        ],
        boot_timeout=75,
        install_timeout=900,
        host_env={"HOME": "/isolated", "ALLOWED_SECRET": "secret"},
    )

    assert result is vm
    assert captured["preset_name"] == "claude-code"
    assert captured["build"] == {
        "vm_name": "demo",
        "name_prefix": "claude",
        "os": "ubuntu",
        "backend": "qemu",
        "memory": expected_memory,
        "disk_size_mib": expected_disk,
        "ssh_key_path": None,
    }
    assert captured["build_env"] == {"HOME": "/isolated", "ALLOWED_SECRET": "secret"}
    assert captured["apply_env"] == {"HOME": "/isolated", "ALLOWED_SECRET": "secret"}
    assert os.environ["ORIGINAL"] == "kept"
    assert "ALLOWED_SECRET" not in os.environ
    assert events == ["build", "update", "construct", "start:75", "wait:75", "channel", "apply:900"]
    assert config.updates.get("vcpu_count") == cpus
    if cpus is None:
        assert "vcpu_count" not in config.updates
    forwards = config.updates["port_forwards"]
    assert isinstance(forwards, list)
    assert forwards[0] == PortForwardConfig(
        host_address="0.0.0.0", host_port=3000, guest_port=30
    )
    assert captured["construct"] == (
        (config,),
        {
            "ssh_key_path": "private-key",
            "mounts": ["/host:/guest"],
            "writable_mounts": True,
        },
    )


@pytest.mark.parametrize("error", [RuntimeError("failed"), KeyboardInterrupt()])
def test_create_preset_closes_and_restores_environment_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
) -> None:
    events: list[str] = []
    vm = FakeVM(events)
    monkeypatch.setenv("ORIGINAL", "kept")
    monkeypatch.setattr(
        smolvm_preset,
        "get_preset",
        lambda name: SimpleNamespace(name=name, default_mem_mib=1, default_disk_mib=1),
    )
    monkeypatch.setattr(
        smolvm_preset,
        "_build_auto_config",
        lambda **kwargs: (FakeConfig(events), "key"),
    )
    monkeypatch.setattr(smolvm_preset, "SmolVM", lambda *args, **kwargs: vm)
    monkeypatch.setattr(
        smolvm_preset,
        "apply_preset",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(type(error)):
        create_preset(host_env={"HOME": "/isolated", "SECRET": "hidden"})

    assert events[-1] == "close"
    assert events.count("close") == 1
    assert os.environ["ORIGINAL"] == "kept"
    assert "SECRET" not in os.environ
    output = capsys.readouterr()
    assert "hidden" not in output.out
    assert "hidden" not in output.err


def test_create_preset_restores_environment_before_vm_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORIGINAL", "kept")
    monkeypatch.setattr(
        smolvm_preset,
        "get_preset",
        lambda name: SimpleNamespace(name=name, default_mem_mib=1, default_disk_mib=1),
    )
    monkeypatch.setattr(
        smolvm_preset,
        "_build_auto_config",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )

    with pytest.raises(RuntimeError, match="build failed"):
        create_preset()

    assert os.environ["ORIGINAL"] == "kept"
