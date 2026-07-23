import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest
import smolvm
import smolvm.facade
import smolvm.utils

from sbx import cli, guest_setup, lifecycle_warnings, runtime, session_state, vm_metadata, vm_state


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.runtime, "DEBUG", False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (tmp_path / "home-config.toml",))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (tmp_path / ".sbx.toml",))
    monkeypatch.setattr(vm_metadata, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vm_metadata, "SBX_VMS_FILE", tmp_path / "state" / "vms.json")
    monkeypatch.setattr(session_state, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(session_state, "SESSIONS_FILE", tmp_path / "state" / "sessions.json")
    monkeypatch.setattr(cli.network, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli.network, "TUNNELS_FILE", tmp_path / "state" / "tunnels.json")
    monkeypatch.setattr(cli, "SMOLVM_DB_PATH", tmp_path / "smolvm.db")
    monkeypatch.setattr(vm_state, "SMOLVM_DB_PATH", tmp_path / "smolvm.db")
    monkeypatch.setattr(guest_setup, "set_hostname", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli.smolvm_preset,
        "create_preset",
        lambda preset_name, **kwargs: SimpleNamespace(
            vm_id=kwargs.get("vm_name") or f"{preset_name}-sbx", close=lambda: None
        ),
    )


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


def test_vm_metadata_load_save_and_project_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = project / ".sbx.toml"
    config.write_text('[sbx]\nproject_path = "."\n', encoding="utf-8")
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (config,))
    monkeypatch.chdir(project)

    args = SimpleNamespace(config=None)
    identity = cli._project_identity(args, {"sbx": {"project_path": "."}})
    vm_metadata.record_vm_project("vm1", identity)

    assert vm_metadata.load_vm_metadata() == {"vm1": identity}
    assert identity["project_root"] == str(project.resolve())
    assert identity["config_path"] == str(config.resolve())


def test_vm_metadata_corrupt_json_raises() -> None:
    vm_metadata.SBX_VMS_FILE.parent.mkdir(parents=True)
    vm_metadata.SBX_VMS_FILE.write_text("not json", encoding="utf-8")

    with pytest.raises(cli.ConfigError, match="invalid sbx VM metadata"):
        vm_metadata.load_vm_metadata()


