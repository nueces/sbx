import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbx import cli, guest_setup, runtime, vm_metadata, vm_state

_ORIGINAL_HOST_GIT_CONFIG = guest_setup.host_git_config


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.runtime, "DEBUG", False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (tmp_path / "home-config.toml",))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (tmp_path / ".sbx.toml",))
    monkeypatch.setattr(vm_metadata, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vm_metadata, "SBX_VMS_FILE", tmp_path / "state" / "vms.json")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda project_root=None: None)
    monkeypatch.setattr(guest_setup, "set_hostname", lambda *args, **kwargs: None)
    monkeypatch.setattr(guest_setup, "sync_guest_clock", lambda *args, **kwargs: None)
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda preset_name, **kwargs: SimpleNamespace(
            vm_id=kwargs.get("vm_name") or f"{preset_name}-sbx", close=lambda: None
        ),
    )


def install_fake_smolvm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli.runtime, "smolvm_argv", lambda args: ["smolvm", *args])
    return smolvm


def print_smolvm_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli.runtime, "run_smolvm", lambda args, **kwargs: print(" ".join(args)) or 0
    )


def capture_preset(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    *,
    vm_id: str = "vm1",
) -> None:
    def fake_create(preset_name: str, **kwargs: object) -> SimpleNamespace:
        captured["preset"] = (preset_name, kwargs)
        return SimpleNamespace(
            vm_id=vm_id,
            close=lambda: captured.setdefault("preset_closed", True),
        )

    monkeypatch.setattr(cli.smolvm_preset, "create_preset", fake_create)


def test_smolvm_runner_does_not_need_console_script_on_path() -> None:
    assert runtime.smolvm_argv(["doctor"]) == [
        sys.executable,
        "-c",
        "from smolvm.cli.main import main; raise SystemExit(main())",
        "doctor",
    ]


def test_smolvm_runner_suppresses_upstream_version_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.delenv("SBX_SMOLVM_VERSION_NOTICES", raising=False)
    monkeypatch.setattr(runtime, "run", fake_run)

    assert runtime.run_smolvm(["doctor"]) == 0

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["SMOLVM_DISABLE_VERSION_CHECK"] == "1"


def test_smolvm_runner_allows_upstream_version_notice_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setenv("SBX_SMOLVM_VERSION_NOTICES", "true")
    monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
    monkeypatch.setattr(runtime, "run", fake_run)

    assert runtime.run_smolvm(["doctor"]) == 0

    env = captured["env"]
    assert isinstance(env, dict)
    assert "SMOLVM_DISABLE_VERSION_CHECK" not in env


def test_doctor_checks_qemu_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["doctor"])

    assert rc == 0
    assert capfd.readouterr().out == "doctor --backend qemu\n"


def test_doctor_warns_when_config_differs_from_existing_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / ".sbx.toml"
    config.write_text(
        '[sbx]\nname = "the-quest"\ndisk_size = 10240\nmemory = 8192\ncpus = 4\n',
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = doctor ]; then printf \'%s\\n\' "$*"; exit 0; fi\n'
        'if [ "$1" = sandbox ] && [ "$2" = info ]; then\n'
        "  printf '%s\\n' "
        '\'{"data":{"vm":{"status":"stopped","disk_size":81920,'
        '"memory":8192,"vcpus":4}}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli.runtime, "smolvm_argv", lambda args: ["smolvm", *args])

    rc = cli.main(["doctor"])

    assert rc == 0
    out = capfd.readouterr().out
    assert "doctor --backend qemu" in out
    assert "VM 'the-quest' already exists and differs from .sbx.toml" in out
    assert "disk_size: config requests 10240 MiB, existing VM has 81920 MiB" in out
    assert "sbx recreate the-quest --force" in out


def test_doctor_warns_when_local_image_is_larger_than_requested_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "image"
    image.mkdir()
    rootfs = image / "rootfs.ext4"
    with rootfs.open("wb") as fh:
        fh.truncate(80 * 1024 * 1024)
    (image / "smolvm-image.json").write_text(
        '{"kernel":"vmlinux.bin","rootfs":"rootfs.ext4"}', encoding="utf-8"
    )
    (image / "vmlinux.bin").write_text("kernel", encoding="utf-8")
    config = tmp_path / ".sbx.toml"
    config.write_text(
        f'[sbx]\nname = "the-quest"\nimage = "{image}"\ndisk_size = 10\n',
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = doctor ]; then printf \'%s\\n\' "$*"; exit 0; fi\n'
        'if [ "$1" = sandbox ] && [ "$2" = info ]; then\n'
        "  printf '%s\\n' "
        '\'{"data":{"vm":{"status":"stopped","disk_size":10,'
        '"memory":512,"vcpus":2}}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli.runtime, "smolvm_argv", lambda args: ["smolvm", *args])

    rc = cli.main(["doctor"])

    assert rc == 0
    out = capfd.readouterr().out
    assert "configured disk_size is smaller than the local image rootfs" in out
    assert "disk_size: 10 MiB" in out
    assert "local image rootfs: 80 MiB" in out
    assert "Set [sbx].disk_size to at least 80" in out
    assert "or rebuild the configured local image with a rootfs no larger than 10 MiB" in out


