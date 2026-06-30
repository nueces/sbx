from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import smolvm
import smolvm.facade
import smolvm.utils

from sbx import cli


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (tmp_path / "home-config.toml",))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (tmp_path / ".sbx.toml",))
    monkeypatch.setattr(cli, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "TUNNELS_FILE", tmp_path / "state" / "tunnels.json")
    monkeypatch.setattr(cli, "SESSIONS_FILE", tmp_path / "state" / "sessions.json")
    monkeypatch.setattr(cli, "SMOLVM_DB_PATH", tmp_path / "smolvm.db")


@pytest.fixture
def local_image_dir(tmp_path: Path) -> Path:
    image = tmp_path / "image"
    image.mkdir()
    (image / "vmlinux.bin").write_text("kernel", encoding="utf-8")
    (image / "rootfs.ext4").write_text("rootfs", encoding="utf-8")
    (image / "initrd.img").write_text("initrd", encoding="utf-8")
    (image / "smolvm-image.json").write_text(
        json.dumps(
            {
                "name": "debian-pi",
                "kernel": "vmlinux.bin",
                "rootfs": "rootfs.ext4",
                "initrd": "initrd.img",
                "boot_args": "boot args",
                "sbx": {"agent": "pi", "launch_command": "custom-pi"},
            }
        ),
        encoding="utf-8",
    )
    return image


@pytest.fixture
def fake_smolvm_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    private_key.write_text("private", encoding="utf-8")
    public_key.write_text("ssh-ed25519 public", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeSmolVM:
        def __init__(self, config: object, **kwargs: object) -> None:
            del kwargs
            captured["config"] = config
            self.vm_id = "local-vm"

        def start(self, **kwargs: object) -> None:
            captured["started"] = True
            captured["start_kwargs"] = kwargs

        def wait_for_ssh(self, **kwargs: object) -> None:
            captured["waited"] = True
            captured["wait_kwargs"] = kwargs

        def close(self) -> None:
            captured["closed"] = True

    def fake_vm_config(**kwargs: object) -> dict[str, object]:
        captured["vm_config"] = kwargs
        return kwargs

    monkeypatch.setattr(smolvm, "SmolVM", FakeSmolVM)
    monkeypatch.setattr(smolvm, "VMConfig", fake_vm_config)
    monkeypatch.setattr(smolvm.utils, "ensure_ssh_key", lambda: (private_key, public_key))
    return captured


@pytest.fixture
def completed_ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["cmd"], 0, stdout="ok", stderr="")


def _write_vm_row(db_path: Path, vm_id: str, *, status: str, config: dict[str, object]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vms (id TEXT PRIMARY KEY, status TEXT, config TEXT)")
        conn.execute(
            "INSERT INTO vms (id, status, config) VALUES (?, ?, ?)",
            (vm_id, status, json.dumps(config)),
        )


def _read_vm_config(db_path: Path, vm_id: str) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT config FROM vms WHERE id = ?", (vm_id,)).fetchone()
    assert row is not None
    config = json.loads(row[0])
    assert isinstance(config, dict)
    return config


def test_sync_existing_vm_mounts_updates_stale_stopped_vm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = tmp_path / "project"
    host.mkdir()
    _write_vm_row(
        cli.SMOLVM_DB_PATH,
        "vm1",
        status="stopped",
        config={"workspace_mounts": [{"host_path": "/old", "guest_path": "/old"}]},
    )

    cli._sync_existing_vm_mounts_from_config(
        "vm1", [f"{host}:/workspace"], writable_mounts=True
    )

    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1")["workspace_mounts"] == [
        {"host_path": str(host), "guest_path": "/workspace", "mount_tag": None, "writable": True}
    ]
    assert capsys.readouterr().out == "sbx: updated mounts for existing VM 'vm1'\n"


def test_sync_existing_vm_mounts_skips_matching_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = tmp_path / "project"
    host.mkdir()
    config = {
        "workspace_mounts": [
            {
                "host_path": str(host),
                "guest_path": "/workspace",
                "mount_tag": None,
                "writable": False,
            }
        ],
        "other": "kept",
    }
    _write_vm_row(cli.SMOLVM_DB_PATH, "vm1", status="stopped", config=config)

    cli._sync_existing_vm_mounts_from_config(
        "vm1", [f"{host}:/workspace"], writable_mounts=False
    )

    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1") == config
    assert capsys.readouterr().out == ""