def test_sync_existing_vm_mounts_rejects_wrong_project(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_vm_row(cli.SMOLVM_DB_PATH, "vm1", status="stopped", config={"workspace_mounts": []})
    vm_metadata.record_vm_project(
        "vm1", {"project_root": str(old), "config_path": str(old / ".sbx.toml")}
    )

    with pytest.raises(cli.ConfigError, match="belongs to"):
        cli._sync_existing_vm_start_config(
            "vm1",
            [f"{new}:/workspace"],
            writable_mounts=True,
            port_forwards=[],
            project={"project_root": str(new), "config_path": str(new / ".sbx.toml")},
        )

    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1") == {"workspace_mounts": []}


def test_warn_running_mount_drift_does_not_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = tmp_path / "project"
    host.mkdir()
    config = {"workspace_mounts": [{"host_path": "/old", "guest_path": "/old"}]}
    _write_vm_row(cli.SMOLVM_DB_PATH, "vm1", status="running", config=config)

    cli._warn_running_mount_drift("vm1", [f"{host}:/workspace"], writable_mounts=True)

    assert "mounts that differ" in capsys.readouterr().err
    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1") == config


def test_host_git_config_uses_repo_local_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["git", "-C", str(tmp_path)] and argv[-1] == "user.email":
            return subprocess.CompletedProcess(argv, 0, stdout="repo@example.com\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(guest_setup.subprocess, "run", fake_run)

    assert guest_setup.host_git_config(tmp_path) == '[user]\n\temail = "repo@example.com"\n'
    assert ["git", "-C", str(tmp_path), "config", "--get", "user.email"] in calls


def test_doctor_fix_repairs_local_bookkeeping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    vm_metadata.record_vm_project(
        "vm1", {"project_root": str(missing), "config_path": str(missing / ".sbx.toml")}
    )
    session_state.save_sessions({"vm1": {"sessions": [{"pid": -1, "kind": "run"}]}})
    cli.network._save_tunnels({"vm1": {"auth_port": {"pid": -1, "host_port": 1}}})
    monkeypatch.setattr(vm_state, "smolvm_vms", lambda all_vms=True: [])
    monkeypatch.setattr(cli.runtime, "run_smolvm", lambda argv: 0)

    assert cli.main(["doctor", "--fix"]) == 0

    assert vm_metadata.load_vm_metadata() == {}
    assert session_state.load_sessions() == {}
    assert cli.network._load_tunnels() == {}


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

    cli._sync_existing_vm_start_config(
        "vm1", [f"{host}:/workspace"], writable_mounts=True, port_forwards=[]
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

    cli._sync_existing_vm_start_config(
        "vm1", [f"{host}:/workspace"], writable_mounts=False, port_forwards=[]
    )

    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1") == config
    assert capsys.readouterr().out == ""


def test_cmd_start_syncs_env_before_existing_vm_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\nenv = ["SBX_TOKEN"]\n', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(
        cli, "_start_existing_vm_if_needed", lambda *args, **kwargs: calls.append("start") or 0
    )
    monkeypatch.setattr(guest_setup, "host_git_config", lambda project_root=None: None)
    monkeypatch.setattr(
        guest_setup, "sync_forwarded_env", lambda *args, **kwargs: calls.append("sync")
    )
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: calls.append("attach") or 0)

    assert cli.main(["--config", str(config), "run"]) == 0
    assert calls == ["start", "sync", "attach"]


def test_sync_existing_vm_start_config_updates_port_forwards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_vm_row(cli.SMOLVM_DB_PATH, "vm1", status="stopped", config={})

    cli._sync_existing_vm_start_config(
        "vm1", None, writable_mounts=False, port_forwards=["0.0.0.0:3000:3000"]
    )

    assert _read_vm_config(cli.SMOLVM_DB_PATH, "vm1")["port_forwards"] == [
        {"host_address": "0.0.0.0", "host_port": 3000, "guest_port": 3000}
    ]
    assert capsys.readouterr().out == "sbx: updated port forwards for existing VM 'vm1'\n"


def test_cmd_start_does_not_sync_running_vm_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda project_root=None: None)
    monkeypatch.setattr(
        cli,
        "_sync_existing_vm_start_config",
        lambda *args, **kwargs: calls.append("sync"),
    )
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: 0)

    assert cli.main(["--config", str(config), "run"]) == 0
    assert calls == []


def test_port_forward_specs_parse_forms() -> None:
    assert cli.network.port_forwards_from_specs(["3000", "8080:3000", "0.0.0.0:3000:3000"]) == [
        {"host_address": "127.0.0.1", "host_port": 3000, "guest_port": 3000},
        {"host_address": "127.0.0.1", "host_port": 8080, "guest_port": 3000},
        {"host_address": "0.0.0.0", "host_port": 3000, "guest_port": 3000},
    ]


@pytest.mark.parametrize("spec", ["0", "65536", "abc", "1:2:3:4", ":3000:3000"])
def test_port_forward_specs_reject_invalid(spec: str) -> None:
    with pytest.raises(cli.ConfigError):
        cli.network.parse_port_forward(spec)


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
    cli._sync_existing_vm_start_config("missing", [], writable_mounts=False, port_forwards=[])
    assert capsys.readouterr().out == ""

    with sqlite3.connect(cli.SMOLVM_DB_PATH) as conn:
        conn.execute("CREATE TABLE vms (id TEXT PRIMARY KEY, status TEXT, config TEXT)")
    cli._sync_existing_vm_start_config("missing", [], writable_mounts=False, port_forwards=[])
    assert capsys.readouterr().out == ""


