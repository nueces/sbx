import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from smolvm.presets import Preset
from smolvm.types import PortForwardConfig

from sbx import guest_setup, smolvm_preset, vm_metadata
from sbx.runtime import ConfigError


@pytest.fixture
def preset_sdk(monkeypatch, tmp_path):
    events = []
    captured = SimpleNamespace(events=events, failure=None, build=None, updates=None)
    real = tmp_path / "real-cache"
    real.mkdir()
    link = tmp_path / "temporary-home-cache"
    link.symlink_to(real, target_is_directory=True)
    for name in ("kernel", "rootfs", "private-key"):
        (real / name).write_text("fixture")
    captured.real, captured.link = real, link

    class Config:
        env_vars = {}
        kernel_path = link / "kernel"
        rootfs_path = link / "rootfs"
        initrd_path = None

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
        return Config(), str(link / "private-key")

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
        name="pi",
        summary="test preset",
        install_script="install",
        setup_script="unnecessary OS package installation",
        host_env_vars=("OPENAI_API_KEY",),
    )
    monkeypatch.setattr(smolvm_preset, "get_preset", lambda name: captured.preset)
    monkeypatch.setattr(smolvm_preset, "_build_base_config", build)
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
        guest_os="debian",
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
        memory=memory or 2048,
        disk_size_mib=disk,
        default_disk_size_mib=8192,
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
        ssh_key_path=str(preset_sdk.real / "private-key"),
        mounts=["/host:/guest"],
        writable_mounts=True,
    )


def test_preset_paths_survive_temporary_home_cleanup(preset_sdk):
    create_preset()
    preset_sdk.link.unlink()
    assert preset_sdk.updates["kernel_path"] == preset_sdk.real / "kernel"
    assert preset_sdk.updates["rootfs_path"] == preset_sdk.real / "rootfs"
    assert preset_sdk.updates.get("initrd_path") is None
    assert preset_sdk.updates["kernel_path"].is_file()
    assert preset_sdk.updates["rootfs_path"].is_file()
    assert preset_sdk.construct[1]["ssh_key_path"] == str(preset_sdk.real / "private-key")


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
        "_build_base_config",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    with pytest.raises(RuntimeError, match="build failed"):
        create_preset()
    assert os.environ["ORIGINAL"] == "kept"
    assert "close" not in preset_sdk.events


@pytest.mark.parametrize(
    "entry",
    [
        "OPENAI_API_KEY=literal",
        "OPENAI_API_KEY=${SOURCE}",
        "OPENAI_API_KEY=${MISSING}",
        "OPENAI_API_KEY=",
    ],
)
def test_selected_values_are_installed_before_preset_without_host_reinjection(
    preset_sdk,
    monkeypatch,
    entry,
):
    monkeypatch.setenv("SOURCE", "alias-secret")
    monkeypatch.delenv("MISSING", raising=False)
    values = guest_setup.resolve_env_entries(
        [
            entry,
            "HOME=/guest/home",
            "PATH=/guest/bin",
            "SMOLVM_DATA_DIR=/guest/state",
        ]
    )
    original_host = dict(os.environ)
    host_env = {"HOME": "/isolated", "PATH": "/host/bin", "OPENAI_API_KEY": "wrong-host-key"}
    create_preset(host_env=host_env, env_values=values)
    assert preset_sdk.synced == values
    assert preset_sdk.applied_preset.host_env_vars == ()
    assert preset_sdk.applied_preset.setup_script == ""
    assert preset_sdk.preset.setup_script == "unnecessary OS package installation"
    assert preset_sdk.preset.host_env_vars == ("OPENAI_API_KEY",)
    assert preset_sdk.applied_preset.install_script == preset_sdk.preset.install_script
    expected = {"UNRELATED": "keep", **{k: v for k, v in values.items() if v is not None}}
    assert preset_sdk.install_env == expected
    assert preset_sdk.build_env == preset_sdk.apply_env == host_env
    assert dict(os.environ) == original_host
    assert preset_sdk.session[1]["env_values"] == values


@pytest.fixture
def base_image(tmp_path, monkeypatch):
    image = tmp_path / "images" / "sbx"
    image.mkdir(parents=True)
    for name in ("kernel", "rootfs", "initrd", "private-key", "public-key"):
        (image / name).write_text("fixture", encoding="utf-8")
    manifest = {"kernel": "kernel", "rootfs": "rootfs", "boot_args": "custom boot args"}
    (image / "smolvm-image.json").write_text(json.dumps(manifest), encoding="utf-8")
    link = tmp_path / "temporary-home-images"
    link.symlink_to(image.parent, target_is_directory=True)
    monkeypatch.setattr(smolvm_preset, "resolve_image_dir", lambda: link)
    monkeypatch.setattr(
        smolvm_preset,
        "ensure_ssh_key",
        lambda: (link / "sbx/private-key", link / "sbx/public-key"),
    )
    return SimpleNamespace(image=image, link=link, manifest=manifest)