def test_cmd_start_does_not_sync_running_vm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(cli, "_host_git_config", lambda: None)
    monkeypatch.setattr(
        cli,
        "_sync_existing_vm_mounts_from_config",
        lambda *args, **kwargs: calls.append("sync"),
    )
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    assert cli.main(["--config", str(config), "run"]) == 0
    assert calls == []


def test_workspace_mount_specs_parse_bare_and_explicit(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    explicit = tmp_path / "explicit"
    bare.mkdir()
    explicit.mkdir()

    assert cli._workspace_mounts_from_specs(
        [str(bare), f"{explicit}:/workspace"], writable=True
    ) == [
        {"host_path": str(bare), "guest_path": str(bare), "mount_tag": None, "writable": True},
        {
            "host_path": str(explicit),
            "guest_path": "/workspace",
            "mount_tag": None,
            "writable": True,
        },
    ]


def test_workspace_mount_specs_reject_duplicate_guest_paths(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    with pytest.raises(cli.ConfigError, match="duplicate mount guest path"):
        cli._workspace_mounts_from_specs([f"{one}:/workspace", f"{two}:/workspace"], writable=True)


def test_sync_existing_vm_mounts_missing_db_or_row_does_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._sync_existing_vm_mounts_from_config("missing", [], writable_mounts=False)
    assert capsys.readouterr().out == ""

    with sqlite3.connect(cli.SMOLVM_DB_PATH) as conn:
        conn.execute("CREATE TABLE vms (id TEXT PRIMARY KEY, status TEXT, config TEXT)")
    cli._sync_existing_vm_mounts_from_config("missing", [], writable_mounts=False)
    assert capsys.readouterr().out == ""


def test_start_local_image_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_post_start_actions",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    args = type("Args", (), {"name": "vm-from-cli", "memory": None})()

    rc = cli._start_local_image(
        args=args,
        config={"sbx": {}},
        image_dir=local_image_dir,
        manifest=cli._local_image_manifest(local_image_dir),
        agent="pi",
        mounts=[".:/workspace"],
        writable_mounts=True,
        attach=True,
        run_user="agent",
        auth_port=True,
        auth_host_port=1455,
        auth_guest_port=1455,
        stop_on_exit=False,
        cwd="/workspace",
        git_config_text="[user]\n",
    )

    assert rc == 0
    assert fake_smolvm_sdk["started"] is True
    assert fake_smolvm_sdk["waited"] is True
    assert fake_smolvm_sdk["start_kwargs"] == {"boot_timeout": 30.0}
    assert fake_smolvm_sdk["wait_kwargs"] == {"timeout": 30.0}
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["vm_id"] == "vm-from-cli"
    assert vm_config["boot_args"] == "boot args"
    assert vm_config["ssh_public_key"] == "ssh-ed25519 public"
    assert captured["launch_command"] == "custom-pi"
    assert captured["git_config_text"] == "[user]\n"


def test_run_helpers_and_require_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(cli.subprocess, "run", missing_run)
    assert cli._run(["missing"]) == 127
    assert cli._run_capture(["missing"]) is None
    assert "command not found" in capsys.readouterr().err

    def called_process_error(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(9, ["cmd"])

    monkeypatch.setattr(cli.subprocess, "run", called_process_error)
    assert cli._run(["cmd"]) == 9

    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    assert cli._require("missing", "install it") is False
    assert "install it" in capsys.readouterr().err


def test_simple_helper_error_branches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    assert cli._resolve_project_path(str(tmp_path / "missing")) == tmp_path / "missing"
    assert cli._same_path_mount(str(tmp_path)) == f"{tmp_path}:{tmp_path}"
    with pytest.raises(cli.ConfigError, match="run_user"):
        cli._validate_run_user("bad user")
    with pytest.raises(cli.ConfigError, match="invalid env var"):
        cli._validate_env_names(["1BAD"])
    cli._print_start_failure('{"error":{"message":"QEMU exited early"}}')
    assert "Try `sbx recreate" in capsys.readouterr().err


def test_ssh_command_and_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeVm:
        def _ssh_direct_command(self) -> list[str]:
            return ["ssh", "root@host"]

        def close(self) -> None:
            pass

    class FakeSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> FakeVm:
            return FakeVm()

    monkeypatch.setattr(smolvm.facade, "SmolVM", FakeSmolVM)
    assert cli._ssh_command("vm1") == ["ssh", "root@host"]

    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    assert cli._pid_is_alive(123) is True

    def missing_process(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(cli.os, "kill", missing_process)
    assert cli._pid_is_alive(123) is False


def test_ssh_command_missing_vm_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from smolvm.exceptions import VMNotFoundError

    class MissingSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> object:
            raise VMNotFoundError(vm_id)

    monkeypatch.setattr(smolvm.facade, "SmolVM", MissingSmolVM)

    with pytest.raises(cli.ConfigError, match="VM 'reviewhero' not found"):
        cli._ssh_command("reviewhero")


def test_shell_prechecks_missing_managed_vm_before_registering_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "reviewhero"\nrun_user = "agent"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: None)

    def fail_register(vm_id: str, kind: str) -> None:
        raise AssertionError("session should not be registered for a missing VM")

    monkeypatch.setattr(cli, "_register_session", fail_register)

    rc = cli.main(["--config", str(config), "shell"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "VM 'reviewhero' not found" in captured.err
    assert "Traceback" not in captured.err


def test_shell_reports_missing_vm_from_smolvm_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from smolvm.exceptions import VMNotFoundError

    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "reviewhero"\nrun_user = "agent"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(cli, "_host_git_config", lambda: None)
    monkeypatch.setattr(cli, "_stop_vm_if_last_session", lambda vm_id, *, stop_on_exit: None)

    class MissingSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> object:
            raise VMNotFoundError(vm_id)

    monkeypatch.setattr(smolvm.facade, "SmolVM", MissingSmolVM)

    rc = cli.main(["--config", str(config), "shell"])

    captured = capsys.readouterr()
    assert rc == 2
    assert "sbx: VM 'reviewhero' not found" in captured.err
    assert "Traceback" not in captured.err


def test_start_local_image_closes_on_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    closed: list[bool] = []

    class FailingSmolVM:
        vm_id = "vm1"

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("boom")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(smolvm, "SmolVM", FailingSmolVM)
    args = type("Args", (), {"name": None, "memory": None})()
    with pytest.raises(RuntimeError, match="boom"):
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=cli._local_image_manifest(local_image_dir),
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
    assert closed == [True]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ({"rootfs": "rootfs.ext4"}, "'kernel'"),
        ({"kernel": "vmlinux.bin"}, "'rootfs'"),
        (
            {"kernel": "missing", "rootfs": "rootfs.ext4"},
            "image kernel not found",
        ),
        (
            {"kernel": "vmlinux.bin", "rootfs": "missing"},
            "image rootfs not found",
        ),
        (
            {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": []},
            "'sbx' must be an object",
        ),
        (
            {
                "kernel": "vmlinux.bin",
                "rootfs": "rootfs.ext4",
                "sbx": {"agent": "codex"},
            },
            "does not match configured agent",
        ),
        (
            {
                "kernel": "vmlinux.bin",
                "rootfs": "rootfs.ext4",
                "sbx": {"launch_command": []},
            },
            "launch_command",
        ),
        (
            {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "boot_args": []},
            "boot_args",
        ),
    ],
)
def test_start_local_image_validation_errors(
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
    manifest: dict[str, object],
    message: str,
) -> None:
    args = type("Args", (), {"name": None})()

    with pytest.raises(cli.ConfigError, match=message):
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )


def test_local_image_manifest_errors(tmp_path: Path) -> None:
    with pytest.raises(cli.ConfigError, match="must point to a local image directory"):
        cli._local_image_manifest(tmp_path / "missing")

    image = tmp_path / "image"
    image.mkdir()
    with pytest.raises(cli.ConfigError, match="manifest not found"):
        cli._local_image_manifest(image)

    (image / "smolvm-image.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="invalid image manifest JSON"):
        cli._local_image_manifest(image)

    (image / "smolvm-image.json").write_text("[]", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="must be a JSON object"):
        cli._local_image_manifest(image)


def test_git_config_missing_git_and_escaping(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(cli.subprocess, "run", missing_git)
    assert cli._host_git_config() is None

    values = {"user.name": 'Ada "Back\\slash"', "user.email": "multi\nline"}

    def fake_git(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        value = values.get(argv[-1])
        if value is None:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_git)
    assert cli._host_git_config() == '[user]\n\tname = "Ada \\"Back\\\\slash\\""\n'


def test_install_git_config_for_root_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])

    def fake_run_capture(argv: list[str], *, env: dict[str, str] | None = None):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run_capture", fake_run_capture)

    cli._install_git_config("vm1", None, "[user]\n")
    cli._install_git_config("vm1", "agent", "[user]\n")
    cli._install_git_config("vm1", "agent", None)

    assert "/root/.gitconfig" in commands[0][-1]
    assert "/home/agent/.gitconfig" in commands[1][-1]
    assert len(commands) == 2


def test_prepare_run_user_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])
    captured: dict[str, object] = {}

    def fake_ok(argv: list[str], *, env: dict[str, str] | None = None):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run_capture", fake_ok)
    cli._prepare_run_user("vm1", "agent")
    assert "getent hosts" in captured["argv"][-1]
    assert "127.0.1.1" in captured["argv"][-1]
    assert ".ssh .pi .codex .claude .claude.json" in captured["argv"][-1]

    monkeypatch.setattr(cli, "_run_capture", lambda argv: None)
    with pytest.raises(cli.ConfigError, match="ssh command not found"):
        cli._prepare_run_user("vm1", "agent")

    def fake_fail(argv: list[str], *, env: dict[str, str] | None = None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad")

    monkeypatch.setattr(cli, "_run_capture", fake_fail)
    with pytest.raises(cli.ConfigError, match="bad"):
        cli._prepare_run_user("vm1", "agent")


def test_attach_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(cli, "_run", lambda argv: commands.append(list(argv)) or 0)

    assert cli._attach_as_root("vm1", "pi", cwd="/workspace") == 0
    assert cli._attach_as_user("vm1", "agent", "pi", cwd="/workspace") == 0

    assert "-t" in commands[0]
    assert "cd /workspace" in commands[0][-1]
    assert "exec pi" in commands[0][-1]
    assert "sudo -iu agent" in commands[1][-1]


def test_post_start_actions_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(cli, "_expose_auth_port", lambda *args: calls.append(("port", args)) or 0)
    monkeypatch.setattr(cli, "_register_session", lambda *args: calls.append(("register", args)))
    monkeypatch.setattr(
        cli, "_unregister_session", lambda *args: calls.append(("unregister", args))
    )
    monkeypatch.setattr(
        cli, "_stop_vm_if_last_session", lambda *args, **kwargs: calls.append(("stop", kwargs))
    )
    monkeypatch.setattr(cli, "_prepare_run_user", lambda *args: calls.append(("prepare", args)))
    monkeypatch.setattr(cli, "_install_git_config", lambda *args: calls.append(("git", args)))
    monkeypatch.setattr(
        cli, "_attach_as_user", lambda *args, **kwargs: calls.append(("user", args)) or 0
    )
    monkeypatch.setattr(
        cli, "_attach_as_root", lambda *args, **kwargs: calls.append(("root", args)) or 0
    )

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            agent="pi",
            attach=False,
            run_user=None,
            auth_port=True,
            auth_host_port=1,
            auth_guest_port=2,
            stop_on_exit=True,
        )
        == 0
    )
    assert calls == [("port", ("vm1", 1, 2))]

    calls.clear()
    assert (
        cli._post_start_actions(
            vm_name="vm1",
            agent="pi",
            attach=True,
            run_user="agent",
            auth_port=False,
            auth_host_port=1,
            auth_guest_port=2,
            stop_on_exit=True,
            git_config_text="git",
        )
        == 0
    )
    assert [name for name, _ in calls] == [
        "register",
        "prepare",
        "git",
        "user",
        "unregister",
        "stop",
    ]