def test_start_local_image_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
) -> None:
    captured: dict[str, object] = {}
    calls: list[str] = []
    monkeypatch.setattr(
        guest_setup, "sync_forwarded_env", lambda *args, **kwargs: calls.append("sync")
    )
    monkeypatch.setattr(
        cli,
        "_post_start_actions",
        lambda **kwargs: calls.append("attach") or captured.update(kwargs) or 0,
    )
    args = type("Args", (), {"name": "vm-from-cli", "memory": None})()

    rc = cli._start_local_image(
        args=args,
        config={"sbx": {}},
        image_dir=local_image_dir,
        manifest=lifecycle_warnings.local_image_manifest(local_image_dir),
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
        forward_env=["SBX_TOKEN"],
        port_forwards=["8080:3000"],
    )

    assert rc == 0
    assert calls == ["sync", "attach"]
    assert fake_smolvm_sdk["started"] is True
    assert fake_smolvm_sdk["waited"] is True
    assert fake_smolvm_sdk["start_kwargs"] == {"boot_timeout": 60.0}
    assert fake_smolvm_sdk["wait_kwargs"] == {"timeout": 60.0}
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["vm_id"] == "vm-from-cli"
    assert vm_config["boot_args"] == "boot args"
    assert vm_config["comm_channel"] == "ssh"
    assert vm_config["ssh_public_key"] == "ssh-ed25519 public"
    assert vm_config["port_forwards"] == [
        {"host_address": "127.0.0.1", "host_port": 8080, "guest_port": 3000}
    ]
    assert captured["command"] == "custom-pi"
    assert captured["git_config_text"] == "[user]\n"