def base_config(**overrides):
    return smolvm_preset._build_base_config(
        **(
            dict(
                vm_name="demo",
                name_prefix="pi",
                memory=2048,
                disk_size_mib=None,
                default_disk_size_mib=8192,
            )
            | overrides
        )
    )


@pytest.mark.parametrize(
    "image_size,requested,expected",
    [(64, None, 8192), (9000, None, 9000), (64, 16384, 16384)],
)
def test_presets_use_prepared_base_and_fit_its_rootfs(base_image, image_size, requested, expected):
    with (base_image.image / "rootfs").open("r+b") as rootfs:
        rootfs.truncate(image_size * 1024 * 1024)
    config, key = base_config(disk_size_mib=requested)
    base_image.link.unlink()
    assert config.kernel_path == base_image.image / "kernel"
    assert config.rootfs_path == base_image.image / "rootfs"
    assert config.initrd_path is None
    assert config.boot_args == "custom boot args"
    assert config.kernel_path.is_file() and config.rootfs_path.is_file()
    assert config.ssh_public_key == "fixture"
    assert key == str(base_image.image / "private-key")
    assert config.memory == 2048
    assert config.vcpu_count == 1
    assert config.disk_size_mib == expected
    assert config.grow_filesystem is (expected > image_size)


def test_presets_load_optional_initrd_and_default_boot_args(base_image):
    manifest = base_image.manifest | {"initrd": "initrd"}
    del manifest["boot_args"]
    (base_image.image / "smolvm-image.json").write_text(json.dumps(manifest), encoding="utf-8")
    config, _ = base_config()
    assert config.initrd_path == base_image.image / "initrd"
    assert config.boot_args == smolvm_preset.BOOT_ARGS


def test_missing_prepared_base_fails_without_stock_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(smolvm_preset, "resolve_image_dir", lambda: tmp_path)
    monkeypatch.setattr(smolvm_preset, "ensure_ssh_key", lambda: pytest.fail("unexpected keys"))
    with pytest.raises(ConfigError, match="sbx image build"):
        base_config()


@pytest.mark.parametrize(
    "problem,match",
    [
        ("missing-manifest", "manifest not found"),
        ("invalid-json", "invalid image manifest JSON"),
        ("kernel", "image kernel not found"),
        ("rootfs", "image rootfs not found"),
        ("initrd", "image initrd not found"),
        ("boot_args", "boot_args.*string"),
        ("small-disk", "smaller than the local image rootfs"),
        ("unknown-size", "cannot determine image rootfs size"),
    ],
)
def test_invalid_prepared_base_fails_before_vm_creation(base_image, monkeypatch, problem, match):
    manifest_path = base_image.image / "smolvm-image.json"
    if problem == "missing-manifest":
        manifest_path.unlink()
    elif problem == "invalid-json":
        manifest_path.write_text("{", encoding="utf-8")
    elif problem in {"kernel", "rootfs", "initrd"}:
        (base_image.image / problem).unlink()
        if problem == "initrd":
            manifest_path.write_text(
                json.dumps(base_image.manifest | {"initrd": "initrd"}), encoding="utf-8"
            )
    elif problem == "boot_args":
        manifest_path.write_text(
            json.dumps(base_image.manifest | {"boot_args": []}), encoding="utf-8"
        )
    elif problem == "small-disk":
        with (base_image.image / "rootfs").open("r+b") as rootfs:
            rootfs.truncate(9000 * 1024 * 1024)
    else:
        monkeypatch.setattr(smolvm_preset.lifecycle_warnings, "rootfs_size_mib", lambda path: None)
    monkeypatch.setattr(smolvm_preset, "ensure_ssh_key", lambda: pytest.fail("unexpected keys"))
    with pytest.raises(ConfigError, match=match):
        base_config(disk_size_mib=8192)


def test_opencode_uses_existing_installer_without_an_sdk_preset(preset_sdk, monkeypatch):
    monkeypatch.setattr(
        smolvm_preset, "get_preset", lambda name: pytest.fail("SDK has no OpenCode")
    )
    calls = []
    monkeypatch.setattr(
        guest_setup, "install_runtime_tools", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    create_preset(preset_name="opencode", run_user="agent", install_timeout=900)
    assert calls == [(("demo", "agent", ["opencode"], 900), {"guest": preset_sdk.guest})]
    assert "apply" not in preset_sdk.events
    assert preset_sdk.build["default_disk_size_mib"] == 8192
    assert preset_sdk.build["memory"] == 2048


def test_presets_reject_other_os_without_stock_fallback(preset_sdk):
    with pytest.raises(ConfigError, match="prepared Debian sbx base image"):
        create_preset(guest_os="alpine")
    assert preset_sdk.build is None
    assert not preset_sdk.events