def test_tunnel_and_session_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_pid_is_alive", lambda pid: pid == 123)
    cli._record_auth_tunnel("vm1", pid=123, host_port=1455, guest_port=1455)
    assert cli._tracked_auth_tunnel("vm1") == {"pid": 123, "host_port": 1455, "guest_port": 1455}
    assert cli._tracked_auth_tunnel_for_host_port(1455) == (
        "vm1",
        {"pid": 123, "host_port": 1455, "guest_port": 1455},
    )
    assert cli._tracked_auth_tunnel_for_host_port(9999) is None
    cli._remove_auth_tunnel_record("vm1")
    assert cli._tracked_auth_tunnel("vm1") is None
    cli._save_tunnels({"bad": [], "dead": {"auth_port": {"pid": 999, "host_port": 1}}})
    assert cli._tracked_auth_tunnel_for_host_port(1) is None

    cli._save_sessions(
        {"vm1": {"sessions": [{"pid": 123, "kind": "run"}, {"pid": 999, "kind": "run"}]}}
    )
    assert cli._active_sessions("vm1") == [{"pid": 123, "kind": "run"}]

    stopped: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_smolvm", lambda argv: stopped.append(list(argv)) or 0)
    cli._stop_vm_if_last_session("vm1", stop_on_exit=False)
    assert stopped == []
    cli._save_sessions({})
    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)
    assert stopped == [["sandbox", "stop", "vm1"]]