def test_run_helpers_and_require_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(runtime.subprocess, "run", missing_run)
    assert runtime.run(["missing"]) == 127
    assert runtime.run_capture(["missing"]) is None
    assert "command not found" in capsys.readouterr().err

    def called_process_error(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(9, ["cmd"])

    monkeypatch.setattr(runtime.subprocess, "run", called_process_error)
    assert runtime.run(["cmd"]) == 9


def test_simple_helper_error_branches(tmp_path: Path) -> None:
    assert cli._deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    assert cli._resolve_project_path(str(tmp_path / "missing")) == tmp_path / "missing"
    assert cli._same_path_mount(str(tmp_path)) == f"{tmp_path}:{tmp_path}"
    with pytest.raises(cli.ConfigError, match="run_user"):
        guest_setup.validate_run_user("bad user")
    with pytest.raises(cli.ConfigError, match="invalid env var"):
        guest_setup.validate_env_names(["1BAD"])


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
    assert guest_setup.ssh_command("vm1") == ["ssh", "root@host"]

    monkeypatch.setattr(session_state.os, "kill", lambda pid, sig: None)
    assert session_state.pid_is_alive(123) is True

    def missing_process(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(session_state.os, "kill", missing_process)
    assert session_state.pid_is_alive(123) is False


def test_ssh_command_missing_vm_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from smolvm.exceptions import VMNotFoundError

    class MissingSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> object:
            raise VMNotFoundError(vm_id)

    monkeypatch.setattr(smolvm.facade, "SmolVM", MissingSmolVM)

    with pytest.raises(cli.ConfigError, match="VM 'the-quest' not found"):
        guest_setup.ssh_command("the-quest")


def test_shell_prechecks_missing_managed_vm_before_registering_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "the-quest"\nrun_user = "agent"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: None)

    def fail_register(vm_id: str, kind: str) -> None:
        raise AssertionError("session should not be registered for a missing VM")

    monkeypatch.setattr(session_state, "register_session", fail_register)

    rc = cli.main(["--config", str(config), "shell"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "VM 'the-quest' not found" in captured.err
    assert "Traceback" not in captured.err


def test_shell_reports_missing_vm_from_smolvm_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from smolvm.exceptions import VMNotFoundError

    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "the-quest"\nrun_user = "agent"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda project_root=None: None)
    monkeypatch.setattr(cli, "_stop_vm_if_last_session", lambda vm_id, *, stop_on_exit: None)

    class MissingSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> object:
            raise VMNotFoundError(vm_id)

    monkeypatch.setattr(smolvm.facade, "SmolVM", MissingSmolVM)

    rc = cli.main(["--config", str(config), "shell"])

    captured = capsys.readouterr()
    assert rc == 2
    assert "sbx: VM 'the-quest' not found" in captured.err
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
            manifest=lifecycle_warnings.local_image_manifest(local_image_dir),
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
        lifecycle_warnings.local_image_manifest(tmp_path / "missing")

    image = tmp_path / "image"
    image.mkdir()
    with pytest.raises(cli.ConfigError, match="manifest not found"):
        lifecycle_warnings.local_image_manifest(image)

    (image / "smolvm-image.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="invalid image manifest JSON"):
        lifecycle_warnings.local_image_manifest(image)

    (image / "smolvm-image.json").write_text("[]", encoding="utf-8")
    with pytest.raises(cli.ConfigError, match="must be a JSON object"):
        lifecycle_warnings.local_image_manifest(image)


def test_git_config_missing_git_and_escaping(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(guest_setup.subprocess, "run", missing_git)
    assert guest_setup.host_git_config() is None

    values = {"user.name": 'Ada "Back\\slash"', "user.email": "multi\nline"}

    def fake_git(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        value = values.get(argv[-1])
        if value is None:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(guest_setup.subprocess, "run", fake_git)
    assert guest_setup.host_git_config() == '[user]\n\tname = "Ada \\"Back\\\\slash\\""\n'


def test_install_git_config_for_root_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(guest_setup, "ssh_command", lambda vm_id: ["ssh", "root@host"])

    def fake_run_capture(argv: list[str], *, env: dict[str, str] | None = None):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.runtime, "run_capture", fake_run_capture)

    guest_setup.install_git_config("vm1", None, "[user]\n", run_capture=fake_run_capture)
    guest_setup.install_git_config("vm1", "agent", "[user]\n", run_capture=fake_run_capture)
    guest_setup.install_git_config("vm1", "agent", None, run_capture=fake_run_capture)

    assert "/root/.gitconfig" in commands[0][-1]
    assert "/home/agent/.gitconfig" in commands[1][-1]
    assert len(commands) == 2


def test_prepare_run_user_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guest_setup, "ssh_command", lambda vm_id: ["ssh", "root@host"])
    captured: dict[str, object] = {}

    def fake_ok(argv: list[str], *, env: dict[str, str] | None = None):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.runtime, "run_capture", fake_ok)
    guest_setup.prepare_run_user("vm1", "agent", run_capture=fake_ok)
    assert "getent hosts" in captured["argv"][-1]
    assert "127.0.1.1" in captured["argv"][-1]
    assert ".ssh .pi .codex .claude .claude.json" in captured["argv"][-1]

    monkeypatch.setattr(cli.runtime, "run_capture", lambda argv, **kwargs: None)
    with pytest.raises(cli.ConfigError, match="ssh command not found"):
        guest_setup.prepare_run_user("vm1", "agent", run_capture=lambda argv: None)

    def fake_fail(argv: list[str], *, env: dict[str, str] | None = None):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad")

    monkeypatch.setattr(cli.runtime, "run_capture", fake_fail)
    with pytest.raises(cli.ConfigError, match="bad"):
        guest_setup.prepare_run_user("vm1", "agent", run_capture=fake_fail)


def test_attach_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(guest_setup, "ssh_command", lambda vm_id: ["ssh", "root@host"])

    def run(argv: list[str]) -> int:
        commands.append(list(argv))
        return 0

    monkeypatch.setattr(cli.runtime, "run", run)

    assert guest_setup.attach("vm1", "pi", cwd="/workspace", run=run) == 0
    assert guest_setup.attach("vm1", "pi", user="agent", cwd="/workspace", run=run) == 0

    assert "-t" in commands[0]
    assert "cd /workspace" in commands[0][-1]
    assert "exec pi" in commands[0][-1]
    assert "sudo -iu agent" in commands[1][-1]


def test_sync_guest_clock_sets_host_time_and_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_run_capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(guest_setup, "ssh_command", lambda vm_id: ["ssh", vm_id])
    monkeypatch.setattr(guest_setup, "host_timezone", lambda: "America/Chicago")

    guest_setup.sync_guest_clock("vm1", run_capture=fake_run_capture)

    assert captured[:2] == ["ssh", "vm1"]
    assert "America/Chicago" in captured[2]
    assert "ln -sf" in captured[2]
    assert "date -u -s" in captured[2]


def test_post_start_actions_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        cli.network, "expose_auth_port", lambda *args: calls.append(("port", args)) or 0
    )
    monkeypatch.setattr(
        session_state, "register_session", lambda *args: calls.append(("register", args))
    )
    monkeypatch.setattr(
        session_state, "unregister_session", lambda *args: calls.append(("unregister", args)) or 0
    )
    monkeypatch.setattr(
        cli, "_stop_vm_if_last_session", lambda *args, **kwargs: calls.append(("stop", kwargs))
    )
    monkeypatch.setattr(
        guest_setup, "sync_guest_clock", lambda *args, **kwargs: calls.append(("clock", args))
    )
    monkeypatch.setattr(
        guest_setup,
        "prepare_run_user",
        lambda *args, **kwargs: calls.append(("prepare", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "install_git_config",
        lambda *args, **kwargs: calls.append(("git", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "attach",
        lambda *args, **kwargs: calls.append(("user" if kwargs.get("user") else "root", args)) or 0,
    )

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            command="pi",
            attach=False,
            run_user=None,
            auth_port=True,
            auth_host_port=1,
            auth_guest_port=2,
            stop_on_exit=True,
        )
        == 0
    )
    assert calls == [("clock", ("vm1",)), ("port", ("vm1", 1, 2))]

    calls.clear()
    assert (
        cli._post_start_actions(
            vm_name="vm1",
            command="pi",
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
        "clock",
        "register",
        "prepare",
        "git",
        "user",
        "unregister",
        "stop",
    ]


def test_tunnel_and_session_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_state, "pid_is_alive", lambda pid: pid == 123)
    monkeypatch.setattr(cli.network, "pid_is_alive", lambda pid: pid == 123)
    cli.network._record_auth_tunnel("vm1", pid=123, host_port=1455, guest_port=1455)
    assert cli.network._tracked_auth_tunnel("vm1") == {
        "pid": 123,
        "host_port": 1455,
        "guest_port": 1455,
    }
    assert cli.network._tracked_auth_tunnel_for_host_port(1455) == (
        "vm1",
        {"pid": 123, "host_port": 1455, "guest_port": 1455},
    )
    assert cli.network._tracked_auth_tunnel_for_host_port(9999) is None
    cli.network._remove_auth_tunnel_record("vm1")
    assert cli.network._tracked_auth_tunnel("vm1") is None
    cli.network._save_tunnels({"bad": [], "dead": {"auth_port": {"pid": 999, "host_port": 1}}})
    assert cli.network._tracked_auth_tunnel_for_host_port(1) is None

    session_state.save_sessions(
        {"vm1": {"sessions": [{"pid": 123, "kind": "run"}, {"pid": 999, "kind": "run"}]}}
    )
    assert session_state.active_sessions("vm1") == [{"pid": 123, "kind": "run"}]

    stopped: list[list[str]] = []
    monkeypatch.setattr(cli.runtime, "run_smolvm", lambda argv: stopped.append(list(argv)) or 0)
    cli._stop_vm_if_last_session("vm1", stop_on_exit=False)
    assert stopped == []
    session_state.save_sessions({})
    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)
    assert stopped == [["sandbox", "stop", "vm1"]]


def test_expose_auth_port_error_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.network,
        "_tracked_auth_tunnel",
        lambda vm_id: {"pid": 123, "host_port": 1, "guest_port": 2},
    )
    assert cli.network.expose_auth_port("vm1", 1, 2) == 0

    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel_for_host_port", lambda port: ("vm2", {}))
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: True)
    assert cli.network.expose_auth_port("vm1", 1, 2) == 0
    assert "VM 'vm2'" in capsys.readouterr().err

    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel_for_host_port", lambda port: None)
    assert cli.network.expose_auth_port("vm1", 1, 2) == 0
    assert "not tracked by sbx" in capsys.readouterr().err

    class ExitedProcess:
        pid = 123
        returncode = 1
        stderr = None

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: False)
    monkeypatch.setattr(cli.network, "ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(cli.network.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    assert cli.network.expose_auth_port("vm1", 1, 2) == 1


def test_foreground_port_forward_uses_one_ssh_for_multiple_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str]) -> int:
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(cli.network, "ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(cli.network, "run", fake_run)

    assert (
        cli.network._foreground_port_forward(
            "vm1", [("127.0.0.1", 3000, 3000), ("127.0.0.1", 8080, 80)]
        )
        == 0
    )
    assert captured["argv"] == [
        "ssh",
        "-N",
        "-L",
        "127.0.0.1:3000:127.0.0.1:3000",
        "-L",
        "127.0.0.1:8080:127.0.0.1:80",
        "-o",
        "ExitOnForwardFailure=yes",
        "root@host",
    ]


def test_delete_vm_error_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.runtime, "run_capture", lambda argv, **kwargs: None)
    assert cli._delete_vm("vm1") == 127

    not_found = json.dumps({"data": {"failed": [{"error": "VM 'vm1' not found"}]}})
    monkeypatch.setattr(
        cli.runtime,
        "run_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout=not_found, stderr="warn\n"
        ),
    )
    assert cli._delete_vm("vm1") == 0
    captured = capsys.readouterr()
    assert "warn" in captured.err
    assert "nothing to destroy" in captured.out

    monkeypatch.setattr(
        cli.runtime,
        "run_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3, stdout="not-json", stderr=""),
    )
    assert cli._delete_vm("vm1") == 3
    assert "not-json" in capsys.readouterr().out


