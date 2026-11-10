import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from smolvm.presets import Preset
from smolvm.types import PortForwardConfig

from sbx import guest_setup, smolvm_preset, vm_metadata


@pytest.fixture
def preset_sdk(monkeypatch, tmp_path):
    events = []
    captured = SimpleNamespace(events=events, failure=None, build=None, updates=None)

    class Config:
        env_vars = {}

        def model_copy(self, *, update):
            captured.updates = update
            return self

    class VM:
        vm_id = "demo"
        info = SimpleNamespace(config=Config())

        def start(self, **kwargs):
            pytest.fail("facade startup would mount before account creation")

        def refresh(self):
            events.append("refresh")

        def close(self):
            events.append("close")

    vm = VM()
    vm.env_state = {"UNRELATED": "keep", "OPENAI_API_KEY": "stale"}
    captured.vm = vm
    captured.guest = SimpleNamespace(
        vm=vm, mounts=[], phase=lambda name, *args, **kwargs: events.append(name)
    )

    def build(**kwargs):
        captured.build = kwargs
        captured.build_env = dict(os.environ)
        events.append("build")
        return Config(), "private-key"

    def construct(*args, **kwargs):
        captured.construct = (args, kwargs)
        events.append("construct")
        return vm

    @contextmanager
    def session(*args, **kwargs):
        events.append("session")
        captured.session = (args, kwargs)
        yield captured.guest
        events.append("session-close")

    def apply(channel, preset, **kwargs):
        assert channel is captured.guest
        captured.applied_preset = preset
        captured.install_env = dict(vm.env_state)
        captured.apply_env = dict(os.environ)
        captured.apply_options = kwargs
        events.append("apply")
        if captured.failure:
            raise captured.failure

    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    captured.preset = Preset(
        name="pi", summary="test preset", install_script="install",
        host_env_vars=("OPENAI_API_KEY",),
    )
    monkeypatch.setattr(smolvm_preset, "get_preset", lambda name: captured.preset)
    monkeypatch.setattr(smolvm_preset, "_build_auto_config", build)
    monkeypatch.setattr(smolvm_preset, "SmolVM", construct)
    monkeypatch.setattr(smolvm_preset, "apply_preset", apply)
    monkeypatch.setattr(guest_setup, "bootstrap_session", session)
    monkeypatch.setattr(
        guest_setup, "start_vm", lambda *args, **kwargs: events.append("manager-start")
    )
    def sync(vm_id, values, **kwargs):
        events.append("env")
        captured.synced = dict(values)
        for name, value in values.items():
            if value is None:
                vm.env_state.pop(name, None)
            else:
                vm.env_state[name] = value

    monkeypatch.setattr(guest_setup, "sync_forwarded_env", sync)
    monkeypatch.setattr(guest_setup, "host_timezone", lambda: "UTC")
    return captured


def create_preset(**overrides):
    options = dict(
        preset_name="pi",
        vm_name=None,
        guest_os="ubuntu",
        cpus=None,
        memory_mib=None,
        disk_size_mib=None,
        mounts=[],
        writable_mounts=False,
        port_forwards=[],
        boot_timeout=60,
        install_timeout=600,
        host_env={"HOME": "/isolated"},
        run_user="agent",
        env_values={},
    )
    return smolvm_preset.create_preset(**(options | overrides))