def test_expose_auth_port_error_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "_tracked_auth_tunnel", lambda vm_id: {"pid": 123, "host_port": 1, "guest_port": 2}
    )
    assert cli._expose_auth_port("vm1", 1, 2) == 0

    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli, "_tracked_auth_tunnel_for_host_port", lambda port: ("vm2", {}))
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: True)
    assert cli._expose_auth_port("vm1", 1, 2) == 0
    assert "VM 'vm2'" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_tracked_auth_tunnel_for_host_port", lambda port: None)
    assert cli._expose_auth_port("vm1", 1, 2) == 0
    assert "not tracked by sbx" in capsys.readouterr().err

    class ExitedProcess:
        pid = 123
        returncode = 1
        stderr = None

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: False)
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    assert cli._expose_auth_port("vm1", 1, 2) == 1


def test_delete_vm_error_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_run_capture", lambda argv: None)
    assert cli._delete_vm("vm1") == 127

    not_found = json.dumps({"data": {"failed": [{"error": "VM 'vm1' not found"}]}})
    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 1, stdout=not_found, stderr="warn\n"),
    )
    assert cli._delete_vm("vm1") == 0
    captured = capsys.readouterr()
    assert "warn" in captured.err
    assert "nothing to destroy" in captured.out

    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 3, stdout="not-json", stderr=""),
    )
    assert cli._delete_vm("vm1") == 3
    assert "not-json" in capsys.readouterr().out