def test_run_rejects_non_qemu_backend(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nbackend = "firecracker"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "run", "vm1"])

    assert rc == 2
    assert "other backends are not supported yet" in capsys.readouterr().err


def test_debug_prints_commands_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(vm_state, "smolvm_vms", lambda all_vms=False: [])

    rc = cli.main(["--debug", "ls"])

    captured = capfd.readouterr()
    assert rc == 0
    assert "sbx debug: argv: ['--debug', 'ls']" in captured.err


def test_list_does_not_require_name(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    vm = SimpleNamespace(
        vm_id="vm1",
        status="running",
        config=SimpleNamespace(rootfs_path=Path("/images/debian/rootfs.ext4")),
        network=SimpleNamespace(ssh_host_port=2204),
    )
    monkeypatch.setattr(vm_state, "smolvm_vms", lambda all_vms=False: [vm])
    vm_metadata.record_vm_project(
        "vm1", {"project_root": "/project", "config_path": "/project/.sbx.toml"}
    )

    rc = cli.main(["list"])

    assert rc == 0
    assert capfd.readouterr().out == (
        "NAME  STATUS   PROJECT   IMAGE   SSH\nvm1   running  /project  debian  2204\n"
    )


def test_list_defaults_to_all_and_can_filter_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(vm_state, "smolvm_vms", lambda all_vms=False: calls.append(all_vms) or [])

    assert cli.main(["ls"]) == 0
    assert cli.main(["list", "--running"]) == 0

    assert calls == [True, False]


def test_list_json_uses_null_for_missing_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm = SimpleNamespace(vm_id="vm1", status="stopped", config=None, network=None)
    monkeypatch.setattr(vm_state, "smolvm_vms", lambda all_vms=False: [vm])

    assert cli.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "name": "vm1",
            "status": "stopped",
            "project": None,
            "image": None,
            "ssh_port": None,
        }
    ]


def test_run_help_groups_supported_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    for heading in ("Session:", "Workspace:", "VM resources:", "Configuration and output:"):
        assert heading in out
    assert "--no-attach" in out
    assert "--write-config" in out


def test_shell_uses_configured_default_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    print_smolvm_args(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "shell", "--keep-running"])

    assert rc == 0
    assert capfd.readouterr().out == "sandbox ssh vm1\n"


def test_shell_uses_configured_run_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nrun_user = "agent"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_prepare(vm_id: str, user: str, **kwargs: object) -> None:
        captured["prepare"] = (vm_id, user)

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, kwargs.get("user"), launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(guest_setup, "prepare_run_user", fake_prepare)
    monkeypatch.setattr(guest_setup, "attach", fake_attach)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    rc = cli.main(["--config", str(config), "shell", "--keep-running"])

    assert rc == 0
    assert captured["prepare"] == ("vm1", "agent")
    assert captured["attach"] == ("vm1", "agent", "bash", None)


def test_shell_uses_configured_project_path_as_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[sbx]\nname = "vm1"\nrun_user = "agent"\nproject_path = "{project}"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(guest_setup, "prepare_run_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, kwargs.get("user"), launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(guest_setup, "attach", fake_attach)

    rc = cli.main(["--config", str(config), "shell", "--keep-running"])

    assert rc == 0
    assert captured["attach"] == ("vm1", "agent", "bash", str(project))


def test_shell_root_uses_configured_project_path_as_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[sbx]\nname = "vm1"\nrun_user = "agent"\nproject_path = "{project}"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(guest_setup, "attach", fake_attach)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    rc = cli.main(["--config", str(config), "shell", "--root", "--keep-running"])

    assert rc == 0
    assert captured["attach"] == ("vm1", "bash", str(project))


def test_shell_root_ignores_configured_run_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    print_smolvm_args(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nrun_user = "agent"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "shell", "--root", "--keep-running"])

    assert rc == 0
    assert capfd.readouterr().out == "sandbox ssh vm1\n"


def test_shell_without_name_or_config_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["shell"])

    assert rc == 2
    assert "shell requires a VM name argument or [sbx].name" in capsys.readouterr().err


def test_shell_syncs_env_from_config_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nenv = ["SBX_TOKEN"]\n', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(
        guest_setup, "sync_forwarded_env", lambda *args, **kwargs: calls.append("sync")
    )
    monkeypatch.setattr(
        cli.runtime, "run_smolvm", lambda *args, **kwargs: calls.append("attach") or 0
    )

    assert cli.main(["--config", str(config), "shell", "--keep-running"]) == 0
    assert calls == ["sync", "attach"]


def test_shell_starts_stopped_vm_before_env_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nenv = ["SBX_TOKEN"]\n', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "stopped")
    monkeypatch.setattr(
        cli, "_start_existing_vm_if_needed", lambda *args, **kwargs: calls.append("start") or 0
    )
    monkeypatch.setattr(
        guest_setup, "sync_forwarded_env", lambda *args, **kwargs: calls.append("sync")
    )
    monkeypatch.setattr(
        cli.runtime, "run_smolvm", lambda *args, **kwargs: calls.append("attach") or 0
    )

    assert cli.main(["--config", str(config), "shell", "--keep-running"]) == 0
    assert calls == ["start", "sync", "attach"]


