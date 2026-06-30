from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sbx import cli

_ORIGINAL_HOST_GIT_CONFIG = cli._host_git_config


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (tmp_path / "home-config.toml",))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (tmp_path / ".sbx.toml",))
    monkeypatch.setattr(cli, "_host_git_config", lambda: None)


def install_fake_smolvm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli, "_smolvm_argv", lambda args: ["smolvm", *args])
    return smolvm


def print_smolvm_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_run_smolvm", lambda args, **kwargs: print(" ".join(args)) or 0)


def test_smolvm_runner_does_not_need_console_script_on_path() -> None:
    assert cli._smolvm_argv(["doctor"]) == [
        sys.executable,
        "-c",
        "from smolvm.cli.main import main; raise SystemExit(main())",
        "doctor",
    ]


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
        '[sbx]\nname = "reviewhero"\ndisk_size = 10240\nmemory = 8192\ncpus = 4\n',
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = doctor ]; then printf '%s\\n' \"$*\"; exit 0; fi\n"
        "if [ \"$1\" = sandbox ] && [ \"$2\" = info ]; then\n"
        "  printf '%s\\n' "
        "'{\"data\":{\"vm\":{\"status\":\"stopped\",\"disk_size\":81920,"
        "\"memory\":8192,\"vcpus\":4}}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli, "_smolvm_argv", lambda args: ["smolvm", *args])

    rc = cli.main(["doctor"])

    assert rc == 0
    out = capfd.readouterr().out
    assert "doctor --backend qemu" in out
    assert "VM 'reviewhero' already exists and differs from .sbx.toml" in out
    assert "disk_size: config requests 10240 MiB, existing VM has 81920 MiB" in out
    assert "sbx recreate reviewhero --force" in out


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
        f'[sbx]\nname = "reviewhero"\nimage = "{image}"\ndisk_size = 10\n',
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smolvm = bin_dir / "smolvm"
    smolvm.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = doctor ]; then printf '%s\\n' \"$*\"; exit 0; fi\n"
        "if [ \"$1\" = sandbox ] && [ \"$2\" = info ]; then\n"
        "  printf '%s\\n' "
        "'{\"data\":{\"vm\":{\"status\":\"stopped\",\"disk_size\":10,"
        "\"memory\":512,\"vcpus\":2}}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli, "_smolvm_argv", lambda args: ["smolvm", *args])

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
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["--debug", "ls"])

    captured = capfd.readouterr()
    assert rc == 0
    assert "sbx debug: argv: ['--debug', 'ls']" in captured.err
    assert "sbx debug: run: smolvm sandbox list" in captured.err