def test_network_status_variants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 1, stdout="bad out\n", stderr="bad err\n"),
    )
    assert cli.cmd_network_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 1
    captured = capsys.readouterr()
    assert "bad out" in captured.out
    assert "bad err" in captured.err

    payload = json.dumps(
        {
            "data": {
                "vm": {
                    "name": "vm1",
                    "status": "running",
                    "backend": "qemu",
                    "ip_address": "10.0.2.15",
                    "ssh_port": 2200,
                }
            }
        }
    )
    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""),
    )
    monkeypatch.setattr(
        cli, "_tracked_auth_tunnel", lambda name: {"pid": 123, "host_port": 1, "guest_port": 2}
    )
    assert cli.cmd_network_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
    assert "active" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda name: None)
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: True)
    assert cli.cmd_network_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
    assert "busy/untracked" in capsys.readouterr().out


def test_config_and_validation_error_branches(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="invalid TOML"):
        cli._read_toml(bad)
    with pytest.raises(cli.ConfigError, match="config file does not exist"):
        cli.load_config(str(tmp_path / "missing.toml"))
    with pytest.raises(cli.ConfigError, match="must be a TOML table"):
        cli._section({"sbx": []}, "sbx")
    with pytest.raises(cli.ConfigError, match="must be a string or an array"):
        cli._list_value(1, key="x")
    with pytest.raises(cli.ConfigError, match="must be a string or an array"):
        cli._list_value(["ok", 1], key="x")
    with pytest.raises(cli.ConfigError, match="run_user"):
        cli._validate_run_user("bad user")
    with pytest.raises(cli.ConfigError, match="invalid env var"):
        cli._validate_env_names(["BAD-NAME"])
    with pytest.raises(cli.ConfigError, match="could not read"):
        cli._extract_started_vm_name("not-json")
    with pytest.raises(cli.ConfigError, match="did not include"):
        cli._extract_started_vm_name('{"data":{"vm":{"name":""}}}')


