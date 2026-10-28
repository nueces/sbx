import builtins
import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from smolvm.types import CommandResult

from sbx import bootstrap, guest_setup, vm_metadata
from sbx.runtime import ConfigError


@pytest.fixture
def guest_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resource = files("sbx.image").joinpath("resources/scripts/sbx-bootstrap.py")
    spec = importlib.util.spec_from_file_location("guest_script", resource)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT_UID", os.getuid())
    monkeypatch.setattr(module, "STAGE", tmp_path / "stage")
    monkeypatch.setattr(module, "ETC", tmp_path / "etc")
    monkeypatch.setattr(module, "ROOT_HOME", tmp_path / "root")
    monkeypatch.setattr(module, "LOWER_ROOT", tmp_path / "mnt", raising=False)
    monkeypatch.setattr(module, "UPPER_ROOT", tmp_path / "tmp", raising=False)
    monkeypatch.setattr(module, "LOCAL_BIN", tmp_path / "bin")
    monkeypatch.setattr(module, "FILESYSTEMS", tmp_path / "filesystems")
    module.ROOT_HOME.mkdir()
    module.LOWER_ROOT.mkdir()
    module.UPPER_ROOT.mkdir()
    module.LOCAL_BIN.mkdir()
    module.FILESYSTEMS.write_text("nodev 9p\nnodev overlay\n")
    module.STAGE.mkdir(mode=0o700)
    module.ETC.mkdir()
    commands = []
    accounts = {}

    def lookup(user):
        if user not in accounts:
            raise KeyError(user)
        return accounts[user]

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv == ["useradd", "-D"]:
            return SimpleNamespace(stdout=f"HOME={tmp_path / 'home'}\n", stderr="", returncode=0)
        if argv[0] == "useradd":
            user = argv[-1]
            home = tmp_path / "home" / user
            home.mkdir(parents=True)
            (home / ".profile").write_text("skeleton")
            accounts[user] = SimpleNamespace(
                pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid()
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(module.pwd, "getpwnam", lookup)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.Runtime, "current_mounts", lambda self: {})
    return SimpleNamespace(module=module, commands=commands, accounts=accounts, root=tmp_path)


def data(**updates):
    return (
        dict(
            version=1,
            phase="account",
            user="agent",
            vm_name="vm1",
            mounts=[],
            arguments={},
            env_names=[],
        )
        | updates
    )


def test_guest_account_precedes_home_and_preserves_repeat_setup(guest_script):
    guest = guest_script.module.Runtime(data())
    guest.account()
    home = Path(guest_script.accounts["agent"].pw_dir)
    assert (home / ".profile").read_text() == "skeleton"
    (home / "keep").write_text("unchanged")
    guest.account()
    assert (
        sum(command[0][0] == "useradd" and "-m" in command[0] for command in guest_script.commands)
        == 1
    )
    assert (home / "keep").read_text() == "unchanged"


def test_root_account_is_checked_but_never_created(guest_script):
    guest_script.accounts["root"] = SimpleNamespace(
        pw_dir=str(guest_script.module.ROOT_HOME), pw_uid=0, pw_gid=0
    )
    guest = guest_script.module.Runtime(data(user="root"))
    guest.account()
    assert guest_script.commands == []
    del guest_script.accounts["root"]
    with pytest.raises(ValueError, match="missing.*repair"):
        guest.account()
    assert guest_script.commands == []


def test_guest_failed_sudo_validation_retries_without_recreating_account(guest_script, monkeypatch):
    original = guest_script.module.subprocess.run
    fail = [True]

    def command(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == "visudo" and fail[0]:
            result.returncode, result.stderr = 1, "invalid sudoers policy"
        return result

    monkeypatch.setattr(guest_script.module.subprocess, "run", command)
    guest = guest_script.module.Runtime(data())
    with pytest.raises(ValueError, match="sudoers"):
        guest.account()
    policy = guest_script.module.ETC / "sudoers.d" / "agent"
    assert not policy.exists()
    assert not list(policy.parent.glob(".sbx-*"))
    fail[0] = False
    guest.account()
    assert policy.stat().st_mode & 0o777 == 0o440
    assert sum(argv[0] == "useradd" and "-m" in argv for argv, _ in guest_script.commands) == 1


def test_guest_missing_home_fails_without_recreation(guest_script):
    missing = guest_script.root / "missing-home"
    guest_script.accounts["agent"] = SimpleNamespace(pw_dir=str(missing), pw_uid=123, pw_gid=123)
    with pytest.raises(ValueError, match="repair"):
        guest_script.module.Runtime(data()).account()
    assert not missing.exists()
    assert guest_script.commands == []


@pytest.mark.parametrize("populated", [False, True])
def test_guest_existing_home_without_account_needs_repair(guest_script, populated):
    home = guest_script.root / "home" / "agent"
    home.mkdir(parents=True)
    if populated:
        (home / "keep").write_text("unchanged")
    with pytest.raises(ValueError, match="repair"):
        guest_script.module.Runtime(data()).account()
    assert not any("-m" in command[0] for command in guest_script.commands)
    assert home.exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"phase": "unknown"},
        {"user": "bad\nuser"},
        {"version": 2},
        {"mounts": [{"target": "/home/agent/../bad", "tag": "workspace0", "writable": True}]},
        {"mounts": [{"target": "/workspace", "tag": "-o", "writable": True}]},
        {"mounts": [{"target": "/", "tag": "workspace0", "writable": True}]},
        {"arguments": {"unexpected": True}},
    ],
)
def test_guest_rejects_invalid_data_before_commands(guest_script, updates):
    with pytest.raises(ValueError):
        guest_script.module.Runtime(data(**updates))
    assert guest_script.commands == []