def test_shell_invalid_env_fails_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nenv = ["BAD-NAME"]\n', encoding="utf-8")

    monkeypatch.setattr(
        cli.runtime, "run_smolvm", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError)
    )

    assert cli.main(["--config", str(config), "shell", "--keep-running"]) == 2


def test_run_uses_local_toml_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    local_config = tmp_path / ".sbx.toml"
    local_config.write_text(
        """
[sbx]
agent = "codex"
name = "demo"
mount = [".:/workspace"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (local_config,))
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured, vm_id="demo")

    rc = cli.main(["run"])

    assert rc == 0
    preset_name, preset_kwargs = captured["preset"]
    assert preset_name == "codex"
    assert preset_kwargs["vm_name"] == "demo"
    assert preset_kwargs["mounts"] == [".:/workspace"]
    assert capfd.readouterr().out == "Started 'demo'. Launching codex...\n"


def test_run_supports_all_exposed_options_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    extra_mount = tmp_path / "extra"
    extra_mount.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[sbx]
agent_default_unused = "claude"
agent = "codex"
name = "configured"
memory = 8192
disk_size = 32768
cpus = 4
backend = "qemu"
os = "ubuntu"
mount = ["{extra_mount}", ".:/workspace"]
project_path = "{project}"
writable_mounts = false
install_timeout = 900
boot_timeout = 75
port_forwards = ["8080:80"]
""".strip(),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured, vm_id="configured")

    rc = cli.main(["--config", str(config), "run", "--no-attach"])

    assert rc == 0
    preset_name, preset_kwargs = captured["preset"]
    assert preset_name == "codex"
    assert preset_kwargs["vm_name"] == "configured"
    assert preset_kwargs["memory_mib"] == 8192
    assert preset_kwargs["disk_size_mib"] == 32768
    assert preset_kwargs["cpus"] == 4
    assert preset_kwargs["guest_os"] == "ubuntu"
    assert preset_kwargs["install_timeout"] == 900
    assert preset_kwargs["boot_timeout"] == 75
    assert preset_kwargs["mounts"] == [
        f"{project}:{project}",
        f"{extra_mount}:{extra_mount}",
        ".:/workspace",
    ]
    assert preset_kwargs["writable_mounts"] is True
    assert preset_kwargs["port_forwards"] == [
        {"host_address": "127.0.0.1", "host_port": 8080, "guest_port": 80}
    ]
    assert "Created sandbox 'configured'." in capfd.readouterr().out


def test_run_uses_local_image_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    image_dir = tmp_path / "debian-pi"
    image_dir.mkdir()
    (image_dir / "vmlinux.bin").write_text("kernel", encoding="utf-8")
    (image_dir / "rootfs.ext4").write_text("rootfs", encoding="utf-8")
    (image_dir / "smolvm-image.json").write_text(
        '{"name":"debian-pi","kernel":"vmlinux.bin","rootfs":"rootfs.ext4",'
        '"sbx":{"agent":"pi","launch_command":"pi"}}',
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'''
[sbx]
name = "vm1"
image = "{image_dir}"
copy_host_credentials = true
mount = [".:/workspace"]
'''.strip(),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_start_local_image(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_start_local_image", fake_start_local_image)
    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local images must not install presets")
        ),
    )

    rc = cli.main(["--config", str(config), "run"])

    assert rc == 0
    assert captured["image_dir"] == image_dir
    assert captured["agent"] == "pi"
    assert captured["mounts"] == [".:/workspace"]
    assert captured["attach"] is True
    assert "copy_host_credentials=true" not in capsys.readouterr().err


def test_run_user_from_config_starts_then_attaches_as_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured)

    def fake_prepare(vm_id: str, user: str, **kwargs: object) -> None:
        captured["prepare"] = (vm_id, user)

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, kwargs.get("user"), launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(cli.network, "expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(guest_setup, "prepare_run_user", fake_prepare)
    monkeypatch.setattr(guest_setup, "attach", fake_attach)
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nrun_user = "agent"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "run"])

    assert rc == 0
    assert captured["preset_closed"] is True
    assert captured["prepare"] == ("vm1", "agent")
    assert captured["attach"] == ("vm1", "agent", "pi", None)
    out = capfd.readouterr().out
    assert "Created sandbox 'vm1'." in out
    assert "sandbox stop vm1" in out


def test_host_git_config_copies_only_safe_global_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "user.name": "Ada Lovelace",
        "user.email": "ada@example.test",
        "init.defaultBranch": "main",
    }

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        text: bool,
        capture_output: bool,
    ) -> subprocess.CompletedProcess[str]:
        key = argv[-1]
        value = values.get(key)
        if value is None:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")

    monkeypatch.setattr(guest_setup, "host_git_config", _ORIGINAL_HOST_GIT_CONFIG)
    monkeypatch.setattr(guest_setup.subprocess, "run", fake_run)

    assert guest_setup.host_git_config() == (
        '[user]\n\tname = "Ada Lovelace"\n\temail = "ada@example.test"\n\n'
        '[init]\n\tdefaultBranch = "main"\n'
    )