def test_list_passthrough_does_not_require_name(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    print_smolvm_args(monkeypatch)

    rc = cli.main(["ls"])

    assert rc == 0
    assert capfd.readouterr().out == "sandbox list\n"


def test_list_all_includes_stopped_vms(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    print_smolvm_args(monkeypatch)

    rc = cli.main(["ls", "--all"])

    assert rc == 0
    assert capfd.readouterr().out == "sandbox list --all\n"

    rc = cli.main(["ls", "-a"])

    assert rc == 0
    assert capfd.readouterr().out == "sandbox list --all\n"


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

    def fake_prepare(vm_id: str, user: str) -> None:
        captured["prepare"] = (vm_id, user)

    def fake_attach(vm_id: str, user: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, user, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_prepare_run_user", fake_prepare)
    monkeypatch.setattr(cli, "_attach_as_user", fake_attach)
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

    monkeypatch.setattr(cli, "_prepare_run_user", lambda vm_id, user: None)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    def fake_attach(vm_id: str, user: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, user, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_attach_as_user", fake_attach)

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

    def fake_attach(vm_id: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_attach_as_root", fake_attach)
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

    rc = cli.main(["run", "--no-auth-port"])

    assert rc == 0
    assert capfd.readouterr().out == (
        "codex start --name demo --backend qemu --boot-timeout 30 --mount .:/workspace --attach\n"
    )


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
backend = "qemu"
os = "ubuntu"
mount = ["{extra_mount}", ".:/workspace"]
project_path = "{project}"
writable_mounts = false
install_timeout = 900
boot_timeout = 75
""".strip(),
        encoding="utf-8",
    )

    rc = cli.main(["--config", str(config), "run", "--no-auth-port", "--no-attach"])

    assert rc == 0
    assert capfd.readouterr().out == (
        "codex start --name configured --memory 8192 --disk-size 32768 "
        "--os ubuntu --install-timeout 900 --backend qemu --boot-timeout 75 "
        f"--mount {project}:{project} "
        f"--mount {extra_mount}:{extra_mount} "
        "--mount .:/workspace --writable-mounts --no-attach\n"
    )


def test_run_with_cpus_uses_sdk_start_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        '[sbx]\nagent = "pi"\ncpus = 4\nauth_port = false\ngit_config = false\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_start_preset_with_sdk(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_start_preset_with_sdk", fake_start_preset_with_sdk)

    rc = cli.main(["--config", str(config), "run", "--no-attach"])

    assert rc == 0
    assert captured["agent"] == "pi"
    assert captured["cpus"] == 4


def test_run_uses_local_image_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
mount = [".:/workspace"]
'''.strip(),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_start_local_image(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_start_local_image", fake_start_local_image)

    rc = cli.main(["--config", str(config), "run", "--no-auth-port"])

    assert rc == 0
    assert captured["image_dir"] == image_dir
    assert captured["agent"] == "pi"
    assert captured["mounts"] == [".:/workspace"]
    assert captured["attach"] is True


def test_run_user_from_config_starts_without_smolvm_attach_then_attaches_as_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    def fake_prepare(vm_id: str, user: str) -> None:
        captured["prepare"] = (vm_id, user)

    def fake_attach(vm_id: str, user: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, user, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(cli, "_prepare_run_user", fake_prepare)
    monkeypatch.setattr(cli, "_attach_as_user", fake_attach)
    config = tmp_path / "config.toml"
    config.write_text(
        """
[sbx]
run_user = "agent"
""".strip(),
        encoding="utf-8",
    )

    rc = cli.main(["--config", str(config), "run"])

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
        "--json",
    ]
    assert captured["prepare"] == ("vm1", "agent")
    assert captured["attach"] == ("vm1", "agent", "pi", None)
    assert capfd.readouterr().out == (
        "Started 'vm1'. Launching pi as user agent...\nsandbox stop vm1\n"
    )


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

    monkeypatch.setattr(cli, "_host_git_config", _ORIGINAL_HOST_GIT_CONFIG)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._host_git_config() == (
        '[user]\n\tname = "Ada Lovelace"\n\temail = "ada@example.test"\n\n'
        '[init]\n\tdefaultBranch = "main"\n'
    )


def test_git_config_defaults_on_for_managed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_host_git_config", lambda: "[user]\n\tname = Test\n")
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(
        cli,
        "_install_git_config",
        lambda vm_id, user, text: captured.update({"git": (vm_id, user, text)}),
    )
    monkeypatch.setattr(cli, "_attach_as_root", lambda vm_id, launch_command, cwd=None: 0)

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{ "ok": true, "data": {"vm": {"name": "vm1"}} }\n',
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)

    assert cli.main(["run"]) == 0
    assert captured["git"] == ("vm1", None, "[user]\n\tname = Test\n")


def test_no_git_config_disables_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_host_git_config", lambda: "[user]\n\tname = Test\n")
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(
        cli,
        "_install_git_config",
        lambda vm_id, user, text: captured.update({"git": (vm_id, user, text)}),
    )
    monkeypatch.setattr(cli, "_attach_as_root", lambda vm_id, launch_command, cwd=None: 0)

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{ "ok": true, "data": {"vm": {"name": "vm1"}} }\n',
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)

    assert cli.main(["run", "--no-git-config"]) == 0
    assert captured["git"] == ("vm1", None, None)


def test_credential_free_env_preserves_real_smolvm_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SMOLVM_DATA_DIR", raising=False)

    env = cli._credential_free_env(tmp_path / "temp-home", forward_env=[])

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

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["argv"] = argv
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli, "_credential_free_env", fake_credential_free_env)
    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(["run", "--no-attach", "--no-auth-port"])

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
    ]
    assert captured["env"] == {"HOME": str(captured["temp_home"]), "SBX_TEST": "credential-free"}


def test_env_vars_are_not_forwarded_by_default_with_host_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(["run", "--no-attach", "--copy-host-credentials", "--no-auth-port"])

    assert rc == 0
    assert captured["env"] is not None
    assert "OPENAI_API_KEY" not in captured["env"]


def test_env_flag_explicitly_forwards_selected_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(
        [
            "run",
            "--no-attach",
            "--copy-host-credentials",
            "--env",
            "OPENAI_API_KEY",
            "--no-auth-port",
        ]
    )

    assert rc == 0
    assert captured["env"]["OPENAI_API_KEY"] == "secret"


def test_copy_host_credentials_flag_uses_current_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fail_credential_free_env(temp_home: Path, *, forward_env: list[str]) -> dict[str, str]:
        raise AssertionError("credential-free env should not be created")

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["argv"] = argv
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli, "_credential_free_env", fail_credential_free_env)
    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(["run", "--no-attach", "--copy-host-credentials", "--no-auth-port"])

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
    ]
    assert captured["env"] is not None


def test_destroy_deletes_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["rm", "vm1", "--force"])

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
            "--name",
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
    assert "wrote .sbx.toml" in capfd.readouterr().err


def test_write_config_updates_only_missing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text(
        '[sbx]\nname = "vm1"\nmemory = 4096\n', encoding="utf-8"
    )
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(
        ["create", "--name", "vm2", "--memory", "8192", "--disk-size", "40960", "--write-config"]
    )

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
        "if [ \"$1\" = sandbox ] && [ \"$2\" = info ]; then\n"
        "  printf '%s\\n' '{\"data\":{\"vm\":{\"status\":\"stopped\"}}}'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    smolvm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cli, "_smolvm_argv", lambda args: ["smolvm", *args])

    assert cli.main(["run", "vm1", "--no-attach", "--no-auth-port"]) == 0
    assert not (tmp_path / ".sbx.toml").exists()

    assert cli.main(["run", "vm1", "--no-attach", "--no-auth-port", "--write-config"]) == 0
    assert 'name = "vm1"' in (tmp_path / ".sbx.toml").read_text(encoding="utf-8")
    assert "wrote .sbx.toml" in capfd.readouterr().err


def test_no_write_config_disables_auto_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    rc = cli.main(["create", "--name", "vm1", "--no-write-config"])

    assert rc == 0
    assert not (tmp_path / ".sbx.toml").exists()


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

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    def fake_attach(vm_id: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_attach_as_root", fake_attach)

    rc = cli.main(
        [
            "run",
            "--copy-host-credentials",
            "--no-auth-port",
            "--project-path",
            str(project),
        ]
    )

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--mount",
        f"{project}:{project}",
        "--writable-mounts",
        "--no-attach",
        "--json",
    ]
    assert captured["attach"] == ("vm1", "pi", str(project))


def test_run_exposes_auth_port_by_default_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    def fake_expose(vm_id: str, host_port: int, guest_port: int) -> int:
        captured["expose"] = (vm_id, host_port, guest_port)
        return 0

    def fake_attach(vm_id: str, launch_command: str, cwd: str | None = None) -> int:
        captured["attach"] = (vm_id, launch_command, cwd)
        return 0

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_expose_auth_port", fake_expose)
    monkeypatch.setattr(cli, "_attach_as_root", fake_attach)

    rc = cli.main(["run", "--copy-host-credentials"])

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
        "--json",
    ]
    assert captured["expose"] == ("vm1", 1455, 1455)
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

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(cli, "_attach_as_root", lambda vm_id, launch_command, cwd=None: 0)

    rc = cli.main(["run", "--name", "vm1"])

    assert rc == 0
    assert calls == [
        ["smolvm", "sandbox", "info", "vm1", "--json"],
        ["smolvm", "sandbox", "start", "vm1", "--boot-timeout", "30"],
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

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)

    rc = cli.main(["run", "--name", "vm1"])

    assert rc == 1
    assert "sbx recreate vm1 --force" in capsys.readouterr().err


def test_failed_managed_run_hides_json_and_prints_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["smolvm", "sandbox", "info"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=(
                '{"ok": false, "error": {"message": '
                "\"QEMU exited early while booting VM '999714'\"}}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)

    rc = cli.main(["run", "--name", "vm1"])

    output = capsys.readouterr()
    assert rc == 1
    assert output.out == ""
    assert "QEMU exited early" in output.err
    assert "sbx recreate <name> --force" in output.err


def test_run_positional_name_before_options_does_not_pass_sbx_flags_to_smolvm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["smolvm", "sandbox", "info"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        captured["create"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "pi-sbx"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["create"] = argv
        return 0

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(["run", "pi-sbx", "--no-auth-port", "--no-attach"])

    assert rc == 0
    assert captured["create"] == [
        "smolvm",
        "pi",
        "start",
        "--name",
        "pi-sbx",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
    ]


def test_run_positional_name_creates_missing_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["smolvm", "sandbox", "info"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        captured["create"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "pi-sbx"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(cli, "_attach_as_root", lambda vm_id, launch_command, cwd=None: 0)

    rc = cli.main(["run", "pi-sbx"])

    assert rc == 0
    assert captured["create"] == [
        "smolvm",
        "pi",
        "start",
        "--name",
        "pi-sbx",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
        "--json",
    ]


def test_run_missing_vm_creates_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run_capture(
        argv: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["smolvm", "sandbox", "info"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        captured["create"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"ok": true, "data": {"vm": {"name": "vm1"}}, '
                '"command": "start", "exit_code": 0, "error": null}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "_expose_auth_port", lambda vm_id, host_port, guest_port: 0)
    monkeypatch.setattr(cli, "_attach_as_root", lambda vm_id, launch_command, cwd=None: 0)

    rc = cli.main(["run", "--name", "vm1", "--copy-host-credentials"])

    assert rc == 0
    assert captured["create"] == [
        "smolvm",
        "pi",
        "start",
        "--name",
        "vm1",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
        "--json",
    ]


def test_create_is_run_no_attach_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_fake_smolvm(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(["create", "--name", "vm1", "--copy-host-credentials", "--no-auth-port"])

    assert rc == 0
    assert captured["argv"] == [
        "smolvm",
        "pi",
        "start",
        "--name",
        "vm1",
        "--backend",
        "qemu",
        "--boot-timeout",
        "30",
        "--no-attach",
    ]


def test_expose_auth_port_warns_when_port_already_listening(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_ssh_command(vm_id: str) -> list[str]:
        raise AssertionError("should not create a second tunnel")

    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: True)
    monkeypatch.setattr(cli, "_tracked_auth_tunnel_for_host_port", lambda port: None)
    monkeypatch.setattr(cli, "_ssh_command", fail_ssh_command)

    assert cli._expose_auth_port("vm1", 1455, 1455) == 0
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

    monkeypatch.setattr(cli, "_localhost_port_is_listening", fake_listening)
    monkeypatch.setattr(cli, "_ssh_command", fake_ssh_command)
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli, "_record_auth_tunnel", lambda *args, **kwargs: None)

    rc = cli._expose_auth_port("vm1", 1455, 1455)

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

    monkeypatch.setattr(cli, "_expose_auth_port", fake_expose)

    rc = cli.main(["network", "auth-port", "vm1"])

    assert rc == 0
    assert captured["expose"] == ("vm1", 1455, 1455, False)


def test_network_commands_default_to_configured_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".sbx.toml").write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_expose_auth_port",
        lambda vm_id, host_port, guest_port, *, replace=False: captured.setdefault(
            "expose", (vm_id, host_port, guest_port, replace)
        )
        and 0,
    )
    def fake_close_auth_tunnel(vm_id: str) -> bool:
        captured["close"] = vm_id
        return False

    monkeypatch.setattr(cli, "_close_tracked_auth_tunnel", fake_close_auth_tunnel)
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: False)

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

    monkeypatch.setattr(cli, "_run_smolvm_capture", fake_run_capture)

    assert cli.main(["network", "auth-port"]) == 0
    assert cli.main(["network", "close-auth-port"]) == 0
    assert cli.main(["network", "status"]) == 0

    assert captured["expose"] == ("vm1", 1455, 1455, False)
    assert captured["close"] == "vm1"
    assert captured["status"] == ["sandbox", "info", "vm1", "--json"]
    assert "No tracked auth port tunnel for 'vm1'." in capfd.readouterr().out


def test_network_close_auth_port_without_tracked_tunnel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "TUNNELS_FILE", tmp_path / "state" / "tunnels.json")

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
    calls: list[list[str] | tuple[str, str]] = []

    def fake_delete(vm_id: str, extra_args: list[str] | None = None) -> int:
        calls.append(("delete", vm_id))
        return 0

    def fake_run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "_delete_vm", fake_delete)
    monkeypatch.setattr(cli, "_run", fake_run)

    rc = cli.main(
        [
            "recreate",
            "--force",
            "--name",
            "vm1",
            "--no-attach",
            "--copy-host-credentials",
            "--no-auth-port",
        ]
    )

    assert rc == 0
    assert calls == [
        ("delete", "vm1"),
        [
            "smolvm",
            "pi",
            "start",
            "--name",
            "vm1",
            "--backend",
            "qemu",
            "--boot-timeout",
            "30",
            "--no-attach",
        ],
    ]


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

    rc = cli.main(
        [
            "--config",
            str(config),
            "run",
            "--project-path",
            str(cli_project),
            "--no-auth-port",
            "--no-attach",
        ]
    )

    assert rc == 0
    assert capfd.readouterr().out == (
        "pi start --backend qemu --boot-timeout 30 "
        f"--mount {cli_project}:{cli_project} "
        "--writable-mounts --no-attach\n"
    )


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

    rc = cli.main(
        [
            "--config",
            str(config),
            "run",
            "--agent",
            "claude",
            "--name",
            "from-cli",
            "--no-auth-port",
        ]
    )

    assert rc == 0
    assert capfd.readouterr().out == (
        "claude start --name from-cli --backend qemu --boot-timeout 30 --attach\n"
    )


def test_invalid_agent_in_config_returns_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[sbx]\nagent = "bad"\n', encoding="utf-8")

    rc = cli.main(["--config", str(config), "run"])

    assert rc == 2
    assert "[sbx].agent must be one of" in capsys.readouterr().err


def test_image_build_debian_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    from sbx.image import build_debian

    captured = {}

    def fake_main_from_args(args: object) -> int:
        captured["with_docker"] = args.with_docker
        captured["name"] = args.name
        return 0

    monkeypatch.setattr(build_debian, "main_from_args", fake_main_from_args)

    assert cli.main(["image", "build-debian", "--with-docker", "--name", "docker-image"]) == 0
    assert captured == {"with_docker": True, "name": "docker-image"}


def test_image_ls_lists_local_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    images = tmp_path / ".smolvm" / "images"
    docker_image = images / "debian-sbx-docker"
    plain_image = images / "debian-sbx"
    invalid_image = images / "invalid"
    docker_image.mkdir(parents=True)
    plain_image.mkdir()
    invalid_image.mkdir()
    (docker_image / "smolvm-image.json").write_text(
        '{"name":"debian-sbx-docker","kernel":"vmlinux-docker.bin","rootfs":"rootfs.ext4","sbx":{"agent":"pi","features":["docker"]}}',
        encoding="utf-8",
    )
    (plain_image / "smolvm-image.json").write_text(
        '{"name":"debian-sbx","kernel":"vmlinux.bin","rootfs":"rootfs.ext4","sbx":{"agent":"pi","features":[]}}',
        encoding="utf-8",
    )
    (invalid_image / "smolvm-image.json").write_text("not json", encoding="utf-8")

    assert cli.main(["image", "ls"]) == 0

    out = capsys.readouterr().out
    assert "NAME" in out
    assert "FEATURES" in out
    assert "debian-sbx-docker" in out
    assert "docker" in out
    assert "debian-sbx" in out
    assert "invalid" not in out


def test_image_ls_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    image = tmp_path / ".smolvm" / "images" / "debian-sbx-docker"
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
            "name": "debian-sbx-docker",
            "path": str(image),
            "rootfs": "rootfs.ext4",
        }
    ]