def test_network_status_variants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.network,
        "run_smolvm_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="bad out\n", stderr="bad err\n"
        ),
    )
    assert cli.network.cmd_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 1
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
        cli.network,
        "run_smolvm_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""),
    )
    monkeypatch.setattr(
        cli.network,
        "_tracked_auth_tunnel",
        lambda name: {"pid": 123, "host_port": 1, "guest_port": 2},
    )
    assert cli.network.cmd_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
    assert "active" in capsys.readouterr().out

    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: True)
    assert cli.network.cmd_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
    assert "busy/untracked" in capsys.readouterr().out


def test_sync_forwarded_env_sets_present_and_unsets_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeSmolVM:
        _info = SimpleNamespace(config=SimpleNamespace(comm_channel="ssh"))

        @classmethod
        def from_id(cls, vm_id: str, **kwargs: object) -> Self:
            calls.append(("from_id", kwargs))
            return cls()

        def set_env_vars(self, values: dict[str, str]) -> None:
            calls.append(("set", values))

        def unset_env_vars(self, names: list[str]) -> None:
            calls.append(("unset", names))

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(smolvm.facade, "SmolVM", FakeSmolVM, raising=False)
    monkeypatch.setenv("SBX_PRESENT", "value")
    monkeypatch.delenv("SBX_MISSING", raising=False)

    guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT", "SBX_MISSING"])

    assert calls == [
        ("from_id", {}),
        ("set", {"SBX_PRESENT": "value"}),
        ("unset", ["SBX_MISSING"]),
        ("close", None),
    ]