def test_command_edge_cases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_run_smolvm", lambda *args, **kwargs: 7)
    assert cli.cmd_doctor(type("Args", (), {})()) == 7

    monkeypatch.setattr(cli, "_confirm_destructive_action", lambda *args, **kwargs: False)
    args = type(
        "Args", (), {"config_data": {"sbx": {}}, "force": False, "name": None, "name_arg": None}
    )()
    assert cli.cmd_recreate(args) == 2
    assert "requires a VM name" in capsys.readouterr().err

    args.name = "vm1"
    assert cli.cmd_recreate(args) == 2

    monkeypatch.setattr(cli, "_confirm_destructive_action", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli, "_delete_vm", lambda name: 7)
    assert cli.cmd_recreate(args) == 7


def test_main_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_config", lambda path: (_ for _ in ()).throw(cli.ConfigError("bad"))
    )
    assert cli.main(["doctor"]) == 2
    assert "sbx: bad" in capsys.readouterr().err


def test_expose_auth_port_missing_ssh_oserror_and_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: False)
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert cli._expose_auth_port("vm1", 1, 2) == 127
    assert "command not found: ssh" in capsys.readouterr().err

    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("nope"))
    )
    assert cli._expose_auth_port("vm1", 1, 2) == 1
    assert "failed to start auth port tunnel" in capsys.readouterr().err

    class RunningProcess:
        pid = 123
        stderr = None

        def poll(self) -> None:
            return None

    times = iter([0.0, 6.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    assert cli._expose_auth_port("vm1", 1, 2) == 1
    assert killed == [(123, cli.signal.SIGTERM)]
    assert "did not become ready" in capsys.readouterr().err


def test_close_auth_port_kills_tracked_tunnel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda name: {"pid": 123})
    removed: list[str] = []
    monkeypatch.setattr(cli, "_remove_auth_tunnel_record", lambda name: removed.append(name))
    states = iter([True, False, False])
    monkeypatch.setattr(cli, "_pid_is_alive", lambda pid: next(states))
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert cli.cmd_close_auth_port(type("Args", (), {"name": "vm1"})()) == 0
    assert killed == [(123, cli.signal.SIGTERM)]
    assert removed == ["vm1"]
    assert "Closed auth port" in capsys.readouterr().out


def test_network_status_inactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.dumps(
        {
            "data": {
                "vm": {
                    "name": "vm1",
                    "status": "running",
                    "backend": "qemu",
                    "ip_address": "10.0.2.15",
                    "ssh_port": 2200,
                }
            }
        }
    )
    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""),
    )
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda name: None)
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: False)

    assert cli.cmd_network_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
    assert "Auth callback: inactive" in capsys.readouterr().out


