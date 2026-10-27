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
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_guest_lifecycle) -> None:
    monkeypatch.setattr(cli.runtime, "DEBUG", False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (tmp_path / "home-config.toml",))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (tmp_path / ".sbx.toml",))
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "state" / "vms")
    monkeypatch.setattr(session_state, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(session_state, "SESSIONS_FILE", tmp_path / "state" / "sessions.json")
    monkeypatch.setattr(cli.network, "SBX_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli.network, "TUNNELS_FILE", tmp_path / "state" / "tunnels.json")
    monkeypatch.setattr(cli, "SMOLVM_DB_PATH", tmp_path / "smolvm.db")
    monkeypatch.setattr(vm_state, "SMOLVM_DB_PATH", tmp_path / "smolvm.db")
    monkeypatch.setattr(guest_setup, "set_hostname", lambda *args, **kwargs: None)

    def create_preset(preset_name: str, **kwargs: object) -> SimpleNamespace:
        vm_id = str(kwargs.get("vm_name") or f"{preset_name}-sbx")
        callback = kwargs.get("before_provision")
        if callable(callback):
            callback(vm_id)
        guest_setup.prepare_boot(
            vm_id,
            str(kwargs.get("run_user", "root")),
            list(kwargs.get("forward_env", [])),
            guest=fake_guest_lifecycle,
        )
        return SimpleNamespace(vm_id=vm_id, close=lambda: None)

    monkeypatch.setattr(cli.smolvm_preset, "create_preset", create_preset)


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
                "sbx": {},
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

    def start_vm(vm_id, **kwargs):
        captured["started"] = True
        captured["start_kwargs"] = {"boot_timeout": kwargs["boot_timeout"]}

    monkeypatch.setattr(guest_setup, "start_vm", start_vm)
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
    vm_metadata.claim_vm("vm1", identity, "agent", created=True)

    assert vm_metadata.load_vm_metadata() == {
        "vm1": {**identity, "run_user": "agent", "provisioned": False}
    }
    assert identity["project_root"] == str(project.resolve())
    assert identity["config_path"] == str(config.resolve())


def test_vm_metadata_corrupt_json_raises() -> None:
    path = vm_metadata.metadata_path("vm1")
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(cli.ConfigError, match="invalid sbx VM metadata"):
        vm_metadata.load_vm_metadata()


def test_sync_existing_vm_mounts_rejects_wrong_project(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_vm_row(cli.SMOLVM_DB_PATH, "vm1", status="stopped", config={"workspace_mounts": []})
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": str(old), "config_path": str(old / ".sbx.toml")},
        "agent",
        created=True,
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
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": str(missing), "config_path": str(missing / ".sbx.toml")},
        "agent",
        created=True,
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
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": str(tmp_path), "config_path": str(config)},
        guest_setup.host_username(),
        created=True,
    )
    vm_metadata.mark_provisioned("vm1")

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
    assert calls == ["start", "attach"]


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
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": str(tmp_path), "config_path": str(config)},
        guest_setup.host_username(),
        created=True,
    )
    vm_metadata.mark_provisioned("vm1")

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
    assert calls == ["attach"]
    assert fake_smolvm_sdk["started"] is True
    assert "waited" not in fake_smolvm_sdk  # Readiness now belongs to the bootstrap session.
    assert fake_smolvm_sdk["start_kwargs"] == {"boot_timeout": 60.0}
    assert "wait_kwargs" not in fake_smolvm_sdk
    vm_config = fake_smolvm_sdk["vm_config"]
    assert isinstance(vm_config, dict)
    assert vm_config["vm_id"] == "vm-from-cli"
    assert vm_config["boot_args"] == "boot args"
    assert vm_config["comm_channel"] == "ssh"
    assert vm_config["ssh_public_key"] == "ssh-ed25519 public"
    assert vm_config["port_forwards"] == [
        {"host_address": "127.0.0.1", "host_port": 8080, "guest_port": 3000}
    ]
    assert captured["command"] == "pi"
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


def test_host_username_uses_account_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guest_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(guest_setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(
        guest_setup.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="developer")
    )
    assert guest_setup.host_username() == "developer"

    monkeypatch.setattr(guest_setup.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "nueces")
    monkeypatch.setattr(guest_setup.pwd, "getpwnam", lambda user: SimpleNamespace(pw_name=user))
    assert guest_setup.host_username() == "nueces"

    monkeypatch.setattr(
        guest_setup.pwd, "getpwnam", lambda user: (_ for _ in ()).throw(KeyError(user))
    )
    monkeypatch.setattr(guest_setup.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="root"))
    assert guest_setup.host_username() == "root"


@pytest.mark.parametrize("sudo_user", ["root", "missing", "bad user"])
def test_main_hides_stale_sudo_env_from_non_root_commands(
    monkeypatch: pytest.MonkeyPatch, sudo_user: str
) -> None:
    observed: dict[str, str] = {}
    sudo_env = {
        "SUDO_COMMAND": "/bin/bash",
        "SUDO_GID": "0",
        "SUDO_HOME": "/root",
        "SUDO_UID": "0",
        "SUDO_USER": sudo_user,
    }
    for key, value in sudo_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 1000)

    class Parser:
        def parse_args(self, argv: list[str]) -> SimpleNamespace:
            return SimpleNamespace(
                action="completion",
                config=None,
                debug=False,
                func=lambda args: (
                    observed.update(
                        {
                            key: runtime.os.environ[key]
                            for key in runtime.SUDO_ENV_KEYS
                            if key in runtime.os.environ
                        }
                    )
                    or 0
                ),
            )

    monkeypatch.setattr(cli, "build_parser", Parser)

    assert cli.main([]) == 0
    assert observed == {}
    assert {key: runtime.os.environ[key] for key in runtime.SUDO_ENV_KEYS} == sudo_env