def test_sync_forwarded_env_uses_direct_ssh_for_legacy_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeSmolVM:
        _info = SimpleNamespace(config=SimpleNamespace(comm_channel=None))

        @classmethod
        def from_id(cls, vm_id: str, **kwargs: object) -> Self:
            calls.append(("from_id", kwargs))
            return cls()

        def close(self) -> None:
            calls.append(("close", None))

    def fake_run_capture(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(("ssh", argv[-1]))
        return subprocess.CompletedProcess(argv, 0, "export OLD=kept\nexport SBX_TOKEN=old\n", "")

    monkeypatch.setattr(smolvm.facade, "SmolVM", FakeSmolVM, raising=False)
    monkeypatch.setattr(guest_setup, "ssh_command", lambda vm_id: ["ssh", vm_id])
    monkeypatch.setattr(cli.runtime, "run_capture", fake_run_capture)
    monkeypatch.setenv("SBX_TOKEN", "value")

    guest_setup.sync_forwarded_env("vm1", ["SBX_TOKEN", "MISSING"], run_capture=fake_run_capture)

    assert calls[0] == ("from_id", {})
    assert calls[1] == ("ssh", "cat /etc/profile.d/smolvm_env.sh 2>/dev/null || true")
    assert calls[2][0] == "ssh"
    assert "base64 -d" in str(calls[2][1])
    assert calls[3] == ("close", None)


def test_sync_forwarded_env_empty_allowlist_skips_smolvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSmolVM:
        @classmethod
        def from_id(cls, vm_id: str) -> Self:
            raise AssertionError("SmolVM should not be opened")

    monkeypatch.setattr(smolvm.facade, "SmolVM", FakeSmolVM, raising=False)

    guest_setup.sync_forwarded_env("vm1", [])


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
        guest_setup.validate_run_user("bad user")
    with pytest.raises(cli.ConfigError, match="invalid env var"):
        guest_setup.validate_env_names(["BAD-NAME"])


def test_command_edge_cases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.runtime, "run_smolvm", lambda *args, **kwargs: 7)
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
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda vm_id: None)
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: False)
    monkeypatch.setattr(cli.network, "ssh_command", lambda vm_id: ["ssh", "root@host"])
    monkeypatch.setattr(
        cli.network.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert cli.network.expose_auth_port("vm1", 1, 2) == 127
    assert "command not found: ssh" in capsys.readouterr().err

    monkeypatch.setattr(
        cli.network.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("nope")),
    )
    assert cli.network.expose_auth_port("vm1", 1, 2) == 1
    assert "failed to start auth port tunnel" in capsys.readouterr().err

    class RunningProcess:
        pid = 123
        stderr = None

        def poll(self) -> None:
            return None

    times = iter([0.0, 6.0])
    monkeypatch.setattr(cli.network.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.network.time, "sleep", lambda seconds: None)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.network.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(cli.network.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    assert cli.network.expose_auth_port("vm1", 1, 2) == 1
    assert killed == [(123, cli.network.signal.SIGTERM)]
    assert "did not become ready" in capsys.readouterr().err


def test_close_auth_port_kills_tracked_tunnel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda name: {"pid": 123})
    removed: list[str] = []
    monkeypatch.setattr(
        cli.network, "_remove_auth_tunnel_record", lambda name: removed.append(name)
    )
    states = iter([True, False, False])
    monkeypatch.setattr(cli.network, "pid_is_alive", lambda pid: next(states))
    monkeypatch.setattr(cli.network.time, "monotonic", lambda: 0)
    monkeypatch.setattr(cli.network.time, "sleep", lambda seconds: None)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.network.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert cli.network.cmd_close_auth_port(type("Args", (), {"name": "vm1"})()) == 0
    assert killed == [(123, cli.network.signal.SIGTERM)]
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
        cli.network,
        "run_smolvm_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""),
    )
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: False)

    assert cli.network.cmd_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 0
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
    monkeypatch.setattr(
        guest_setup, "sync_guest_clock", lambda vm_id, **kwargs: calls.append("clock")
    )
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda *args: calls.append("port") or 9)
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: calls.append("attach") or 0)

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            command="pi",
            attach=True,
            run_user=None,
            auth_port=True,
            auth_host_port=1455,
            auth_guest_port=1455,
            stop_on_exit=True,
        )
        == 9
    )
    assert calls == ["clock", "port"]