def test_git_config_defaults_on_for_managed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        guest_setup, "host_git_config", lambda project_root=None: "[user]\n\tname = Test\n"
    )
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(
        guest_setup,
        "install_git_config",
        lambda vm_id, user, text, **kwargs: captured.update({"git": (vm_id, user, text)}),
    )
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: 0)

    capture_preset(monkeypatch, captured)

    assert cli.main(["run"]) == 0
    assert captured["git"] == ("vm1", None, "[user]\n\tname = Test\n")


def test_config_disables_git_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    (tmp_path / ".sbx.toml").write_text("[sbx]\ngit_config = false\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        guest_setup, "host_git_config", lambda project_root=None: "[user]\n\tname = Test\n"
    )
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(
        guest_setup,
        "install_git_config",
        lambda vm_id, user, text, **kwargs: captured.update({"git": (vm_id, user, text)}),
    )
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: 0)

    capture_preset(monkeypatch, captured)

    assert cli.main(["run"]) == 0
    assert captured["git"] == ("vm1", None, None)


def test_shell_uses_configured_git_forwarding_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[sbx]\nname = "vm1"\nrun_user = "agent"\ngit_config = false\n', encoding="utf-8"
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        guest_setup, "host_git_config", lambda project_root=None: "must not be read"
    )
    monkeypatch.setattr(
        guest_setup,
        "install_git_config",
        lambda vm_id, user, text, **kwargs: captured.update({"git": text}),
    )
    monkeypatch.setattr(guest_setup, "prepare_run_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    assert cli.main(["--config", str(config), "shell", "--keep-running"]) == 0
    assert captured["git"] is None


def test_credential_free_env_preserves_real_smolvm_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SMOLVM_DATA_DIR", raising=False)

    env = guest_setup.credential_free_env(tmp_path / "temp-home", forward_env=[])

    assert env["HOME"] == str(tmp_path / "temp-home")
    assert env["SMOLVM_DATA_DIR"] == str(home / ".local" / "state" / "smolvm")
    assert (tmp_path / "temp-home" / ".smolvm").is_symlink()


def test_run_does_not_copy_host_credentials_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
        captured["temp_home"] = temp_home
        return {"HOME": str(temp_home), "SBX_TEST": "credential-free"}

    monkeypatch.setattr(guest_setup, "credential_free_env", fake_credential_free_env)
    capture_preset(monkeypatch, captured, vm_id="pi-sbx")

    rc = cli.main(["run", "--no-attach"])

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["host_env"] == {
        "HOME": str(captured["temp_home"]),
        "SBX_TEST": "credential-free",
    }
    assert not Path(captured["temp_home"]).exists()


def test_env_vars_are_not_forwarded_by_default_with_host_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    (tmp_path / ".sbx.toml").write_text("[sbx]\ncopy_host_credentials = true\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured: dict[str, object] = {}

    capture_preset(monkeypatch, captured, vm_id="pi-sbx")

    rc = cli.main(["run", "--no-attach"])

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert "OPENAI_API_KEY" not in preset_kwargs["host_env"]


def test_env_flag_explicitly_forwards_selected_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    (tmp_path / ".sbx.toml").write_text("[sbx]\ncopy_host_credentials = true\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured: dict[str, object] = {}

    capture_preset(monkeypatch, captured, vm_id="pi-sbx")

    rc = cli.main(["run", "--no-attach", "--env", "OPENAI_API_KEY"])

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["host_env"]["OPENAI_API_KEY"] == "secret"


def test_copy_host_credentials_config_uses_current_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    (tmp_path / ".sbx.toml").write_text("[sbx]\ncopy_host_credentials = true\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fail_credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
        raise AssertionError("credential-free env should not be created")

    monkeypatch.setattr(guest_setup, "credential_free_env", fail_credential_free_env)
    capture_preset(monkeypatch, captured, vm_id="pi-sbx")

    rc = cli.main(["run", "--no-attach"])

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["host_env"]["HOME"] == os.environ["HOME"]
    assert "copy_host_credentials=true" in capsys.readouterr().err


def test_existing_vm_does_not_warn_about_credential_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text(
        '[sbx]\nname = "vm1"\ncopy_host_credentials = true\n', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "running")
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    assert cli.main(["run", "--no-attach"]) == 0
    assert "copy_host_credentials=true" not in capsys.readouterr().err


def test_destroy_deletes_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["remove", "vm1", "--force"])

    assert rc == 0
    assert capfd.readouterr().out == "Destroyed VM 'vm1'.\n"


def test_create_auto_writes_project_config_for_new_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(
        [
            "create",
            "vm1",
            "--agent",
            "codex",
            "--memory",
            "8192",
            "--disk-size",
            "40960",
            "--project-path",
            ".",
            "--run-user",
            "agent",
            "--env",
            "OPENAI_API_KEY",
        ]
    )

    assert rc == 0
    text = (tmp_path / ".sbx.toml").read_text(encoding="utf-8")
    assert 'name = "vm1"' in text
    assert 'agent = "codex"' in text
    assert "memory = 8192" in text
    assert "disk_size = 40960" in text
    assert 'project_path = "."' in text
    assert 'run_user = "agent"' in text
    assert 'env = ["OPENAI_API_KEY"]' in text
    assert "copy_host_credentials = false" in text
    assert "git_config = true" in text
    output = capfd.readouterr()
    assert "Created sandbox 'vm1'." in output.out
    assert "Wrote .sbx.toml." in output.out
    assert "Run agent:  sbx run" in output.out


def test_project_config_values_preserve_curated_image_workflow() -> None:
    values = cli._project_config_values(
        SimpleNamespace(
            image="~/.smolvm/images/sbx",
            memory=None,
            cpus=None,
            disk_size=None,
            project_path=".",
            run_user="agent",
            writable_mounts=True,
            env=None,
        ),
        {},
        vm_name="the-quest",
        agent="pi",
    )

    assert values == {
        "name": "the-quest",
        "agent": "pi",
        "image": "~/.smolvm/images/sbx",
        "project_path": ".",
        "run_user": "agent",
        "writable_mounts": True,
    }


def test_write_config_updates_only_missing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text('[sbx]\nname = "vm1"\nmemory = 4096\n', encoding="utf-8")
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["create", "vm2", "--memory", "8192", "--disk-size", "40960", "--write-config"])

    assert rc == 0
    text = (tmp_path / ".sbx.toml").read_text(encoding="utf-8")
    assert 'name = "vm1"' in text
    assert "memory = 4096" in text
    assert "memory = 8192" not in text
    assert 'agent = "pi"' in text
    assert "disk_size = 40960" in text
    assert (
        "updated .sbx.toml with missing project defaults: agent, disk_size"
        in capfd.readouterr().err
    )


def test_reusing_existing_vm_writes_config_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = sandbox ] && [ "$2" = info ]; then\n'
        '  printf \'%s\\n\' \'{"data":{"vm":{"status":"stopped"}}}\'\n'
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli.runtime, "smolvm_argv", lambda args: ["smolvm", *args])
    monkeypatch.setattr(guest_setup, "sync_guest_clock", lambda vm_id, **kwargs: None)

    assert cli.main(["run", "vm1", "--no-attach"]) == 0
    assert not (tmp_path / ".sbx.toml").exists()

    assert cli.main(["run", "vm1", "--no-attach", "--write-config"]) == 0
    assert 'name = "vm1"' in (tmp_path / ".sbx.toml").read_text(encoding="utf-8")
    assert "wrote .sbx.toml" in capfd.readouterr().err