def test_lifecycle_lock_rejects_overlap_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    with vm_metadata.Lifecycle() as first:
        first.lock("vm1")
        first.lock("vm1")
        with vm_metadata.Lifecycle() as second, pytest.raises(ConfigError, match="in progress"):
            second.lock("vm1")
    with vm_metadata.Lifecycle() as later:
        later.lock("vm1")


def test_guest_script_runs_in_isolated_subprocess(tmp_path):
    resource = files("sbx.image").joinpath("resources/scripts/sbx-bootstrap.py")
    config = tmp_path / "input.json"
    config.write_text(json.dumps(data(phase="unknown")))
    # Invalid input must fail before any privileged operation, even without root.
    result = subprocess.run(["python3", str(resource), str(config)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_real_script_phases_run_in_isolated_subprocess_with_command_stubs(tmp_path):
    root = tmp_path / "guest area 'quoted'"
    root.mkdir()
    driver = """import importlib.util, json, os, pathlib, sys
from types import SimpleNamespace
import pytest
spec = importlib.util.spec_from_file_location("isolated_resource_tests", sys.argv[1])
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
with pytest.MonkeyPatch.context() as patch:
    state = tests.guest_script.__wrapped__(pathlib.Path(sys.argv[2]), patch)
    m = state.module
    patch.setattr(m, "secure_directory", lambda path: None)
    patch.setattr(m.os, "getgrouplist", lambda user, gid: [gid])
    patch.setenv("TOKEN", "SYNTHETIC_ISOLATED_SECRET")
    mounted, installed = {}, set()
    fail = [True]
    original = m.subprocess.run
    def command(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == "mount":
            mounted[argv[-1]] = dict(source=argv[-2], fstype=argv[2], options=argv[4])
        elif argv[:2] == ["bash", "-lc"]:
            result.returncode = 0 if "pi" in installed else 1
        elif argv[0] == "timeout":
            assert kwargs["env"]["HOME"] == str(home)
            if fail[0]:
                result.returncode = 1
                result.stderr = "installer failed SYNTHETIC_ISOLATED_SECRET"
            else:
                installed.add("pi")
        return result
    patch.setattr(m.subprocess, "run", command)
    patch.setattr(m.Runtime, "current_mounts", lambda self: mounted)
    home = state.root / "home" / "agent"
    target = home / "project 'quoted'"
    mounts = [dict(target=str(target), tag="workspace0", writable=True)]
    config = m.STAGE / "phase.json"
    def phase(name, arguments=None):
        config.write_text(json.dumps(tests.data(phase=name, mounts=mounts,
                                               arguments=arguments or {}, env_names=["TOKEN"])))
        config.chmod(0o600)
        return m.main([str(config)])
    assert phase("mounts") == 1 and not home.exists()
    assert phase("account") == 0
    assert (home / ".profile").read_text() == "skeleton"
    assert not target.exists()
    assert phase("account") == 0
    assert phase("mounts") == 0 and phase("mounts") == 0
    assert phase("tools", dict(tools=["pi"], install_timeout=2)) == 1
    fail[0] = False
    assert phase("tools", dict(tools=["pi"], install_timeout=2)) == 0
    assert phase("tools", dict(tools=["pi"], install_timeout=2)) == 0
    commands = [argv for argv, _ in state.commands]
    assert sum(argv[0] == "useradd" and "-m" in argv for argv in commands) == 1
    assert sum(argv[0] == "mount" for argv in commands) == 1
    assert sum(argv[0] == "timeout" for argv in commands) == 2
    assert next(argv for argv in commands if argv[0] == "mount")[-1] == str(target)
    print("PASS: real entrypoint account/mount/install ordering, failure, retry and quoted paths")
"""
    result = subprocess.run(
        [sys.executable, "-c", driver, str(Path(__file__).resolve()), str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS: real entrypoint" in result.stdout
    assert "SYNTHETIC_ISOLATED_SECRET" not in result.stderr
    assert "Traceback" not in result.stderr


def test_host_staging_rejects_workspace_covering_bootstrap():
    vm = SimpleNamespace(
        info=SimpleNamespace(
            config=SimpleNamespace(
                workspace_mounts=[
                    SimpleNamespace(
                        guest_path="/var", resolved_tag=lambda i: "workspace0", writable=True
                    ),
                ]
            )
        )
    )
    with pytest.raises(ConfigError, match="bootstrap"):
        bootstrap.GuestBootstrap(vm, "agent", "vm1")


@pytest.fixture
def account_home(guest_script):
    guest = guest_script.module.Runtime(data())
    guest.account()
    guest_script.commands.clear()
    return guest, Path(guest_script.accounts["agent"].pw_dir)


def test_guest_credentials_publish_once_and_clean_partial_copy(
    guest_script, account_home, monkeypatch
):
    guest, home = account_home
    source = guest_script.module.ROOT_HOME / ".pi"
    source.mkdir()
    (source / "settings").write_text("synthetic")
    stale = home / ".sbx-copy-abandoned"
    stale.mkdir(mode=0o700)
    (stale / "partial").write_text("partial")
    marker = stale / guest_script.module.COPY_MARKER
    marker.write_text(guest_script.module.COPY_MAGIC)
    marker.chmod(0o600)
    original = guest_script.module.shutil.copytree

    def fail(source, destination, **kwargs):
        destination.mkdir()
        (destination / "partial").write_text("partial")
        raise OSError("interrupted")

    monkeypatch.setattr(guest_script.module.shutil, "copytree", fail)
    with pytest.raises(OSError, match="interrupted"):
        guest.credentials()
    assert not (home / ".pi").exists()
    monkeypatch.setattr(guest_script.module.shutil, "copytree", original)
    guest.credentials()
    assert (home / ".pi/settings").read_text() == "synthetic"
    assert (home / ".pi").stat().st_mode & 0o777 == 0o700
    assert not list(home.glob(".sbx-copy-*"))
    (source / "settings").write_text("changed")
    guest.credentials()
    assert (home / ".pi/settings").read_text() == "synthetic"


def test_guest_credential_symlink_is_never_walked_or_chmodded(
    guest_script, account_home, monkeypatch
):
    guest, home = account_home
    external = guest_script.root / "external"
    external.mkdir()
    (external / "keep").write_text("host-data")
    before = (external / "keep").stat()
    (guest_script.module.ROOT_HOME / ".pi").symlink_to(external, target_is_directory=True)
    owned = []
    monkeypatch.setattr(guest_script.module.os, "lchown", lambda path, *a: owned.append(Path(path)))
    guest.credentials()
    assert (home / ".pi").is_symlink()
    assert len(owned) == 1 and owned[0].name == ".pi"
    after = (external / "keep").stat()
    assert (before.st_uid, before.st_gid, before.st_mode) == (
        after.st_uid,
        after.st_gid,
        after.st_mode,
    )
    assert (external / "keep").read_text() == "host-data"


def test_guest_credential_file_is_private_after_publication(guest_script, account_home):
    guest, home = account_home
    source = guest_script.module.ROOT_HOME / ".claude.json"
    source.write_text("synthetic")
    source.chmod(0o644)
    guest.credentials()
    assert (home / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_guest_copy_cleanup_preserves_unmarked_user_directory(guest_script, account_home):
    guest, home = account_home
    backup = home / ".sbx-copy-backup"
    backup.mkdir(mode=0o700)
    (backup / "keep").write_text("user-data")
    guest.credentials()
    assert (backup / "keep").read_text() == "user-data"


def test_guest_initial_copies_skip_share_covered_destinations(guest_script, account_home, capsys):
    _, home = account_home
    (guest_script.module.ROOT_HOME / ".claude.json").write_text("synthetic")
    guest = guest_script.module.Runtime(
        data(mounts=[dict(target=str(home), tag="workspace0", writable=True)])
    )
    guest.credentials()
    guest.args = {"text": "[user]\nname = test\n"}
    guest.git()
    assert not (home / ".claude.json").exists()
    assert not (home / ".gitconfig").exists()
    assert "skipped initial configuration" in capsys.readouterr().err


def test_guest_git_preserves_existing_file(guest_script, account_home):
    _, home = account_home
    guest = guest_script.module.Runtime(
        data(phase="git", arguments={"text": "[user]\nname = test\n"})
    )
    guest.git()
    assert (home / ".gitconfig").read_text() == guest.args["text"]
    assert (home / ".gitconfig").stat().st_mode & 0o777 == 0o600
    guest.args["text"] = "replacement"
    guest.git()
    assert "replacement" not in (home / ".gitconfig").read_text()


@pytest.mark.parametrize("tool", ["pi", "specify"])
def test_guest_tools_preserve_existing_commands(guest_script, account_home, tool):
    guest = guest_script.module.Runtime(
        data(phase="tools", arguments=dict(tools=[tool], install_timeout=123))
    )
    guest.tools()
    assert len(guest_script.commands) == 1
    assert "command -v" in guest_script.commands[0][0][-1]


@pytest.mark.parametrize("tool", ["pi", "specify"])
def test_guest_tools_keep_installer_security_context_and_deadline(
    guest_script, account_home, monkeypatch, tool
):
    _, home = account_home
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=127 if len(calls) == 1 else 0, stdout="", stderr="")

    monkeypatch.setattr(guest_script.module.subprocess, "run", run)
    guest = guest_script.module.Runtime(
        data(phase="tools", arguments=dict(tools=[tool], install_timeout=123))
    )
    guest.lock_fd = 42
    guest.tools()
    argv, options = calls[1]
    assert argv[:5] == ["timeout", "--signal=TERM", "--kill-after=10s", "123s", "bash"]
    assert options["user"] == os.getuid() and options["group"] == os.getgid()
    assert options["env"]["HOME"] == str(home)
    assert options["pass_fds"] == (42,)
    assert options["timeout"] == 138
    if tool == "pi":
        assert 'npm install --global --prefix "$HOME/.local" --ignore-scripts' in argv[-1]
        assert "NPM_CONFIG_USERCONFIG=/dev/stdin" in argv[-1]
        assert "NPM_CONFIG_GLOBALCONFIG=/dev/null" in argv[-1]
        assert "</dev/null" in argv[-1]
        assert "/etc/profile.d/smolvm_env.sh" in argv[-1]
    else:
        assert (
            "uv tool install specify-cli --from git+https://github.com/github/spec-kit.git"
            in argv[-1]
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_secrets_are_redacted_before_host_sanitization(
    guest_script, account_home, monkeypatch, reverse
):
    values = {
        "SBX_BASE": "https://registry.example",
        "SBX_AUTH": "https://registry.example/?token=SYNTHETIC_SECRET",
        "SBX_EMPTY": "",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    names = list(reversed(values)) if reverse else list(values)
    result = SimpleNamespace(
        returncode=1, stdout="install failed " + values["SBX_AUTH"], stderr="nonsecret details"
    )
    monkeypatch.setattr(guest_script.module.subprocess, "run", lambda *a, **kw: result)
    guest = guest_script.module.Runtime(data(env_names=names))
    host = bootstrap.GuestBootstrap(
        SimpleNamespace(
            info=SimpleNamespace(config=SimpleNamespace(workspace_mounts=[], env_vars={}))
        ),
        "agent",
        "vm1",
        names,
    )
    with pytest.raises(ValueError) as error:
        guest.run(["synthetic-installer"])
    message = host.sanitized(str(error.value))
    assert "SYNTHETIC_SECRET" not in message
    assert "token=" not in message
    assert "[redacted]" in message and "nonsecret details" in message
    assert guest.redact("unrelated output") == "unrelated output"


def test_guest_installer_failure_is_redacted(guest_script, account_home, monkeypatch):
    monkeypatch.setenv("SBX_SECRET", "SENTINEL_SECRET")
    results = iter(
        [
            SimpleNamespace(returncode=127, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="failed SENTINEL_SECRET", stderr="details"),
        ]
    )
    monkeypatch.setattr(guest_script.module.subprocess, "run", lambda *a, **kw: next(results))
    guest = guest_script.module.Runtime(
        data(
            phase="tools",
            arguments=dict(tools=["specify"], install_timeout=123),
            env_names=["SBX_SECRET"],
        )
    )
    with pytest.raises(ValueError, match="failed .*redacted") as caught:
        guest.tools()
    assert "SENTINEL_SECRET" not in str(caught.value)


@pytest.mark.parametrize("initial", ["127.0.0.1 localhost\n", "127.0.1.1 old-name\n"])
def test_guest_hostname_and_clock_phases_use_disposable_files(guest_script, initial):
    module = guest_script.module
    (module.ETC / "hosts").write_text(initial)
    module.Runtime(data(phase="hostname")).execute()
    assert (module.ETC / "hostname").read_text() == "vm1\n"
    assert (module.ETC / "hosts").read_text().count("127.0.1.1 vm1") == 1
    arguments = dict(timestamp="2026-10-27 10:00:00", timezone="UTC")
    module.Runtime(data(phase="clock", arguments=arguments)).execute()
    assert (module.ETC / "timezone").read_text() == "UTC\n"
    assert (module.ETC / "localtime").is_symlink()
    assert guest_script.commands[-1][0] == ["date", "-u", "-s", arguments["timestamp"]]


def test_guest_failed_git_publication_cleans_private_temporary(
    guest_script, account_home, monkeypatch
):
    guest, home = account_home
    guest.phase, guest.args = "git", {"text": "[user]\nname = Synthetic User\n"}
    monkeypatch.setattr(
        guest_script.module.os,
        "link",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("publish failed")),
    )
    with pytest.raises(OSError, match="publish failed"):
        guest.git()
    assert not (home / ".gitconfig").exists()
    assert not list(home.glob(".sbx-git-*"))


@pytest.mark.parametrize("distribution", ["ubuntu", "debian"])
@pytest.mark.parametrize("filesystems,writable", [("", True), ("nodev 9p\n", False)])
def test_guest_missing_modules_fail_before_mount_targets(
    guest_script, account_home, distribution, filesystems, writable
):
    guest, home = account_home
    (guest_script.module.ETC / "os-release").write_text(f"ID={distribution}\n")
    guest_script.module.FILESYSTEMS.write_text(filesystems)
    target = home / "project"
    guest.mounts = [dict(target=target, tag="workspace0", writable=writable)]
    with pytest.raises(ValueError, match="kernel lacks.*rebuild a supported image"):
        guest.mounts_phase()
    assert not target.exists()
    assert all(argv[0] == "modprobe" for argv, _ in guest_script.commands)


def test_guest_mount_order_repeat_and_conflicts(guest_script, account_home, monkeypatch):
    _, home = account_home
    current = {}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "mount":
            kind, options, source, target = argv[2], argv[4], argv[-2], argv[-1]
            current[target] = dict(target=target, source=source, fstype=kind, options=options)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(guest_script.module.Runtime, "current_mounts", lambda self: current)
    monkeypatch.setattr(guest_script.module.subprocess, "run", run)
    guest = guest_script.module.Runtime(
        data(
            phase="mounts",
            mounts=[
                dict(target=str(home / "space and 'quote'"), tag="workspace0", writable=True),
                dict(target=str(home / "overlay"), tag="workspace1", writable=False),
            ],
        )
    )
    guest.mounts_phase()
    assert len(calls) == 3
    assert calls[0][-1] == str(home / "space and 'quote'")
    assert calls[1][2] == "9p" and "ro" in calls[1][4]
    assert calls[2][2] == "overlay"
    for item in current.values():
        item["options"] = item["options"].replace(",version=9p2000.L", "")
    guest.mounts_phase()
    assert len(calls) == 3
    current[str(home / "overlay")]["source"] = "conflicting"
    with pytest.raises(ValueError, match="conflicting"):
        guest.mounts_phase()
    assert not any(command[0] == "umount" for command in calls)


def test_guest_mount_failure_blocks_dependent_mounts(guest_script, account_home, monkeypatch):
    _, home = account_home
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing device")

    monkeypatch.setattr(guest_script.module.subprocess, "run", fail)
    guest = guest_script.module.Runtime(
        data(
            phase="mounts",
            mounts=[dict(target=str(home / "overlay"), tag="workspace0", writable=False)],
        )
    )
    with pytest.raises(ValueError, match="missing device"):
        guest.mounts_phase()
    assert len(calls) == 1


def test_guest_missing_account_blocks_target_creation(guest_script):
    target = guest_script.root / "new-target"
    guest = guest_script.module.Runtime(
        data(phase="mounts", mounts=[dict(target=str(target), tag="workspace0", writable=True)])
    )
    with pytest.raises(ValueError, match="account.*missing"):
        guest.mounts_phase()
    assert not target.exists()
    assert guest_script.commands == []


def test_guest_optional_helpers_and_daemon_lock_handling(guest_script, account_home):
    guest, _ = account_home
    guest.lock_fd = 42
    guest.phase = "kvm"
    guest.execute()
    assert guest_script.commands == []
    for name in ("sbx-prepare-kvm", "sbx-start-rootless-docker"):
        path = guest_script.module.LOCAL_BIN / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    guest.execute()
    guest.phase = "docker"
    guest.execute()
    assert guest_script.commands[0][1]["pass_fds"] == (42,)
    assert guest_script.commands[1][1]["pass_fds"] == (42,)


@pytest.fixture
def control():
    class VM:
        info = SimpleNamespace(config=SimpleNamespace(workspace_mounts=[], env_vars={}))

        def __init__(self):
            self.commands = []
            self.uploads = []
            self.fail = None

        def run(self, command, **kwargs):
            self.commands.append(command)
            if command.startswith("timeout ") and self.fail == "timeout":
                raise TimeoutError("connection lost")
            if command.startswith("timeout ") and self.fail == "phase":
                return CommandResult(exit_code=1, stdout="", stderr="failed SENTINEL_SECRET")
            return CommandResult(exit_code=0, stdout="", stderr="")

        def upload_file(self, source, destination, **kwargs):
            if self.fail == "upload":
                raise OSError("transfer failed")
            self.uploads.append((destination, Path(source).read_bytes(), kwargs))

    return VM()


def test_host_redacts_configured_host_and_preset_values_without_staging_them(control, monkeypatch):
    control.info.config.env_vars = {"TOKEN": "configured-secret-long"}
    monkeypatch.setenv("TOKEN", "configured-secret")
    values = {"PRESET_TOKEN": "preset-secret"}
    control.set_env_vars = lambda *a, **kw: None
    control.list_env_vars = lambda: values
    guest = bootstrap.GuestBootstrap(control, "agent", "vm1", env_names=["TOKEN"])
    guest.set_managed_env(values)
    assert guest.sanitized("configured-secret-long configured-secret preset-secret") == (
        "[redacted] [redacted] [redacted]"
    )
    assert all(
        secret.encode() not in payload
        for _, payload, _ in control.uploads
        for secret in [*values.values(), "configured-secret"]
    )


@pytest.mark.parametrize("fail", [False, True])
def test_preset_upload_is_private_before_credential_bytes(control, tmp_path, fail):
    source = tmp_path / "credential"
    source.write_text("SYNTHETIC_CREDENTIAL")
    guest = bootstrap.GuestBootstrap(control, "agent", "vm1")
    original = control.run

    def command(text, **kwargs):
        if "install -m 600" in text and fail:
            return CommandResult(exit_code=1, stdout="", stderr="permission denied")
        return original(text, **kwargs)

    control.run = command
    if fail:
        with pytest.raises(ConfigError, match="private preset transfer"):
            guest.put_file(source, "/root/credential")
        assert all(payload != source.read_bytes() for _, payload, _ in control.uploads)
    else:
        guest.put_file(source, "/root/credential")
        assert "install -m 600 -- /dev/null /root/credential" in control.commands[-1]
        assert control.uploads[-1] == (
            "/root/credential",
            source.read_bytes(),
            {"make_dirs": False},
        )


def test_mount_deadline_covers_overlay_pairs_without_package_install_budget(control):
    guest = bootstrap.GuestBootstrap(control, "agent", "vm1")
    guest.mounts = [
        dict(target=f"/workspace{index}", tag=f"workspace{index}", writable=False)
        for index in range(2)
    ]
    guest_setup.mount_workspaces("vm1", "agent", guest=guest)
    assert any(
        command.startswith("timeout --signal=TERM --kill-after=10s 90s ")
        for command in control.commands
    )


def test_host_stages_script_and_nonsecret_data(control, monkeypatch):
    monkeypatch.setenv("SBX_SECRET", "SENTINEL_SECRET")
    guest = bootstrap.GuestBootstrap(control, "agent", "vm1", ["SBX_SECRET"])
    guest.phase("account")
    guest.phase("tools", dict(tools=["pi"], install_timeout=123))
    scripts = [payload for path, payload, _ in control.uploads if path.endswith(".py")]
    configs = [
        json.loads(payload) for path, payload, _ in control.uploads if path.endswith(".json")
    ]
    # Uploads first land at random private incoming names, then are published.
    assert len(control.uploads) == 3
    assert all(b"SENTINEL_SECRET" not in payload for _, payload, _ in control.uploads)
    assert b"Trusted, root-run sbx provisioning" in control.uploads[0][1]
    assert json.loads(control.uploads[1][1])["phase"] == "account"
    assert all(options == {"make_dirs": False} for _, _, options in control.uploads)
    assert not scripts and not configs
    assert "is_symlink" in control.commands[0]
    assert any("os.chmod(source, 0o600)" in command for command in control.commands)


@pytest.mark.parametrize("failure", ["timeout", "phase", "upload"])
def test_host_failures_redact_or_preserve_in_use_inputs(control, monkeypatch, failure):
    monkeypatch.setenv("SBX_SECRET", "SENTINEL_SECRET")
    guest = bootstrap.GuestBootstrap(control, "agent", "vm1", ["SBX_SECRET"])
    control.fail = failure
    with pytest.raises((ConfigError, OSError, TimeoutError)) as caught:
        guest.phase("tools", dict(tools=["pi"], install_timeout=1))
    assert "SENTINEL_SECRET" not in str(caught.value)
    if failure == "timeout":
        assert not control.commands[-1].startswith("rm ")
    if failure == "upload":
        assert control.commands[-1].startswith("rm -f -- ")


@pytest.mark.parametrize("failure", [None, "ready", "sync"])
@pytest.mark.parametrize("stop_fails", [False, True])
def test_best_effort_stop_flushes_but_preserves_stop_result(
    tmp_path, monkeypatch, capsys, failure, stop_fails
):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    events = []

    class VM:
        def wait_for_ready(self, **kwargs):
            events.append("ready")
            if failure == "ready":
                raise TimeoutError("unreachable")

        def run(self, command, **kwargs):
            assert command == "sync"
            events.append("sync")
            return SimpleNamespace(exit_code=int(failure == "sync"))

    @contextmanager
    def opened(*args, **kwargs):
        try:
            yield VM()
        finally:
            events.append("close")

    class Manager:
        data_dir = tmp_path

        def stop(self, name):
            events.append("stop")
            if stop_fails:
                raise RuntimeError("stop failed")

        def close(self):
            events.append("manager-close")

    monkeypatch.setattr(guest_setup, "open_guest", opened)
    monkeypatch.setattr(guest_setup, "SmolVMManager", Manager)
    monkeypatch.setattr(guest_setup, "_STARTED", {}, raising=False)
    assert guest_setup.stop_vm("vm1") == int(stop_fails)
    assert events.index("close") < events.index("stop")
    assert events[-1] == "manager-close"
    assert ("sync" in events) is (failure != "ready")
    assert ("warning" in capsys.readouterr().err) is (failure is not None)


def test_start_retains_manager_until_owned_process_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    managers = []

    class Manager:
        data_dir = tmp_path

        def __init__(self):
            self.started = False
            self.closed = False
            managers.append(self)

        def start(self, name, **kwargs):
            self.started = True

        def stop(self, name):
            assert self.started, "must use the manager retaining this process handle"

        def close(self):
            self.closed = True

    @contextmanager
    def offline(*args, **kwargs):
        raise TimeoutError("offline")
        yield

    monkeypatch.setattr(guest_setup, "SmolVMManager", Manager)
    monkeypatch.setattr(guest_setup, "_STARTED", {}, raising=False)
    monkeypatch.setattr(guest_setup, "open_guest", offline)
    guest_setup.start_vm("vm1", boot_timeout=60)
    assert not managers[0].closed
    assert guest_setup.stop_vm("vm1") == 0
    assert all(manager.closed for manager in managers)


def test_unchanged_claim_does_not_overwrite_concurrent_success(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    project = dict(project_root="/project", config_path="/project/sbx.toml")
    vm_metadata.claim_vm("vm1", project, "agent", created=True)
    vm_metadata.mark_provisioned("vm1")
    monkeypatch.setattr(
        vm_metadata, "_write", lambda *a, **kw: pytest.fail("read-only reuse rewrote metadata")
    )
    assert vm_metadata.claim_vm("vm1", project, "agent", created=False)["provisioned"] is True


def test_real_script_execution_lease_survives_parent_disconnect(tmp_path):
    resource = files("sbx.image").joinpath("resources/scripts/sbx-bootstrap.py")
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    config = stage / "input.json"
    config.write_text(json.dumps(data(phase="check")))
    config.chmod(0o600)
    ready, release = tmp_path / "ready", tmp_path / "release"
    child = """import pathlib, sys, time
ready, release = map(pathlib.Path, sys.argv[1:])
ready.touch()
deadline = time.monotonic() + 5
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
"""
    driver = """import importlib.util, os, pathlib, sys
source, stage, config, child, ready, release = sys.argv[1:]
spec = importlib.util.spec_from_file_location("isolated_guest", source)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.ROOT_UID = os.getuid()
m.STAGE = pathlib.Path(stage)
# Deployment-directory trust is tested separately; never touch real guest/host directories.
m.secure_directory = lambda path: None
m.Runtime.execute = lambda self: self.run([sys.executable, "-c", child, ready, release])
raise SystemExit(m.main([config]))
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            driver,
            str(resource),
            str(stage),
            str(config),
            child,
            str(ready),
            str(release),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.terminate()  # Like losing the parent, not a force-kill of its process group.
        process.wait(timeout=5)
        with (stage / "execution.lock").open("r+") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.touch()
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
    finally:
        release.touch()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_guest_rejects_untrusted_input_permissions(guest_script, mode):
    path = guest_script.module.STAGE / "input.json"
    path.write_text(json.dumps(data()))
    path.chmod(mode)
    with pytest.raises(ValueError, match="private root-owned"):
        guest_script.module.secure_file(path)


def test_guest_rejects_symlinked_config_and_target(guest_script, account_home):
    guest, home = account_home
    real = home / "real"
    real.mkdir()
    link = home / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        guest.mount_one("workspace0", link / "project", "9p", "rw")
    config = guest_script.module.STAGE / "input.json"
    config.symlink_to(home / "keep")
    with pytest.raises(OSError):
        guest_script.module.secure_file(config)


def test_guest_atomic_publish_does_not_replace_existing_destination(guest_script):
    source, destination = guest_script.root / "copy", guest_script.root / "destination"
    source.mkdir()
    (source / "content").write_text("new")
    destination.mkdir()
    (destination / "keep").write_text("existing")
    guest_script.module.publish_noreplace(source, destination)
    assert (destination / "keep").read_text() == "existing"
    assert not (destination / "content").exists()


def test_guest_wrong_protocol_mount_is_not_reused(guest_script, account_home, monkeypatch):
    guest, home = account_home
    target = home / "project"
    monkeypatch.setattr(
        guest_script.module.Runtime,
        "current_mounts",
        lambda self: {
            str(target): dict(
                target=str(target),
                source="workspace0",
                fstype="9p",
                options="rw,trans=virtio,version=9p2000.u",
            )
        },
    )
    with pytest.raises(ValueError, match="conflicting"):
        guest.mount_one("workspace0", target, "9p", "rw,trans=virtio,version=9p2000.L")


@pytest.mark.parametrize("runner", ["host", "resource"])
@pytest.mark.parametrize(
    "kind,target,blocked",
    [
        ("9p", "/var", True),
        ("virtiofs", "/var/lib", True),
        ("overlay", "/var/lib/sbx", True),
        ("9p", "/home/agent/project", False),
        ("ext4", "/", False),
        ("overlay", "/", False),
    ],
)
def test_staging_rejects_active_shared_storage(
    guest_script, monkeypatch, runner, kind, target, blocked
):
    class Directory(PurePosixPath):
        def is_symlink(self):
            return False

        def exists(self):
            return True

        def mkdir(self, **kwargs):
            pass

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)

        def chmod(self, mode):
            assert mode == 0o700

    output = json.dumps({"filesystems": [dict(target=target, fstype=kind)]})
    process = SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
    monkeypatch.setattr(guest_script.module, "ROOT_UID", 0)
    monkeypatch.setattr(guest_script.module.subprocess, "run", process.run)
    replacements = {"pathlib": SimpleNamespace(Path=Directory), "subprocess": process}

    def imports(name, *args, **kwargs):
        return replacements.get(name) or builtins.__import__(name, *args, **kwargs)

    def probe():
        if runner == "resource":
            guest_script.module.secure_directory(Directory(bootstrap.STAGE))
        else:
            exec(bootstrap.PREPARE, {"__builtins__": dict(vars(builtins), __import__=imports)})

    if blocked:
        with pytest.raises(ValueError, match="active shared folder"):
            probe()
    else:
        probe()


def test_resource_compiles_without_checkout_execution():
    resource = files("sbx.image").joinpath("resources/scripts/sbx-bootstrap.py")
    compile(resource.read_text(), str(resource), "exec")


@pytest.fixture
def ssh_guest(guest_script):
    guest_script.module.Runtime(data()).account()
    guest = guest_script.module.Runtime(
        data(phase="ssh", arguments={"public_key": "ssh-ed25519 cHVibGlj sbx comment"})
    )
    return guest_script, guest, Path(guest_script.accounts["agent"].pw_dir)


def test_authorize_user_key_is_idempotent_and_preserves_restricted_entries(ssh_guest):
    script, guest, home = ssh_guest
    directory = home / ".ssh"
    directory.mkdir()
    path = directory / "authorized_keys"
    original = '# Keep this\ncommand="echo no access",no-pty ssh-ed25519 cHVibGlj existing\n'
    path.write_text(original)
    path.chmod(0o666)
    guest.execute()
    guest.execute()
    assert path.read_text() == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_uid == script.accounts["agent"].pw_uid


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_authorize_user_key_appends_without_replacing_existing_file(ssh_guest, trailing_newline):
    _, guest, home = ssh_guest
    directory = home / ".ssh"
    directory.mkdir()
    path = directory / "authorized_keys"
    original = "ssh-ed25519 b3RoZXI= unrelated" + ("\n" if trailing_newline else "")
    path.write_text(original)
    inode = path.stat().st_ino
    # A previous implementation's abandoned file is unrelated to append-only installation.
    abandoned = directory / ".sbx-authorized-keys"
    abandoned.write_text("leave this alone")
    guest.execute()
    guest.execute()
    assert path.read_text() == "ssh-ed25519 b3RoZXI= unrelated\nssh-ed25519 cHVibGlj\n"
    assert path.stat().st_ino == inode
    assert abandoned.read_text() == "leave this alone"


@pytest.mark.parametrize("options", ["", 'from="192.0.2.1" ', 'command="echo restricted",no-pty '])
@pytest.mark.parametrize("comment", ["Alice's laptop", 'unmatched "quote', "trailing backslash\\"])
def test_authorization_key_comment_is_opaque(ssh_guest, options, comment):
    _, guest, home = ssh_guest
    identity = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN9OphNCyuOQM8NJB2xJX6qsi9wMDh4L3iWVhDbRUJ4b"
    guest.args["public_key"] = identity
    directory = home / ".ssh"
    directory.mkdir()
    path = directory / "authorized_keys"
    original = f"{options}{identity} {comment}\n"
    path.write_text(original)
    guest.execute()
    guest.execute()
    assert path.read_text() == original


def test_authorization_rejects_unparseable_prefix_without_granting_access(ssh_guest):
    _, guest, home = ssh_guest
    directory = home / ".ssh"
    directory.mkdir()
    path = directory / "authorized_keys"
    original = 'command="unterminated ssh-ed25519 cHVibGlj\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="authorized_keys"):
        guest.execute()
    assert path.read_text() == original


@pytest.mark.parametrize("unsafe", ["directory-link", "file-link", "hardlink", "shared"])
def test_authorize_user_key_refuses_unsafe_layouts(ssh_guest, monkeypatch, unsafe):
    script, guest, home = ssh_guest
    directory = home / ".ssh"
    protected = script.root / "must-not-change"
    protected.write_text("private data")
    if unsafe == "directory-link":
        directory.symlink_to(script.root, target_is_directory=True)
    else:
        directory.mkdir()
        if unsafe == "file-link":
            (directory / "authorized_keys").symlink_to(protected)
        elif unsafe == "hardlink":
            os.link(protected, directory / "authorized_keys")
        else:
            monkeypatch.setattr(
                guest, "current_mounts", lambda: {"home": {"target": str(home), "fstype": "9p"}}
            )
    with pytest.raises((ValueError, OSError)):
        guest.execute()
    assert protected.read_text() == "private data"


def test_initial_ssh_copy_precedes_authorization(ssh_guest):
    script, guest, home = ssh_guest
    source = script.module.ROOT_HOME / ".ssh"
    source.mkdir()
    (source / "config").write_text("existing ssh config")
    script.module.Runtime(data(phase="credentials")).execute()
    guest.execute()
    assert (home / ".ssh/config").read_text() == "existing ssh config"
    assert (home / ".ssh/authorized_keys").read_text() == "ssh-ed25519 cHVibGlj\n"


@pytest.mark.parametrize("key", [None, "", "ssh-ed25519 !!", "ssh-ed25519 cHVibGlj\ninjected"])
def test_ssh_phase_rejects_invalid_key_before_writes(guest_script, key):
    with pytest.raises(ValueError):
        guest_script.module.Runtime(data(phase="ssh", arguments={"public_key": key}))
    assert guest_script.commands == []