def test_start_local_image_defaults_and_no_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4"}
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: captured.update(kwargs) or 0)
    args = type("Args", (), {"name": None, "memory": None})()

    assert (
        cli._start_local_image(
            args=args,
            config={"sbx": {"name": "from-config"}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
        == 0
    )
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["vm_id"] == "from-config"
    assert vm_config["boot_args"].endswith("init=/init")
    assert captured["attach"] is False
    assert "Started 'local-vm'." in capsys.readouterr().out


def test_read_toml_oserror_and_create_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(cli.ConfigError, match="Is a directory|Permission denied"):
        cli._read_toml(tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "cmd_start", lambda args: captured.update(vars(args)) or 0)
    args = type("Args", (), {"attach": True, "auth_port": None})()
    assert cli.cmd_create(args) == 0
    assert captured["attach"] is False
    assert captured["auth_port"] is False


def test_post_start_actions_auth_port_failure_skips_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_expose_auth_port", lambda *args: calls.append("port") or 9)
    monkeypatch.setattr(cli, "_attach_as_root", lambda *args, **kwargs: calls.append("attach") or 0)

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            agent="pi",
            attach=True,
            run_user=None,
            auth_port=True,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
        )
        == 9
    )
    assert calls == ["port"]


def test_close_auth_port_escalates_to_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda name: {"pid": 123})
    monkeypatch.setattr(cli, "_remove_auth_tunnel_record", lambda name: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    times = iter([0.0, 4.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    alive = iter([True, True])
    monkeypatch.setattr(cli, "_pid_is_alive", lambda pid: next(alive))
    killed: list[int] = []
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: killed.append(sig))

    assert cli.cmd_close_auth_port(type("Args", (), {"name": "vm1"})()) == 0
    assert killed == [cli.signal.SIGTERM, cli.signal.SIGKILL]


def test_stop_vm_skips_when_other_sessions_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_active_sessions", lambda vm_id: [{"pid": 123, "kind": "run"}])
    stopped: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda argv: stopped.append(list(argv)) or 0)

    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)

    assert stopped == []


def test_delete_vm_success_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "_run_capture",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout='{"data": {}}', stderr=""),
    )

    assert cli._delete_vm("vm1") == 0
    assert "Destroyed VM 'vm1'" in capsys.readouterr().out


def test_local_image_without_launch_command_uses_agent_fallback(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    captured: dict[str, object] = {}
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {"agent": "pi"}}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: captured.update(kwargs) or 0)
    args = type("Args", (), {"name": None, "memory": 1024, "cpus": None})()

    assert (
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=True,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
        == 0
    )
    assert captured["launch_command"] is None
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["memory"] == 1024
    assert "vcpu_count" not in vm_config


def test_local_image_configures_cpus(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {"agent": "pi"}}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)
    args = type("Args", (), {"name": None, "memory": 1024, "cpus": 3})()

    assert (
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
        == 0
    )
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["vcpu_count"] == 3