def test_lifecycle_commands_default_to_configured_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    install_fake_smolvm(monkeypatch, tmp_path)

    assert cli.main(["stop"]) == 0
    assert cli.main(["rm", "--force"]) == 0

    out = capfd.readouterr().out
    assert "sandbox stop vm1" in out
    assert "Destroyed VM 'vm1'." in out


def test_run_with_project_path_attaches_from_mounted_project_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured)

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(guest_setup, "attach", fake_attach)
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda *args: 0)

    rc = cli.main(["run", "--project-path", str(project)])

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["mounts"] == [f"{project}:{project}"]
    assert preset_kwargs["writable_mounts"] is True
    assert captured["attach"] == ("vm1", "pi", str(project))


def test_run_uses_configured_auth_ports_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    (tmp_path / ".sbx.toml").write_text(
        "[sbx]\nauth_port = true\nauth_host_port = 1555\nauth_guest_port = 1666\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured)

    def fake_expose(vm_id: str, host_port: int, guest_port: int) -> int:
        captured["expose"] = (vm_id, host_port, guest_port)
        return 0

    def fake_attach(vm_id: str, launch_command: str, **kwargs: object) -> int:
        captured["attach"] = (vm_id, launch_command, kwargs.get("cwd"))
        return 0

    monkeypatch.setattr(cli.network, "expose_auth_port", fake_expose)
    monkeypatch.setattr(guest_setup, "attach", fake_attach)

    assert cli.main(["run"]) == 0
    assert captured["preset_closed"] is True
    assert captured["expose"] == ("vm1", 1555, 1666)
    assert captured["attach"] == ("vm1", "pi", None)


def test_run_existing_vm_starts_without_creating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1", "status": "stopped"}}, '
                '"command": "info", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli.runtime, "run_capture", fake_run_capture)
    monkeypatch.setattr(cli.runtime, "run", fake_run)
    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("existing VMs must not install presets")
        ),
    )
    monkeypatch.setattr(guest_setup, "sync_guest_clock", lambda vm_id, **kwargs: None)
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: 0)

    rc = cli.main(["run", "vm1"])

    assert rc == 0
    assert calls == [
        ["smolvm", "sandbox", "info", "vm1", "--json"],
        ["smolvm", "sandbox", "start", "vm1", "--boot-timeout", "60"],
        ["smolvm", "sandbox", "stop", "vm1"],
    ]