@pytest.mark.parametrize("memory,disk,cpus", [(None, None, None), (4096, 16384, 4)])
def test_create_preset_orders_account_environment_install_and_mounts(
    preset_sdk, monkeypatch, memory, disk, cpus
):
    monkeypatch.setenv("ORIGINAL", "kept")
    result = create_preset(
        preset_name="claude",
        vm_name="demo",
        memory_mib=memory,
        disk_size_mib=disk,
        cpus=cpus,
        mounts=["/host:/guest"],
        writable_mounts=True,
        port_forwards=[{"host_address": "0.0.0.0", "host_port": 3000, "guest_port": 30}],
        boot_timeout=75,
        install_timeout=900,
        host_env={"HOME": "/isolated", "ALLOWED_SECRET": "secret"},
        before_provision=lambda name: preset_sdk.events.append("claim"),
    )
    assert result is preset_sdk.vm
    assert preset_sdk.build == dict(
        vm_name="demo",
        name_prefix="claude",
        os="ubuntu",
        backend="qemu",
        memory=memory or 2048,
        disk_size_mib=disk or 8192,
        ssh_key_path=None,
    )
    assert preset_sdk.events == [
        "build",
        "construct",
        "claim",
        "manager-start",
        "refresh",
        "session",
        "account",
        "hostname",
        "clock",
        "env",
        "apply",
        "mounts",
        "session-close",
    ]
    assert preset_sdk.apply_options == {"install_timeout": 900}
    assert (
        preset_sdk.build_env
        == preset_sdk.apply_env
        == {"HOME": "/isolated", "ALLOWED_SECRET": "secret"}
    )
    assert os.environ["ORIGINAL"] == "kept"
    assert "ALLOWED_SECRET" not in os.environ
    assert preset_sdk.updates["port_forwards"] == [
        PortForwardConfig(host_address="0.0.0.0", host_port=3000, guest_port=30)
    ]
    assert preset_sdk.updates.get("vcpu_count") == cpus
    assert preset_sdk.updates["comm_channel"] == "ssh"
    assert preset_sdk.updates["ssh_capable"] is True
    assert preset_sdk.construct[1] == dict(
        ssh_key_path="private-key", mounts=["/host:/guest"], writable_mounts=True
    )


@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_create_preset_closes_and_restores_environment_on_failure(
    preset_sdk, monkeypatch, capsys, failure
):
    monkeypatch.setenv("ORIGINAL", "kept")
    preset_sdk.failure = failure
    with pytest.raises(type(failure)):
        create_preset(host_env={"HOME": "/isolated", "SECRET": "hidden"})
    assert preset_sdk.events[-1] == "close"
    assert preset_sdk.events.count("close") == 1
    assert "mounts" not in preset_sdk.events
    assert os.environ["ORIGINAL"] == "kept"
    assert "SECRET" not in os.environ
    output = capsys.readouterr()
    assert "hidden" not in output.out + output.err


def test_create_preset_restores_environment_on_config_failure(preset_sdk, monkeypatch):
    monkeypatch.setenv("ORIGINAL", "kept")
    monkeypatch.setattr(
        smolvm_preset,
        "_build_auto_config",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    with pytest.raises(RuntimeError, match="build failed"):
        create_preset()
    assert os.environ["ORIGINAL"] == "kept"
    assert "close" not in preset_sdk.events


@pytest.mark.parametrize("entry", [
    "OPENAI_API_KEY=literal", "OPENAI_API_KEY=${SOURCE}",
    "OPENAI_API_KEY=${MISSING}", "OPENAI_API_KEY=",
])
def test_selected_values_are_installed_before_preset_without_host_reinjection(
    preset_sdk, monkeypatch, entry,
):
    monkeypatch.setenv("SOURCE", "alias-secret")
    monkeypatch.delenv("MISSING", raising=False)
    values = guest_setup.resolve_env_entries([
        entry, "HOME=/guest/home", "PATH=/guest/bin", "SMOLVM_DATA_DIR=/guest/state",
    ])
    original_host = dict(os.environ)
    host_env = {"HOME": "/isolated", "PATH": "/host/bin", "OPENAI_API_KEY": "wrong-host-key"}
    create_preset(host_env=host_env, env_values=values)
    assert preset_sdk.synced == values
    assert preset_sdk.applied_preset.host_env_vars == ()
    assert preset_sdk.preset.host_env_vars == ("OPENAI_API_KEY",)
    assert preset_sdk.applied_preset.install_script == preset_sdk.preset.install_script
    expected = {"UNRELATED": "keep", **{k: v for k, v in values.items() if v is not None}}
    assert preset_sdk.install_env == expected
    assert preset_sdk.build_env == preset_sdk.apply_env == host_env
    assert dict(os.environ) == original_host
    assert preset_sdk.session[1]["env_values"] == values