def test_local_image_configures_disk_size_and_growth(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {"agent": "pi"}}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)
    args = type("Args", (), {"name": None, "memory": 1024, "cpus": None, "disk_size": None})()

    assert (
        cli._start_local_image(
            args=args,
            config={"sbx": {"disk_size": 40960}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
        == 0
    )
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["disk_size_mib"] == 40960
    assert vm_config["grow_filesystem"] is True


def test_local_image_rejects_disk_size_smaller_than_rootfs(
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    del fake_smolvm_sdk
    rootfs = local_image_dir / "rootfs.ext4"
    with rootfs.open("wb") as fh:
        fh.truncate(80 * 1024 * 1024)
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {"agent": "pi"}}
    args = type("Args", (), {"name": None, "memory": 1024, "cpus": None, "disk_size": 10})()

    with pytest.raises(cli.ConfigError) as excinfo:
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )

    message = str(excinfo.value)
    assert "configured disk_size is smaller than the local image rootfs" in message
    assert "disk_size: 10 MiB" in message
    assert "local image rootfs: 80 MiB" in message
    assert "Set [sbx].disk_size to at least 80" in message
    assert "rebuild the configured local image with a rootfs no larger than 10 MiB" in message


def test_local_image_qcow2_disk_size_does_not_request_filesystem_growth(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    (local_image_dir / "rootfs.qcow2").write_text("rootfs", encoding="utf-8")
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.qcow2", "sbx": {"agent": "pi"}}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)
    args = type("Args", (), {"name": None, "memory": 1024, "cpus": None, "disk_size": 40960})()

    assert (
        cli._start_local_image(
            args=args,
            config={"sbx": {}},
            image_dir=local_image_dir,
            manifest=manifest,
            agent="pi",
            mounts=[],
            writable_mounts=False,
            attach=False,
            run_user=None,
            auth_port=False,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
            cwd=None,
            git_config_text=None,
        )
        == 0
    )
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["disk_size_mib"] == 40960
    assert "grow_filesystem" not in vm_config


def test_network_status_run_capture_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_run_capture", lambda argv: None)

    assert cli.cmd_network_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 127


def test_confirm_destructive_action_noninteractive_and_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NonInteractive:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonInteractive())
    assert cli._confirm_destructive_action("Destroy?", force=False) is False
    assert "refusing destructive action" in capsys.readouterr().err
    assert cli._confirm_destructive_action("Destroy?", force=True) is True

    class Interactive:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", Interactive())
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    assert cli._confirm_destructive_action("Destroy?", force=False) is True


def test_recreate_success_deletes_then_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(cli, "_confirm_destructive_action", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli, "_delete_vm", lambda name: calls.append(("delete", name)) or 0)
    monkeypatch.setattr(cli, "cmd_start", lambda args: calls.append(("start", args.name)) or 0)
    args = type(
        "Args",
        (),
        {
            "config_data": {"sbx": {}},
            "force": False,
            "name": "vm1",
            "name_arg": None,
            "auth_port": None,
        },
    )()

    assert cli.cmd_recreate(args) == 0
    assert calls == [("delete", "vm1"), ("start", "vm1")]
    assert args.attach is False
    assert args.auth_port is False


def test_start_existing_vm_if_needed_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_smolvm", lambda argv: calls.append(list(argv)) or 0)
    assert cli._start_existing_vm_if_needed("vm1", "running", 60) == 0
    assert calls == []
    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 0
    assert calls == [["sandbox", "start", "vm1", "--boot-timeout", "60"]]
    assert cli._start_existing_vm_if_needed("vm1", "error", 60) == 1


def test_start_existing_vm_timeout_hint_when_vm_is_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_run", lambda argv: 1)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 1

    err = capsys.readouterr().err
    assert "VM 'vm1' started, but SSH was not ready within 60s" in err
    assert "sbx run vm1 --boot-timeout" in err


def test_auth_tunnel_success_records_when_port_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_tracked_auth_tunnel", lambda vm_id: None)
    readiness = iter([False, True])
    monkeypatch.setattr(cli, "_localhost_port_is_listening", lambda port: next(readiness))
    monkeypatch.setattr(cli, "_ssh_command", lambda vm_id: ["ssh", "root@host"])
    recorded: list[tuple[str, int, int, int]] = []
    monkeypatch.setattr(
        cli,
        "_record_auth_tunnel",
        lambda vm_id, *, pid, host_port, guest_port: recorded.append(
            (vm_id, pid, host_port, guest_port)
        ),
    )

    class RunningProcess:
        pid = 321

        def poll(self) -> None:
            return None

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())

    assert cli._expose_auth_port("vm1", 1455, 1455) == 0
    assert recorded == [("vm1", 321, 1455, 1455)]


def test_pid_is_alive_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def permission_error(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(cli.os, "kill", permission_error)
    assert cli._pid_is_alive(123) is True


def test_unregister_session_keeps_other_active_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.os, "getpid", lambda: 111)
    monkeypatch.setattr(cli, "_pid_is_alive", lambda pid: pid == 222)
    cli._save_sessions(
        {"vm1": {"sessions": [{"pid": 111, "kind": "run"}, {"pid": 222, "kind": "shell"}]}}
    )

    cli._unregister_session("vm1")

    assert cli._load_sessions() == {"vm1": {"sessions": [{"pid": 222, "kind": "shell"}]}}


def test_network_auth_port_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "_expose_auth_port",
        lambda name, host, guest, *, replace=False: (name, host, guest, replace),
    )
    args = type("Args", (), {"name": "vm1", "host_port": 1, "guest_port": 2, "replace": True})()
    assert cli.cmd_auth_port(args) == ("vm1", 1, 2, True)