def test_run_existing_error_vm_suggests_recreate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1", "status": "error"}}, '
                '"command": "info", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.runtime, "run_capture", fake_run_capture)

    rc = cli.main(["run", "vm1"])

    assert rc == 1
    assert "sbx recreate vm1 --force" in capsys.readouterr().err


def test_failed_managed_run_hides_json_and_prints_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("QEMU exited early while booting VM 'vm1'")
        ),
    )

    rc = cli.main(["run", "vm1"])

    output = capsys.readouterr()
    assert rc == 1
    assert output.out == ""
    assert "failed to create preset 'pi'" in output.err
    assert "QEMU exited early" in output.err


@pytest.mark.parametrize(
    ("error", "expected_rc"),
    [(RuntimeError("failed"), 1), (KeyboardInterrupt(), 130)],
)
def test_preset_failure_cleans_temporary_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
    expected_rc: int,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, Path] = {}

    def fake_credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
        captured["temp_home"] = temp_home
        return {"HOME": str(temp_home)}

    monkeypatch.setattr(guest_setup, "credential_free_env", fake_credential_free_env)
    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    assert cli.main(["create", "vm1"]) == expected_rc
    assert not captured["temp_home"].exists()


def test_run_positional_name_creates_missing_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured, vm_id="pi-sbx")
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda *args: 0)

    assert cli.main(["run", "pi-sbx"]) == 0
    preset_name, preset_kwargs = captured["preset"]
    assert preset_name == "pi"
    assert preset_kwargs["vm_name"] == "pi-sbx"
    assert captured["preset_closed"] is True


def test_run_missing_vm_creates_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured)
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda *args: 0)

    assert cli.main(["run", "vm1", "--no-attach"]) == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["vm_name"] == "vm1"


def test_create_is_run_no_attach_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured)
    monkeypatch.setattr(
        cli,
        "_post_start_actions",
        lambda **kwargs: captured.setdefault("post", kwargs) and 0,
    )

    assert cli.main(["create", "vm1"]) == 0
    assert captured["post"]["attach"] is False


def test_run_json_requires_no_attach(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "vm1", "--json"]) == 2
    assert "requires --no-attach" in capsys.readouterr().err


def test_existing_vm_json_suppresses_start_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "stopped")
    monkeypatch.setattr(cli, "_sync_existing_vm_start_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm_capture",
        lambda argv: subprocess.CompletedProcess(
            argv, 0, stdout="Started upstream VM\n", stderr=""
        ),
    )
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    assert cli.main(["create", "vm1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vm": {"name": "vm1", "status": "running"}}


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "vm1", "--json"],
        ["run", "vm1", "--no-attach", "--json"],
    ],
)
def test_preset_lifecycle_json(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"vm": {"name": "vm1", "status": "running"}}


def test_recreate_json_suppresses_delete_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_delete_vm", lambda name, *, quiet=False: 0)

    assert cli.main(["recreate", "vm1", "--force", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vm": {"name": "vm1", "status": "running"}}


def test_expose_auth_port_warns_when_port_already_listening(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_ssh_command(vm_id: str) -> list[str]:
        raise AssertionError("should not create a second tunnel")

    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: True)
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel_for_host_port", lambda port: None)
    monkeypatch.setattr(cli.network, "ssh_command", fail_ssh_command)

    assert cli.network.expose_auth_port("vm1", 1455, 1455) == 0
    assert "warning" in capsys.readouterr().err


def test_expose_auth_port_uses_direct_ssh_local_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_ssh_command(vm_id: str) -> list[str]:
        captured["vm_id"] = vm_id
        return ["ssh", "-p", "2200", "-i", "/key", "root@127.0.0.1"]

    class FakePopen:
        pid = 123
        stderr = None

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            captured["argv"] = argv
            captured["popen_kwargs"] = kwargs

        def poll(self) -> None:
            return None

    calls = {"listening": 0}

    def fake_listening(port: int) -> bool:
        calls["listening"] += 1
        return calls["listening"] > 1

    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", fake_listening)
    monkeypatch.setattr(cli.network, "ssh_command", fake_ssh_command)
    monkeypatch.setattr(cli.network.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli.network, "_record_auth_tunnel", lambda *args, **kwargs: None)

    rc = cli.network.expose_auth_port("vm1", 1455, 1455)

    assert rc == 0
    assert captured["vm_id"] == "vm1"
    assert captured["argv"] == [
        "ssh",
        "-p",
        "2200",
        "-i",
        "/key",
        "-N",
        "-L",
        "127.0.0.1:1455:127.0.0.1:1455",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
        "root@127.0.0.1",
    ]


def test_auth_port_exposes_pi_callback_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_expose(vm_id: str, host_port: int, guest_port: int, *, replace: bool = False) -> int:
        captured["expose"] = (vm_id, host_port, guest_port, replace)
        return 0

    monkeypatch.setattr(cli.network, "expose_auth_port", fake_expose)

    rc = cli.main(["network", "auth-port", "vm1"])

    assert rc == 0
    assert captured["expose"] == ("vm1", 1455, 1455, False)


def test_network_forward_defaults_to_configured_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_forward(vm_id: str, forwards: list[tuple[str, int, int]]) -> int:
        captured["vm_id"] = vm_id
        captured["forwards"] = forwards
        return 0

    monkeypatch.setattr(cli.network, "_foreground_port_forward", fake_forward)

    assert cli.main(["--config", str(config), "network", "forward", "8080:3000"]) == 0
    assert captured == {
        "vm_id": "vm1",
        "forwards": [("127.0.0.1", 8080, 3000)],
    }


def test_network_forward_requires_name_or_project_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["network", "forward", "3000"]) == 2
    assert "requires a VM name" in capsys.readouterr().err


def test_network_forward_accepts_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli.network,
        "_foreground_port_forward",
        lambda vm_id, forwards: captured.update({"vm_id": vm_id, "forwards": forwards}) or 0,
    )

    assert cli.main(["network", "forward", "--name", "3000", "0.0.0.0:3000:3000"]) == 0
    assert captured == {
        "vm_id": "3000",
        "forwards": [("0.0.0.0", 3000, 3000)],
    }