def test_main_preserves_sudo_env_when_effectively_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "developer")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    observed: list[str | None] = []

    class Parser:
        def parse_args(self, argv: list[str]) -> SimpleNamespace:
            return SimpleNamespace(
                action="completion",
                config=None,
                debug=False,
                func=lambda args: observed.append(runtime.os.getenv("SUDO_USER")) or 0,
            )

    monkeypatch.setattr(cli, "build_parser", Parser)

    assert cli.main([]) == 0
    assert observed == ["developer"]


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
    vm_metadata.claim_vm(
        "the-quest",
        {"project_root": str(tmp_path), "config_path": str(config)},
        "agent",
        created=True,
    )
    vm_metadata.mark_provisioned("the-quest")
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
    monkeypatch.setattr(
        guest_setup, "start_vm", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
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


def test_manifest_run_user() -> None:
    assert cli._manifest_run_user({}) is None
    assert cli._manifest_run_user({"sbx": {"run_user": "agent"}}) == "agent"
    with pytest.raises(cli.ConfigError, match="run_user.*string"):
        cli._manifest_run_user({"sbx": {"run_user": 1000}})


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


def test_obsolete_manifest_fields_warn_and_are_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = {"sbx": {"agent": "pi", "launch_command": "pi", "run_user": "agent"}}

    cli._warn_obsolete_manifest_fields(manifest)

    assert cli._manifest_run_user(manifest) == "agent"
    assert capsys.readouterr().err == (
        "sbx: warning: obsolete image manifest field(s) "
        "sbx.agent, sbx.launch_command are ignored; "
        "remove them from the manifest or rebuild the image\n"
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


def test_install_git_config_for_root_and_user(fake_guest_lifecycle) -> None:
    guest_setup.install_git_config("vm1", None, "[user]\n")
    guest_setup.install_git_config("vm1", "agent", "[user]\n")
    guest_setup.install_git_config("vm1", "agent", None)
    assert [args for args, _ in fake_guest_lifecycle.opened] == [("vm1", "root"), ("vm1", "agent")]
    assert [(name, data) for name, data, _ in fake_guest_lifecycle.phases] == [
        ("git", {"text": "[user]\n"})
    ] * 2


def test_prepare_run_user_success_and_failure(fake_guest_lifecycle) -> None:
    guest_setup.prepare_run_user("vm1", "agent", copy_initial_state=True)
    guest_setup.prepare_run_user("vm1", "root")
    assert [name for name, _, _ in fake_guest_lifecycle.phases] == [
        "account",
        "credentials",
        "account",
    ]
    assert fake_guest_lifecycle.closed == 3
    fake_guest_lifecycle.phase = lambda *a, **kw: (_ for _ in ()).throw(cli.ConfigError("bad"))
    with pytest.raises(cli.ConfigError, match="bad"):
        guest_setup.prepare_run_user("vm1", "agent")
    assert fake_guest_lifecycle.closed == 4


def test_ensure_kvm_access_runs_optional_helper_each_time(fake_guest_lifecycle) -> None:
    guest_setup.ensure_kvm_access("vm1", "agent")
    guest_setup.ensure_kvm_access("vm1", "agent")
    guest_setup.ensure_kvm_access("vm1", "root")
    assert [name for name, _, _ in fake_guest_lifecycle.phases] == ["kvm", "kvm"]


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
    assert "$HOME/.nodejs/bin" not in commands[0][-1]
    assert "sudo -iu agent" in commands[1][-1]


def test_sync_guest_clock_sets_host_time_and_timezone(monkeypatch, fake_guest_lifecycle) -> None:
    monkeypatch.setattr(guest_setup, "host_timezone", lambda: "America/Chicago")
    guest_setup.sync_guest_clock("vm1")
    name, arguments, _ = fake_guest_lifecycle.phases[0]
    assert name == "clock"
    assert arguments["timezone"] == "America/Chicago"
    assert len(arguments["timestamp"]) == 19


def test_post_start_actions_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    project = {"project_root": "/project", "config_path": "/project/.sbx.toml"}
    vm_metadata.claim_vm("vm1", project, "agent", created=True)
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
        "ensure_kvm_access",
        lambda *args, **kwargs: calls.append(("kvm", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "install_runtime_tools",
        lambda *args, **kwargs: calls.append(("install", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "install_git_config",
        lambda *args, **kwargs: calls.append(("git", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "ensure_rootless_docker",
        lambda *args, **kwargs: calls.append(("docker", args)),
    )
    monkeypatch.setattr(
        guest_setup,
        "command_path",
        lambda *args, **kwargs: calls.append(("command", args)) or "/home/agent/.local/bin/pi",
    )
    monkeypatch.setattr(
        guest_setup,
        "attach",
        lambda *args, **kwargs: calls.append(("user", args)) or 0,
    )

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            started_now=True,
            command="pi",
            attach=True,
            run_user="agent",
            auth_port=True,
            auth_host_port=1,
            auth_guest_port=2,
            stop_on_exit=True,
            git_config_text="git",
            provision=True,
            runtime_install=["pi"],
            copy_initial_state=True,
            agent_name="pi",
        )
        == 0
    )
    assert [name for name, _ in calls] == [
        "prepare",
        "clock",
        "port",
        "kvm",
        "install",
        "git",
        "docker",
        "command",
        "register",
        "user",
        "unregister",
        "stop",
    ]
    assert vm_metadata.load_vm("vm1")["provisioned"] is True

    calls.clear()
    assert (
        cli._post_start_actions(
            vm_name="vm1",
            started_now=False,
            command="bash",
            attach=False,
            run_user="agent",
            auth_port=False,
            stop_on_exit=True,
            validate_agent=False,
        )
        == 0
    )
    assert calls == []


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


@pytest.fixture
def forwarded_env_vm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    key_dir = tmp_path / ".smolvm" / "keys"
    key_dir.mkdir(parents=True)
    private = key_dir / "id_ed25519"
    public = key_dir / "id_ed25519.pub"
    private.write_text("PRIVATE_KEY_SENTINEL", encoding="utf-8")
    public.write_text("ssh-ed25519 injected-key comment\n", encoding="utf-8")
    calls: list[tuple[str, object]] = []
    config = SimpleNamespace(comm_channel="ssh", ssh_public_key="ssh-ed25519 injected-key")
    state = SimpleNamespace(
        private=private, public=public, config=config, calls=calls, failure=None, require_key=True
    )

    class FakeSmolVM:
        info = SimpleNamespace(config=config)

        def __init__(self, key: object) -> None:
            self.key = key

        @classmethod
        def from_id(cls, vm_id: str, **kwargs: object) -> Self:
            assert vm_id == "vm1"
            calls.append(("from_id", kwargs))
            if kwargs and state.failure == "connect":
                raise RuntimeError("connection failed")
            return cls(kwargs.get("ssh_key_path"))

        def check_key(self) -> None:
            if (
                state.require_key
                and config.comm_channel == "ssh"
                and config.ssh_public_key == "ssh-ed25519 injected-key"
            ):
                assert self.key == str(private), "known key must precede environment operations"

        def run(self, command, **kwargs):
            self.check_key()
            return SimpleNamespace(exit_code=75 if state.failure == "busy" else 0)

        def set_env_vars(self, values: dict[str, str]) -> None:
            self.check_key()
            calls.append(("set", values))
            if state.failure == "set":
                raise RuntimeError("set failed")

        def unset_env_vars(self, names: list[str]) -> None:
            self.check_key()
            calls.append(("unset", names))
            if state.failure == "unset":
                raise RuntimeError("unset failed")

        def close(self) -> None:
            calls.append(("close", self.key))

    def resolve_key() -> tuple[Path, Path]:
        calls.append(("key", None))
        if state.failure == "key":
            raise OSError("key resolution failed")
        return private, public

    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(smolvm.facade, "SmolVM", FakeSmolVM)
    monkeypatch.setattr(smolvm.utils, "ensure_ssh_key", resolve_key)
    monkeypatch.setenv("SBX_PRESENT", "ENV_SECRET_SENTINEL")
    monkeypatch.delenv("SBX_MISSING", raising=False)
    return state


@pytest.mark.parametrize(
    "names",
    [["SBX_PRESENT"], ["SBX_MISSING"], ["SBX_PRESENT", "SBX_MISSING"]],
)
def test_sync_forwarded_env_sets_present_and_unsets_missing(
    forwarded_env_vm: SimpleNamespace, names: list[str]
) -> None:
    guest_setup.sync_forwarded_env("vm1", names)

    expected: list[tuple[str, object]] = [
        ("from_id", {}),
        ("key", None),
        ("from_id", {"ssh_key_path": str(forwarded_env_vm.private)}),
    ]
    if "SBX_PRESENT" in names:
        expected.append(("set", {"SBX_PRESENT": "ENV_SECRET_SENTINEL"}))
    if "SBX_MISSING" in names:
        expected.append(("unset", ["SBX_MISSING"]))
    expected.extend([("close", str(forwarded_env_vm.private)), ("close", None)])
    assert forwarded_env_vm.calls == expected


@pytest.mark.parametrize("failure", ["key", "connect", "set", "unset"])
def test_sync_forwarded_env_closes_on_failure(
    forwarded_env_vm: SimpleNamespace, failure: str
) -> None:
    forwarded_env_vm.failure = failure
    with pytest.raises((OSError, RuntimeError), match="failed"):
        guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT", "SBX_MISSING"])

    assert forwarded_env_vm.calls[-1] == ("close", None)
    closed_keys = [value for action, value in forwarded_env_vm.calls if action == "close"]
    assert len(closed_keys) == (2 if failure in {"set", "unset"} else 1)


def test_sync_forwarded_env_does_not_generate_missing_known_key(
    forwarded_env_vm: SimpleNamespace,
) -> None:
    forwarded_env_vm.private.unlink()
    with pytest.raises(cli.ConfigError, match="SSH key"):
        guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT"])

    assert forwarded_env_vm.calls == [("from_id", {}), ("close", None)]
    assert not forwarded_env_vm.private.exists()


def test_sync_forwarded_env_keeps_existing_auth_without_public_key_proof(
    forwarded_env_vm: SimpleNamespace,
) -> None:
    forwarded_env_vm.public.unlink()
    forwarded_env_vm.require_key = False
    guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT"])

    assert forwarded_env_vm.calls == [
        ("from_id", {}),
        ("set", {"SBX_PRESENT": "ENV_SECRET_SENTINEL"}),
        ("close", None),
    ]
    assert not forwarded_env_vm.public.exists()


def test_sync_forwarded_env_resolves_sudo_identity(
    forwarded_env_vm: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    looked_up: list[str] = []

    def user_info(user: str) -> SimpleNamespace:
        looked_up.append(user)
        return SimpleNamespace(pw_dir=str(tmp_path))

    monkeypatch.setenv("SUDO_USER", "developer")
    monkeypatch.setattr(guest_setup.pwd, "getpwnam", user_info)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "root-home")
    guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT"])

    assert looked_up == ["developer"]
    assert ("from_id", {"ssh_key_path": str(forwarded_env_vm.private)}) in forwarded_env_vm.calls


@pytest.mark.parametrize("failure", ["key", "connect", "set", "unset"])
def test_sync_forwarded_env_failure_prevents_cli_attach(
    forwarded_env_vm: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    forwarded_env_vm.failure = failure
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": "/project", "config_path": "/project/sbx.toml"},
        "root",
        created=True,
    )

    def reject_attach(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not attach after environment failure")

    monkeypatch.setattr(guest_setup, "attach", reject_attach)
    assert (
        cli._post_start_actions(
            vm_name="vm1",
            started_now=False,
            command="bash",
            attach=True,
            run_user="root",
            auth_port=False,
            stop_on_exit=False,
            forward_env=["SBX_PRESENT", "SBX_MISSING"],
            validate_agent=False,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "failed" in captured.err
    assert "ENV_SECRET_SENTINEL" not in captured.out + captured.err
    assert "PRIVATE_KEY_SENTINEL" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("channel", "injected_key"),
    [
        ("vsock", "ssh-ed25519 injected-key"),
        ("ssh", "ssh-ed25519 different-authorized-key"),
        ("ssh", None),
    ],
)
def test_sync_forwarded_env_preserves_other_auth_and_transports(
    forwarded_env_vm: SimpleNamespace, channel: str, injected_key: str | None
) -> None:
    forwarded_env_vm.config.comm_channel = channel
    forwarded_env_vm.config.ssh_public_key = injected_key
    guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT", "SBX_MISSING"])

    assert forwarded_env_vm.calls == [
        ("from_id", {}),
        ("set", {"SBX_PRESENT": "ENV_SECRET_SENTINEL"}),
        ("unset", ["SBX_MISSING"]),
        ("close", None),
    ]


def test_sync_forwarded_env_does_not_log_secrets(
    forwarded_env_vm: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(runtime, "DEBUG", True)
    guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT"])
    captured = capsys.readouterr()
    for secret in ("ENV_SECRET_SENTINEL", "PRIVATE_KEY_SENTINEL"):
        assert secret not in captured.out + captured.err + caplog.text


def test_sync_forwarded_env_uses_direct_ssh_for_legacy_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeSmolVM:
        info = SimpleNamespace(config=SimpleNamespace(comm_channel=None))

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
    assert calls[1][0] == "ssh"
    assert "cat /etc/profile.d/smolvm_env.sh 2>/dev/null || true" in calls[1][1]
    assert "execution.lock" in calls[1][1]
    assert calls[2][0] == "ssh"
    assert "base64 -d" in str(calls[2][1])
    assert calls[3] == ("close", None)


def test_sync_forwarded_env_empty_allowlist_skips_smolvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_key_resolution() -> None:
        raise AssertionError("SSH key should not be resolved")

    monkeypatch.setattr(smolvm.utils, "ensure_ssh_key", reject_key_resolution)

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
    assert capsys.readouterr().out == ""  # No success announcement before required setup.
    assert captured["created_config"] is False


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
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": "/project", "config_path": "/project/.sbx.toml"},
        "agent",
        created=True,
    )
    monkeypatch.setattr(
        guest_setup, "sync_guest_clock", lambda vm_id, **kwargs: calls.append("clock")
    )
    monkeypatch.setattr(cli.network, "expose_auth_port", lambda *args: calls.append("port") or 9)
    monkeypatch.setattr(guest_setup, "attach", lambda *args, **kwargs: calls.append("attach") or 0)

    assert (
        cli._post_start_actions(
            vm_name="vm1",
            started_now=True,
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
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {}}
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
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {}}
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
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {}}
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
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.ext4", "sbx": {}}
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
    manifest = {"kernel": "vmlinux.bin", "rootfs": "rootfs.qcow2", "sbx": {}}
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
    monkeypatch.setattr(
        cli, "cmd_start", lambda args, **kwargs: calls.append(("start", args.name)) or 0
    )
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
    calls = []
    monkeypatch.setattr(
        guest_setup, "start_vm", lambda name, **kwargs: calls.append((name, kwargs))
    )
    assert cli._start_existing_vm_if_needed("vm1", "running", 60) == 0
    assert calls == []
    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 0
    assert calls == [("vm1", {"boot_timeout": 60, "status": "stopped"})]
    assert cli._start_existing_vm_if_needed("vm1", "error", 60) == 1
    assert len(calls) == 1


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
        guest_setup,
        "start_vm",
        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("startup timed out")),
    )
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")

    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 1

    err = capsys.readouterr().err
    assert "VM 'vm1' started, but SSH was not ready within 60s" in err
    assert "sbx run vm1 --boot-timeout" in err


def test_non_timeout_start_failure_is_not_reported_as_ssh_timeout(monkeypatch, capsys):
    monkeypatch.setattr(
        guest_setup,
        "start_vm",
        lambda *a, **kw: (_ for _ in ()).throw(cli.ConfigError("invalid guest setup")),
    )
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "running")
    assert cli._start_existing_vm_if_needed("vm1", "stopped", 60) == 1
    error = capsys.readouterr().err
    assert "invalid guest setup" in error
    assert "SSH was not ready" not in error


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


def test_vm_metadata_claim_is_per_vm_atomic_and_immutable(tmp_path: Path) -> None:
    project = {"project_root": str(tmp_path), "config_path": str(tmp_path / ".sbx.toml")}

    with pytest.raises(cli.ConfigError, match="no trusted sbx runtime identity"):
        vm_metadata.claim_vm("vm1", project, "agent", created=False)

    claimed = vm_metadata.claim_vm("vm1", project, "agent", created=True)

    assert claimed["run_user"] == "agent"
    assert claimed["provisioned"] is False
    assert vm_metadata.metadata_path("vm1").stat().st_mode & 0o777 == 0o600
    assert vm_metadata.SBX_VMS_DIR.stat().st_mode & 0o777 == 0o700
    with pytest.raises(cli.ConfigError, match="recreate"):
        vm_metadata.claim_vm("vm1", project, "other", created=False)

    vm_metadata.mark_provisioned("vm1")
    vm_metadata.claim_vm("vm1", project, "agent", created=False)
    assert vm_metadata.load_vm("vm1")["provisioned"] is True


def test_root_shell_requires_host_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "running")

    assert cli.main(["shell", "vm1", "--root"]) == 2
    assert "no trusted sbx runtime identity" in capsys.readouterr().err


def test_runtime_install_is_strict_and_cli_replaces_config() -> None:
    configured = {"sbx": {"runtime_install": ["pi", "specify"]}}
    assert cli._runtime_install(SimpleNamespace(runtime_install=None), configured) == [
        "pi",
        "specify",
    ]
    assert cli._runtime_install(SimpleNamespace(runtime_install=[]), configured) == []
    assert cli._runtime_install(SimpleNamespace(runtime_install=["specify"]), configured) == [
        "specify"
    ]
    assert cli._cfg_agent({"sbx": {"agent": "codex", "runtime_install": ["specify"]}}) == "codex"

    for value in ("pi", ["pi", "pi"], ["specify", "specify"], ["unknown"]):
        with pytest.raises(cli.ConfigError):
            cli._runtime_install(
                SimpleNamespace(runtime_install=None), {"sbx": {"runtime_install": value}}
            )
    with pytest.raises(cli.ConfigError, match="positive integer"):
        cli._install_timeout(0)


def test_empty_runtime_install_does_nothing(fake_guest_lifecycle) -> None:
    guest_setup.install_runtime_tools("vm1", "agent", [], 600)
    assert fake_guest_lifecycle.opened == []


@pytest.mark.parametrize("user", ["agent", "root"])
@pytest.mark.parametrize("tool", ["pi", "specify"])
def test_runtime_install_passes_user_tool_and_deadline(fake_guest_lifecycle, user, tool) -> None:
    guest_setup.install_runtime_tools("vm1", user, [tool], 321)
    assert fake_guest_lifecycle.opened[0][0] == ("vm1", user)
    assert fake_guest_lifecycle.phases == [
        ("tools", {"tools": [tool], "install_timeout": 321}, {"timeout": 366})
    ]


@pytest.mark.parametrize("tool", ["pi", "specify"])
def test_cli_runtime_install_implies_existing_vm_reprovision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: str
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nname = "vm1"\n', encoding="utf-8")
    project = {"project_root": str(tmp_path), "config_path": str(config)}
    user = guest_setup.host_username()
    vm_metadata.claim_vm("vm1", project, user, created=True)
    vm_metadata.mark_provisioned("vm1")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda vm_id: "running")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda project_root=None: None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kwargs: captured.update(kwargs) or 0)

    assert (
        cli.main(
            [
                "--config",
                str(config),
                "run",
                "--no-attach",
                "--runtime-install",
                tool,
            ]
        )
        == 0
    )
    assert captured["provision"] is True
    assert captured["was_provisioned"] is True
    assert captured["runtime_install"] == [tool]