def test_close_auth_port_escalates_to_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda name: {"pid": 123})
    monkeypatch.setattr(cli.network, "_remove_auth_tunnel_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.network.time, "sleep", lambda seconds: None)
    times = iter([0.0, 4.0])
    monkeypatch.setattr(cli.network.time, "monotonic", lambda: next(times))
    alive = iter([True, True])
    monkeypatch.setattr(cli.network, "pid_is_alive", lambda pid: next(alive))
    killed: list[int] = []
    monkeypatch.setattr(cli.network.os, "killpg", lambda pid, sig: killed.append(sig))

    assert cli.network.cmd_close_auth_port(type("Args", (), {"name": "vm1"})()) == 0
    assert killed == [cli.network.signal.SIGTERM, cli.network.signal.SIGKILL]


def test_stop_vm_skips_when_other_sessions_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_state, "active_sessions", lambda vm_id: [{"pid": 123, "kind": "run"}]
    )
    stopped: list[list[str]] = []
    monkeypatch.setattr(cli.runtime, "run", lambda argv: stopped.append(list(argv)) or 0)

    cli._stop_vm_if_last_session("vm1", stop_on_exit=True)

    assert stopped == []


def test_delete_vm_success_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.runtime,
        "run_capture",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout='{"data": {}}', stderr=""
        ),
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
    assert captured["command"] == "pi"
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
    monkeypatch.setattr(cli.network, "run_smolvm_capture", lambda argv, **kwargs: None)

    assert cli.network.cmd_status(type("Args", (), {"name": "vm1", "host_port": 1455})()) == 127


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
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm_capture",
        lambda argv: subprocess.CompletedProcess(calls.append(list(argv)) or argv, 0, "", ""),
    )

    assert cli._start_existing_vm_if_needed("vm1", "running", 60) == 0
    assert calls == []
    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 0
    assert calls == [["sandbox", "start", "vm1", "--boot-timeout", "60"]]
    assert cli._start_existing_vm_if_needed("vm1", "error", 60) == 1
    assert calls == [["sandbox", "start", "vm1", "--boot-timeout", "60"]]