def test_network_forward_accepts_multiple_specs_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli.network,
        "_foreground_port_forward",
        lambda vm_id, forwards: captured.update({"vm_id": vm_id, "forwards": forwards}) or 0,
    )

    assert cli.main(["--config", str(config), "network", "forward", "3000", "8080:80"]) == 0
    assert captured == {
        "vm_id": "vm1",
        "forwards": [("127.0.0.1", 3000, 3000), ("127.0.0.1", 8080, 80)],
    }


def test_network_forward_accepts_explicit_name_with_multiple_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli.network,
        "_foreground_port_forward",
        lambda vm_id, forwards: captured.update({"vm_id": vm_id, "forwards": forwards}) or 0,
    )

    assert cli.main(["network", "forward", "--name", "vm2", "3000", "8080:80"]) == 0
    assert captured == {
        "vm_id": "vm2",
        "forwards": [("127.0.0.1", 3000, 3000), ("127.0.0.1", 8080, 80)],
    }


def test_network_forward_ctrl_c_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(vm_id: str, forwards: list[tuple[str, int, int]]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.network, "_foreground_port_forward", interrupted)

    assert cli.main(["network", "forward", "--name", "vm2", "3005"]) == 130


def test_network_commands_default_to_configured_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli.network,
        "expose_auth_port",
        lambda vm_id, host_port, guest_port, *, replace=False: (
            captured.setdefault("expose", (vm_id, host_port, guest_port, replace)) and 0
        ),
    )

    def fake_close_auth_tunnel(vm_id: str) -> bool:
        captured["close"] = vm_id
        return False

    monkeypatch.setattr(cli.network, "_close_tracked_auth_tunnel", fake_close_auth_tunnel)
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: False)

    def fake_run_capture(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["status"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"data":{"vm":{"name":"vm1","status":"stopped","backend":"qemu",'
                '"ip_address":"10.0.2.15","ssh_port":2201}}}'
            ),
        )

    monkeypatch.setattr(cli.network, "run_smolvm_capture", fake_run_capture)

    assert cli.main(["network", "auth-port"]) == 0
    assert cli.main(["network", "close-auth-port"]) == 0
    assert cli.main(["network", "status"]) == 0

    assert captured["expose"] == ("vm1", 1455, 1455, False)
    assert captured["close"] == "vm1"
    assert captured["status"] == ["sandbox", "info", "vm1", "--json"]
    assert "No tracked auth port tunnel for 'vm1'." in capfd.readouterr().out


def test_network_status_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = (
        '{"data":{"vm":{"name":"vm1","status":"running","backend":"qemu",'
        '"ip_address":"10.0.2.15","ssh_port":2201}}}'
    )
    monkeypatch.setattr(
        cli.network,
        "run_smolvm_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=payload),
    )
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: False)

    assert cli.main(["network", "status", "vm1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name": "vm1",
        "status": "running",
        "backend": "qemu",
        "guest_ip": "10.0.2.15",
        "ssh_port": 2201,
        "port_forwards": [],
        "auth_callback": {"status": "inactive", "detail": None},
    }


def test_network_close_auth_port_without_tracked_tunnel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.network, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli.network, "TUNNELS_FILE", tmp_path / "state" / "tunnels.json")

    rc = cli.main(["network", "close-auth-port", "vm1"])

    assert rc == 0
    assert capfd.readouterr().out == "No tracked auth port tunnel for 'vm1'.\n"


def test_recreate_positional_name_without_force_reaches_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["recreate", "vm1"])

    assert rc == 2
    assert "Destroy and recreate VM 'vm1'" in capsys.readouterr().err


def test_recreate_deletes_then_starts_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    calls: list[tuple[str, str]] = []
    captured: dict[str, object] = {}

    def fake_delete(vm_id: str, extra_args: list[str] | None = None) -> int:
        calls.append(("delete", vm_id))
        return 0

    monkeypatch.setattr(cli, "_delete_vm", fake_delete)
    capture_preset(monkeypatch, captured)

    rc = cli.main(["recreate", "vm1", "--force", "--no-attach"])

    assert rc == 0
    assert calls == [("delete", "vm1")]
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["vm_name"] == "vm1"