def test_failed_reprovision_preserves_success_and_prints_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = {"project_root": str(tmp_path), "config_path": str(tmp_path / ".sbx.toml")}
    vm_metadata.claim_vm("vm1", project, "agent", created=True)
    vm_metadata.mark_provisioned("vm1")
    monkeypatch.setattr(guest_setup, "sync_guest_clock", lambda *args, **kwargs: None)
    monkeypatch.setattr(guest_setup, "ensure_kvm_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        guest_setup,
        "prepare_run_user",
        lambda *args, **kwargs: (_ for _ in ()).throw(cli.ConfigError("boom")),
    )

    rc = cli._post_start_actions(
        vm_name="vm1",
        started_now=False,
        command="pi",
        attach=False,
        run_user="agent",
        auth_port=False,
        stop_on_exit=True,
        provision=True,
        was_provisioned=True,
    )

    assert rc == 1
    assert vm_metadata.load_vm("vm1")["provisioned"] is True
    error = capsys.readouterr().err
    assert "previous successful provisioning state remains recorded" in error
    assert "sbx shell vm1 --root" in error
    assert "sbx run vm1 --reprovision --no-attach" in error
    assert "sbx stop vm1" in error


@pytest.fixture
def boot_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    config = tmp_path / ".sbx.toml"
    config.write_text(
        '[sbx]\nname = "vm1"\nrun_user = "agent"\nauth_port = false\n'
        'stop_on_exit = false\nruntime_install = ["pi"]\n',
        encoding="utf-8",
    )
    project = {"project_root": str(tmp_path), "config_path": str(config)}
    vm_metadata.claim_vm("vm1", project, "agent", created=True)
    vm_metadata.mark_provisioned("vm1")
    flow = SimpleNamespace(
        config=config,
        events=[],
        status="stopped",
        fail=None,
        start_rc=0,
        attach_rc=0,
        hostname="smolvm",
        kvm=False,
        docker=False,
        account_exists=True,
    )

    def record(name: str):
        def action(*args, **kwargs):
            flow.events.append((name, args, kwargs))
            if flow.fail == name:
                raise cli.ConfigError(f"{name} failed")
            if name in ("kvm", "docker") and not flow.account_exists:
                raise cli.ConfigError("runtime user does not exist: agent")
            if name == "prepare":
                flow.account_exists = True
            elif name == "hostname":
                flow.hostname = args[0]
            elif name == "kvm":
                flow.kvm = True
            elif name == "docker":
                flow.docker = True
            elif name == "command":
                return "/usr/bin/pi"
            elif name == "attach":
                return flow.attach_rc

        return action

    for name, attribute in (
        ("hostname", "set_hostname"),
        ("clock", "sync_guest_clock"),
        ("prepare", "prepare_run_user"),
        ("mounts", "mount_workspaces"),
        ("kvm", "ensure_kvm_access"),
        ("tools", "install_runtime_tools"),
        ("git", "install_git_config"),
        ("docker", "ensure_rootless_docker"),
        ("env", "sync_forwarded_env"),
        ("command", "command_path"),
        ("attach", "attach"),
    ):
        monkeypatch.setattr(guest_setup, attribute, record(name))
    monkeypatch.setattr(guest_setup, "host_git_config", lambda *args: "safe config")
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: flow.status)
    monkeypatch.setattr(cli, "_sync_existing_vm_start_config", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_warn_running_mount_drift", lambda *a, **kw: None)

    def start(vm_id, **kwargs):
        flow.events.append(("start", (vm_id,), kwargs))
        if flow.start_rc:
            raise cli.ConfigError("startup failed")
        flow.status = "running"

    monkeypatch.setattr(guest_setup, "start_vm", start)
    monkeypatch.setattr(cli, "_maybe_print_boot_timeout_running_hint", lambda *args: False)
    real_mark = vm_metadata.mark_provisioned

    def mark(name):
        flow.events.append(("mark", (name,), {}))
        real_mark(name)

    monkeypatch.setattr(vm_metadata, "mark_provisioned", mark)
    flow.run = lambda *args: cli.main(["--config", str(config), *args])
    flow.names = lambda: [name for name, _, _ in flow.events]
    return flow


@pytest.mark.parametrize("status", ["running", "stopped"])
@pytest.mark.parametrize("command,no_attach", [("run", False), ("run", True), ("shell", False)])
def test_boot_setup_follows_start_not_attachment(
    boot_flow: SimpleNamespace,
    status: str,
    command: str,
    no_attach: bool,
) -> None:
    boot_flow.status = status
    args = [command, *(["--no-attach", "--json"] if no_attach else [])]
    assert boot_flow.run(*args) == 0
    names = boot_flow.names()
    boot = ["hostname", "clock", "kvm", "docker"]
    assert [name for name in names if name in boot] == (boot if status == "stopped" else [])
    assert names.count("start") == int(status == "stopped")
    assert ("attach" in names) is not no_attach
    assert not {"tools", "git", "mark"}.intersection(names)
    assert ("prepare" in names) is (status == "stopped")
    assert ("mounts" in names) is (status == "stopped")
    if status == "stopped":
        assert names.index("prepare") < names.index("mounts")
    if status == "running":
        assert boot_flow.hostname == "smolvm"
    if status == "stopped":
        assert boot_flow.hostname == "vm1"
        assert boot_flow.kvm and boot_flow.docker


@pytest.mark.parametrize("status", ["running", "stopped"])
@pytest.mark.parametrize("options", [["--reprovision"], ["--runtime-install", "pi"]])
def test_boot_reprovision_runs_once_and_marks_last(
    boot_flow: SimpleNamespace,
    status: str,
    options: list[str],
) -> None:
    boot_flow.status = status
    assert boot_flow.run("run", *options, "--no-attach") == 0
    assert [name for name in boot_flow.names() if name != "start"] == [
        "prepare",
        "hostname",
        "clock",
        "env",
        "mounts",
        "kvm",
        "tools",
        "docker",
        "command",
        "mark",
    ]
    prepare = next(kwargs for name, _, kwargs in boot_flow.events if name == "prepare")
    assert prepare.get("copy_initial_state", False) is False


@pytest.mark.parametrize("creation", ["preset", "image", "recreate"])
def test_boot_creation_paths_share_setup(
    boot_flow: SimpleNamespace,
    creation: str,
    local_image_dir: Path,
    fake_smolvm_sdk: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boot_flow.status = None
    vm_metadata.remove_vm("vm1")
    monkeypatch.setattr(guest_setup, "host_username", lambda: "agent")
    if creation == "image":
        # The SDK fixture uses a generated ID, independently of the requested name.
        expected_name = "local-vm"
        args = ["run", "--image", str(local_image_dir)]
    elif creation == "recreate":
        boot_flow.status = "running"
        vm_metadata.claim_vm(
            "vm1",
            {"project_root": str(boot_flow.config.parent), "config_path": str(boot_flow.config)},
            "agent",
            created=True,
        )

        def delete(name, **kwargs):
            vm_metadata.remove_vm(name)
            boot_flow.status = None
            return 0

        monkeypatch.setattr(cli, "_delete_vm", delete)
        args = ["recreate", "--force"]
        expected_name = "vm1"
    else:
        args = ["run"]
        expected_name = "vm1"
    assert boot_flow.run(*args, "--no-attach", "--json") == 0
    expected = ["prepare", "hostname", "clock", "env", "mounts"]
    if creation != "image":
        expected.append("env")  # Reattach sync after the preset's initial boot work.
    assert [name for name in boot_flow.names() if name != "start"] == expected + [
        "kvm",
        "tools",
        "git",
        "docker",
        "command",
        "mark",
    ]
    assert vm_metadata.load_vm(expected_name)["provisioned"] is True
    assert json.loads(capsys.readouterr().out)["vm"]["name"] == expected_name


@pytest.mark.parametrize("failed", ["hostname", "clock", "kvm", "docker"])
@pytest.mark.parametrize("mode", ["run", "shell", "reprovision", "install"])
def test_boot_failure_blocks_normal_completion(
    boot_flow: SimpleNamespace,
    failed: str,
    mode: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boot_flow.fail = failed
    if mode == "shell":
        args = ["shell"]
    else:
        args = ["run", "--no-attach", "--json"]
        if mode == "reprovision":
            args += ["--reprovision"]
        elif mode == "install":
            args += ["--runtime-install", "pi"]
    assert boot_flow.run(*args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert f"{failed} failed" in output.err
    assert ("provisioning VM" in output.err) == (mode in ("reprovision", "install"))
    assert "sbx shell vm1 --root" in output.err
    assert "sbx run vm1 --reprovision --no-attach" in output.err
    assert not {"attach", "mark"}.intersection(boot_flow.names())
    assert boot_flow.status == "running"
    assert vm_metadata.load_vm("vm1")["provisioned"] is True


@pytest.mark.parametrize("failed", [None, "hostname", "clock", "kvm", "docker"])
def test_boot_root_shell_prepares_recorded_user_and_allows_diagnosis(
    boot_flow: SimpleNamespace,
    failed: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boot_flow.fail = failed
    boot_flow.attach_rc = 7
    assert boot_flow.run("shell", "--root") == 7
    assert boot_flow.names() == ["start", "attach"]
    for name, args, kwargs in boot_flow.events:
        if name in ("kvm", "docker"):
            assert args == ("vm1", "agent")
        if name == "attach":
            assert kwargs["user"] is None
    assert capsys.readouterr().err == ""
    assert not boot_flow.kvm and not boot_flow.docker
    boot_flow.events.clear()
    boot_flow.attach_rc = 0
    assert boot_flow.run("run") == 0
    assert boot_flow.names() == ["env", "command", "attach"]


@pytest.mark.parametrize("command", ["run", "shell"])
def test_boot_start_failure_skips_guest_operations(
    boot_flow: SimpleNamespace,
    command: str,
) -> None:
    boot_flow.start_rc = 9
    assert boot_flow.run(command, *(["--root"] if command == "shell" else [])) == 1
    assert boot_flow.names() == ["start"]


@pytest.mark.parametrize("user", ["root", None, "bad user"])
def test_boot_identity_checked_before_guest_mutation(
    boot_flow: SimpleNamespace,
    user: str | None,
) -> None:
    metadata = vm_metadata.load_vm("vm1")
    metadata["run_user"] = user
    vm_metadata.metadata_path("vm1").write_text(json.dumps(metadata), encoding="utf-8")
    if user == "root":
        assert boot_flow.run("shell", "--root") == 0
        assert boot_flow.names() == ["start", "attach"]
    else:
        assert boot_flow.run("shell", "--root") == 2
        assert not {"hostname", "clock", "kvm", "docker", "attach"}.intersection(boot_flow.names())


@pytest.mark.parametrize("provisioned", [False, True])
def test_boot_failed_setup_needs_explicit_retry(
    boot_flow: SimpleNamespace,
    provisioned: bool,
) -> None:
    metadata = vm_metadata.load_vm("vm1")
    metadata["provisioned"] = provisioned
    vm_metadata.metadata_path("vm1").write_text(json.dumps(metadata), encoding="utf-8")
    boot_flow.fail = "kvm"
    assert boot_flow.run("run", "--reprovision", "--no-attach") == 1
    assert vm_metadata.load_vm("vm1")["provisioned"] is provisioned
    boot_flow.events.clear()
    assert boot_flow.run("run", "--no-attach") == (0 if provisioned else 1)
    assert not {"hostname", "clock", "prepare", "kvm", "docker", "mark"}.intersection(
        boot_flow.names()
    )
    boot_flow.fail = None
    boot_flow.events.clear()
    assert boot_flow.run("run", "--reprovision", "--no-attach") == 0
    assert "kvm" in boot_flow.names()
    assert vm_metadata.load_vm("vm1")["provisioned"] is True


@pytest.mark.parametrize("problem", ["missing-metadata", "project", "config"])
def test_boot_root_diagnostics_do_not_bypass_host_validation(
    boot_flow: SimpleNamespace,
    problem: str,
) -> None:
    if problem == "missing-metadata":
        vm_metadata.remove_vm("vm1")
    elif problem == "project":
        metadata = vm_metadata.load_vm("vm1")
        metadata["project_root"] = "/another-project"
        vm_metadata.metadata_path("vm1").write_text(json.dumps(metadata), encoding="utf-8")
    else:
        boot_flow.config.write_text('[sbx]\nport_forwards = ["invalid"]\nname = "vm1"\n')
    assert boot_flow.run("shell", "--root") == 2
    assert boot_flow.events == []


@pytest.mark.parametrize("root_shell", [False, True])
def test_boot_account_failure_blocks_mounts_but_not_root_diagnostics(
    boot_flow: SimpleNamespace,
    root_shell: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boot_flow.fail = "prepare"
    args = ["shell", "--root"] if root_shell else ["run", "--no-attach"]
    assert boot_flow.run(*args) == (0 if root_shell else 1)
    assert "mounts" not in boot_flow.names()
    assert ("prepare" in boot_flow.names()) is not root_shell
    assert ("prepare failed" in capsys.readouterr().err) is not root_shell
    assert ("attach" in boot_flow.names()) is root_shell


@pytest.mark.parametrize("fail_clock", [False, True])
def test_boot_root_provisioning_is_not_diagnostic(
    boot_flow: SimpleNamespace,
    fail_clock: bool,
) -> None:
    metadata = vm_metadata.load_vm("vm1")
    metadata["run_user"] = "root"
    vm_metadata.metadata_path("vm1").write_text(json.dumps(metadata), encoding="utf-8")
    boot_flow.config.write_text(
        '[sbx]\nname = "vm1"\nrun_user = "root"\nauth_port = false\n',
        encoding="utf-8",
    )
    boot_flow.fail = "clock" if fail_clock else None
    assert boot_flow.run("run", "--reprovision", "--no-attach") == int(fail_clock)
    assert not {"kvm", "docker", "attach"}.intersection(boot_flow.names())
    assert ("mark" in boot_flow.names()) is not fail_clock


@pytest.mark.parametrize("keep_running", [False, True])
def test_boot_running_session_keeps_environment_tunnel_and_cleanup(
    boot_flow: SimpleNamespace,
    keep_running: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_flow.status = "running"
    boot_flow.config.write_text(
        '[sbx]\nname = "vm1"\nrun_user = "agent"\nauth_port = true\n'
        'stop_on_exit = true\nenv = ["OLLAMA_HOST"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli.network,
        "expose_auth_port",
        lambda *args: boot_flow.events.append(("port", args, {})) or 0,
    )
    stopped = []
    monkeypatch.setattr(runtime, "run_smolvm", lambda argv: stopped.append(argv) or 0)
    assert boot_flow.run("run", *(["--keep-running"] if keep_running else [])) == 0
    assert boot_flow.names() == ["env", "port", "command", "attach"]
    assert boot_flow.events[0][1] == ("vm1", ["OLLAMA_HOST"])
    assert session_state.active_sessions("vm1") == []
    assert stopped == ([] if keep_running else [["sandbox", "stop", "vm1"]])


def test_guest_clock_errors_are_not_best_effort(fake_guest_lifecycle) -> None:
    fake_guest_lifecycle.phase = lambda *a, **kw: (_ for _ in ()).throw(
        cli.ConfigError("clock failed")
    )
    with pytest.raises(cli.ConfigError, match="clock failed"):
        guest_setup.sync_guest_clock("vm1")


def test_missing_agent_does_not_mark_initial_setup_successful(boot_flow, monkeypatch):
    metadata = vm_metadata.load_vm("vm1")
    metadata["provisioned"] = False
    vm_metadata.metadata_path("vm1").write_text(json.dumps(metadata))
    monkeypatch.setattr(guest_setup, "command_path", lambda *a, **kw: None)
    assert boot_flow.run("run", "--reprovision", "--no-attach") == 1
    assert vm_metadata.load_vm("vm1")["provisioned"] is False
    assert "mark" not in boot_flow.names()


@pytest.mark.parametrize("mode", ["stopped", "reprovision", "environment"])
def test_concurrent_lifecycle_mutation_fails_before_guest_work(boot_flow, mode):
    boot_flow.status = "stopped" if mode == "stopped" else "running"
    if mode == "environment":
        with boot_flow.config.open("a") as handle:
            handle.write('env = ["SBX_SECRET"]\n')
    options = ["--reprovision"] if mode == "reprovision" else []
    with vm_metadata.Lifecycle() as active:
        active.lock("vm1")
        assert boot_flow.run("run", "--no-attach", *options) == 2
    assert boot_flow.events == []


def test_read_only_running_attachment_does_not_wait_for_mutation_lock(boot_flow):
    boot_flow.status = "running"
    with vm_metadata.Lifecycle() as active:
        active.lock("vm1")
        assert boot_flow.run("run") == 0
    assert boot_flow.names() == ["env", "command", "attach"]


def test_environment_sync_rejects_surviving_remote_phase(forwarded_env_vm):
    forwarded_env_vm.failure = "busy"
    with pytest.raises(cli.ConfigError, match="provisioning is active"):
        guest_setup.sync_forwarded_env("vm1", ["SBX_PRESENT"])
    assert not any(name in ("set", "unset") for name, _ in forwarded_env_vm.calls)
    assert forwarded_env_vm.calls[-1] == ("close", None)


def test_boot_root_diagnostics_do_not_hide_programming_errors(
    boot_flow: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(guest_setup, "wait_for_control", broken)
    with pytest.raises(RuntimeError, match="unexpected bug"):
        boot_flow.run("shell", "--root")
    assert "attach" not in boot_flow.names()