def test_mark_error_vm_stopped_for_restart_clears_stale_runtime_fields() -> None:
    with sqlite3.connect(cli.SMOLVM_DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE vms (id TEXT PRIMARY KEY, status TEXT, pid INTEGER, socket_path TEXT)"
        )
        conn.execute(
            "INSERT INTO vms (id, status, pid, socket_path) VALUES (?, ?, ?, ?)",
            ("vm1", "error", 123, "/tmp/stale.sock"),
        )

    vm_state.mark_error_vm_stopped_for_restart("vm1")

    with sqlite3.connect(cli.SMOLVM_DB_PATH) as conn:
        row = conn.execute(
            "SELECT status, pid, socket_path FROM vms WHERE id = ?", ("vm1",)
        ).fetchone()
    assert row == ("stopped", None, None)


def test_start_existing_vm_timeout_hint_when_vm_is_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.runtime,
        "run_smolvm_capture",
        lambda argv: subprocess.CompletedProcess(argv, 1, "", ""),
    )
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 1

    err = capsys.readouterr().err
    assert "VM 'vm1' started, but SSH was not ready within 60s" in err
    assert "sbx run vm1 --boot-timeout" in err


def test_auth_tunnel_success_records_when_port_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.network, "_tracked_auth_tunnel", lambda vm_id: None)
    readiness = iter([False, True])
    monkeypatch.setattr(cli.network, "_localhost_port_is_listening", lambda port: next(readiness))
    monkeypatch.setattr(cli.network, "ssh_command", lambda vm_id: ["ssh", "root@host"])
    recorded: list[tuple[str, int, int, int]] = []
    monkeypatch.setattr(
        cli.network,
        "_record_auth_tunnel",
        lambda vm_id, *, pid, host_port, guest_port: recorded.append(
            (vm_id, pid, host_port, guest_port)
        ),
    )

    class RunningProcess:
        pid = 321

        def poll(self) -> None:
            return None

    monkeypatch.setattr(cli.network.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())

    assert cli.network.expose_auth_port("vm1", 1455, 1455) == 0
    assert recorded == [("vm1", 321, 1455, 1455)]


def test_pid_is_alive_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def permission_error(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(session_state.os, "kill", permission_error)
    assert session_state.pid_is_alive(123) is True


def test_unregister_session_keeps_other_active_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_state.os, "getpid", lambda: 111)
    monkeypatch.setattr(session_state, "pid_is_alive", lambda pid: pid == 222)
    session_state.save_sessions(
        {"vm1": {"sessions": [{"pid": 111, "kind": "run"}, {"pid": 222, "kind": "shell"}]}}
    )

    session_state.unregister_session("vm1")

    assert session_state.load_sessions() == {"vm1": {"sessions": [{"pid": 222, "kind": "shell"}]}}


def test_network_auth_port_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli.network,
        "expose_auth_port",
        lambda name, host, guest, *, replace=False: (name, host, guest, replace),
    )
    args = type("Args", (), {"name": "vm1", "host_port": 1, "guest_port": 2, "replace": True})()
    assert cli.network.cmd_auth_port(args) == ("vm1", 1, 2, True)