def test_cli_project_path_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    config_project = tmp_path / "config-project"
    cli_project = tmp_path / "cli-project"
    config_project.mkdir()
    cli_project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[sbx]
project_path = "{config_project}"
""".strip(),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured, vm_id="pi-sbx")

    rc = cli.main(
        [
            "--config",
            str(config),
            "run",
            "--project-path",
            str(cli_project),
            "--no-attach",
        ]
    )

    assert rc == 0
    _, preset_kwargs = captured["preset"]
    assert preset_kwargs["mounts"] == [f"{cli_project}:{cli_project}"]
    assert preset_kwargs["writable_mounts"] is True
    assert "Created sandbox 'pi-sbx'." in capfd.readouterr().out


def test_cli_flags_override_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        """
[sbx]
agent = "pi"
name = "from-config"
""".strip(),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    capture_preset(monkeypatch, captured, vm_id="from-cli")
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    rc = cli.main(["--config", str(config), "run", "from-cli", "--agent", "claude"])

    assert rc == 0
    preset_name, preset_kwargs = captured["preset"]
    assert preset_name == "claude"
    assert preset_kwargs["vm_name"] == "from-cli"
    assert "Created sandbox 'from-cli'." in capfd.readouterr().out


def test_invalid_agent_in_config_returns_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[sbx]\nagent = "bad"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "run"])

    assert rc == 2
    assert "[sbx].agent must be one of" in capsys.readouterr().err


def test_image_build_subcommand_and_default_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sbx.image import build_debian

    monkeypatch.chdir(tmp_path)
    names = []

    def fake_main_from_args(args: object) -> int:
        names.append(args.name)
        return 0

    monkeypatch.setattr(build_debian, "main_from_args", fake_main_from_args)

    assert cli.main(["image", "build"]) == 0
    assert cli.main(["image", "build", "--name", "custom-image"]) == 0
    assert names == ["sbx", "custom-image"]
    assert not (tmp_path / ".sbx.toml").exists()


def test_image_build_debian_subcommand_is_removed() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["image", "build-debian"])

    assert error.value.code == 2


def test_image_list_lists_local_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    images = tmp_path / ".smolvm" / "images"
    docker_image = images / "legacy-docker"
    plain_image = images / "legacy-plain"
    invalid_image = images / "invalid"
    docker_image.mkdir(parents=True)
    plain_image.mkdir()
    invalid_image.mkdir()
    (docker_image / "smolvm-image.json").write_text(
        '{"name":"legacy-docker","kernel":"vmlinux-docker.bin","rootfs":"rootfs.ext4","sbx":{"agent":"pi","features":["docker"]}}',
        encoding="utf-8",
    )
    (plain_image / "smolvm-image.json").write_text(
        '{"name":"legacy-plain","kernel":"vmlinux.bin","rootfs":"rootfs.ext4","sbx":{"agent":"pi","features":[]}}',
        encoding="utf-8",
    )
    (invalid_image / "smolvm-image.json").write_text("not json", encoding="utf-8")

    assert cli.main(["image", "list"]) == 0

    out = capsys.readouterr().out
    assert "NAME" in out
    assert "FEATURES" in out
    assert "legacy-docker" in out
    assert "docker" in out
    assert "legacy-plain" in out
    assert "invalid" not in out


def test_image_ls_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    image = tmp_path / ".smolvm" / "images" / "legacy-docker"
    image.mkdir(parents=True)
    (image / "smolvm-image.json").write_text(
        '{"kernel":"vmlinux-docker.bin","rootfs":"rootfs.ext4","sbx":{"agent":"pi","features":["docker"]}}',
        encoding="utf-8",
    )

    assert cli.main(["image", "ls", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "agent": "pi",
            "features": ["docker"],
            "kernel": "vmlinux-docker.bin",
            "name": "legacy-docker",
            "path": str(image),
            "rootfs": "rootfs.ext4",
        }
    ]


def test_invalid_configured_vm_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "Bad_Name"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "create"])

    assert rc == 2
    assert "must be a valid hostname" in capsys.readouterr().err


def test_create_sets_hostname_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    hostnames: list[str] = []

    monkeypatch.setattr(
        guest_setup, "set_hostname", lambda vm_id, **kwargs: hostnames.append(vm_id)
    )
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"ok": true, "data": {"vm": {"name": "vm1"}}}\n',
            stderr="",
        ),
    )

    assert cli.main(["create", "vm1"]) == 0
    assert hostnames == ["vm1"]


def test_existing_vm_start_does_not_reset_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    hostnames: list[str] = []
    monkeypatch.setattr(
        guest_setup, "set_hostname", lambda vm_id, **kwargs: hostnames.append(vm_id)
    )
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "stopped")
    monkeypatch.setattr(cli, "_start_existing_vm_if_needed", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    assert cli.main(["run", "vm1", "--no-attach"]) == 0
    assert hostnames == []
