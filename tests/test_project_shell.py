import shlex
import signal
import subprocess
from types import SimpleNamespace

import pytest

from sbx import cli, guest_setup, runtime, vm_state


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project area 'quoted'"
    child = root / "feature" / "worktree"
    child.mkdir(parents=True)
    config = root / ".sbx.toml"
    config.write_text('[sbx]\nname = "project-vm"\nproject_path = "."\n')
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", ())
    monkeypatch.chdir(child)
    return SimpleNamespace(root=root, child=child, config=config)


def test_discovery_identity_and_paths_are_shared(project):
    config = cli.load_config()
    assert config["sbx"]["project_path"] == str(project.root)
    assert config.project_config == project.config
    identity = cli._project_identity(SimpleNamespace(config=None), config)
    assert identity == {"project_root": str(project.root), "config_path": str(project.config)}
    assert cli._project_guest_cwd(config["sbx"]["project_path"]) == str(project.child)
    assert cli._project_guest_cwd(str(project.root), explicit=True) == str(project.root)


def test_nearest_config_is_rediscovered_each_invocation(project, monkeypatch):
    nested = project.child / ".sbx.toml"
    nested.write_text('[sbx]\nname = "nested"\n')
    assert cli.load_config()["sbx"]["name"] == "nested"
    monkeypatch.chdir(project.root)
    assert cli.load_config()["sbx"]["name"] == "project-vm"
    monkeypatch.chdir(project.child)
    nested.write_text("invalid TOML")
    with pytest.raises(runtime.ConfigError, match="invalid TOML"):
        cli.load_config()


def test_symlinked_config_uses_canonical_identity_and_path_base(project, tmp_path):
    source = tmp_path / "actual-config"
    source.mkdir()
    real = source / "config.toml"
    real.write_text('[sbx]\nproject_path = "."\n')
    project.config.unlink()
    project.config.symlink_to(real)
    config = cli.load_config()
    assert config["sbx"]["project_path"] == str(source)
    assert cli._project_identity(SimpleNamespace(config=None), config) == {
        "project_root": str(source),
        "config_path": str(real),
    }


def test_broken_nearest_symlink_does_not_fall_back_even_with_override(project):
    (project.child / ".sbx.toml").symlink_to(project.child / "missing-config")
    with pytest.raises(runtime.ConfigError, match="missing-config"):
        cli.load_config(str(project.config))


def test_each_config_layer_keeps_its_path_base(project, tmp_path, monkeypatch):
    global_dir = tmp_path / "defaults"
    global_dir.mkdir()
    global_config = global_dir / "config.toml"
    global_config.write_text('[sbx]\nimage = "images/debian"\nmount = ["source:/workspace"]\n')
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (global_config,))
    explicit = project.child / "custom.toml"
    explicit.write_text('[sbx]\nname = "explicit"\nproject_path = ".."\n')
    config = cli.load_config("custom.toml")
    assert config["sbx"] == {
        "name": "explicit",
        "project_path": str(project.child.parent),
        "image": str(global_dir / "images/debian"),
        "mount": [f"{global_dir}/source:/workspace"],
    }
    assert config.project_config == explicit
    # CLI paths continue to use invocation cwd, not either config directory.
    assert cli._resolve_project_path(".") == project.child


@pytest.mark.parametrize(
    "command,handler",
    [("run", "cmd_start"), ("shell", "cmd_shell"), ("stop", "cmd_stop"), ("rm", "cmd_remove")],
)
def test_all_commands_share_ancestor_discovery(project, monkeypatch, command, handler):
    captured = []
    monkeypatch.setattr(cli, handler, lambda args: captured.append(args.config_data) or 0)
    assert cli.main([command]) == 0
    assert captured[0]["sbx"]["name"] == "project-vm"
    assert captured[0].project_config == project.config


def test_auto_config_does_not_shadow_ancestor_and_init_writes_local(project):
    config = cli.load_config()
    args = SimpleNamespace(action="run", write_config=None)
    assert not cli._maybe_write_project_config(
        args, config, vm_name="project-vm", agent="pi", created=True
    )
    assert not (project.child / ".sbx.toml").exists()
    original = project.config.read_text()
    args.write_config = True
    assert cli._maybe_write_project_config(args, config, vm_name="local", agent="pi", created=True)
    assert project.config.read_text() == original
    assert 'name = "local"' in (project.child / ".sbx.toml").read_text()


def test_home_expansion_is_not_rebased_to_config_directory(project, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project.config.write_text('[sbx]\nimage = "~/images/debian"\nmount = ["~/source:/workspace"]\n')
    config = cli.load_config()
    assert config["sbx"]["image"] == str(tmp_path / "home/images/debian")
    assert config["sbx"]["mount"] == [f"{tmp_path}/home/source:/workspace"]


def test_actual_mount_mapping_and_drift(project, monkeypatch):
    mounts = [
        {"host_path": str(project.root), "guest_path": "/workspace"},
        {"host_path": str(project.child.parent), "guest_path": "/nested"},
    ]
    monkeypatch.setattr(
        vm_state, "existing_vm_start_config", lambda name: ("running", {"workspace_mounts": mounts})
    )
    assert cli._mounted_guest_cwd("vm", None) == "/nested/worktree"
    assert cli._mounted_guest_cwd("vm", str(project.root)) == "/workspace"
    with pytest.raises(runtime.ConfigError, match="not mounted"):
        cli._mounted_guest_cwd("vm", str(project.root.parent / "elsewhere"))
    monkeypatch.chdir(project.root.parent)
    assert cli._mounted_guest_cwd("vm", None) is None


@pytest.fixture
def user_ssh():
    selected = []

    def ssh(vm_id, *, user):
        selected.append((vm_id, user))
        return ["ssh", f"{user}@host"]

    return SimpleNamespace(command=ssh, selected=selected)


def test_interactive_shell_is_selected_in_guest(user_ssh):
    commands = []
    assert (
        guest_setup.attach(
            "vm",
            None,
            user="developer",
            cwd="/workspace/it's here",
            ssh=user_ssh.command,
            run=lambda argv: commands.append(argv) or 0,
        )
        == 0
    )
    assert user_ssh.selected == [("vm", "developer")]
    assert commands[0][-1] == "cd -- '/workspace/it'\"'\"'s here' && exec \"$SHELL\" -il"
    assert "-t" in commands[0]
    assert "sudo" not in commands[0][-1]


def test_cd_failure_prevents_agent_and_quotes_are_data(tmp_path, user_ssh):
    target = tmp_path / "directory 'quoted' ; not code"
    marker = tmp_path / "launched"
    agent = tmp_path / "fake pi"
    agent.write_text(f"#!/bin/sh\nprintf '%s' \"$PWD\" > {shlex.quote(str(marker))}\nexit 7\n")
    agent.chmod(0o700)

    def run(argv):
        return subprocess.run(["/bin/sh", "-c", argv[-1]], check=False).returncode

    assert guest_setup.attach("vm", str(agent), cwd=str(target), ssh=user_ssh.command, run=run) != 0
    assert not marker.exists()
    target.mkdir()
    assert guest_setup.attach("vm", str(agent), cwd=str(target), ssh=user_ssh.command, run=run) == 7
    assert marker.read_text() == str(target)


def test_agent_exec_preserves_signal_result(tmp_path, user_ssh):
    agent = tmp_path / "terminated-agent"
    agent.write_text("#!/bin/sh\nkill -TERM $$\n")
    agent.chmod(0o700)
    assert (
        guest_setup.attach(
            "vm",
            str(agent),
            user="developer",
            ssh=user_ssh.command,
            run=lambda argv: subprocess.run(["/bin/sh", "-c", argv[-1]], check=False).returncode,
        )
        == -signal.SIGTERM
    )


def test_auth_failure_has_no_root_retry(user_ssh, capsys):
    assert (
        guest_setup.attach("vm", "pi", user="developer", ssh=user_ssh.command, run=lambda argv: 255)
        == 255
    )
    assert user_ssh.selected == [("vm", "developer")]
    assert "--reprovision --no-attach" in capsys.readouterr().err


def test_agent_validation_uses_same_user_context(user_ssh):
    commands = []

    def capture(argv):
        commands.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="/home/developer/.local/bin/pi\n", stderr=""
        )

    assert (
        guest_setup.command_path("vm", "developer", "pi", ssh=user_ssh.command, run_capture=capture)
        == "/home/developer/.local/bin/pi"
    )
    assert user_ssh.selected == [("vm", "developer")]
    assert commands[0][-1] == "command -v pi"


def test_agent_validation_auth_failure_explains_explicit_repair(user_ssh):
    with pytest.raises(runtime.ConfigError, match="--reprovision --no-attach"):
        guest_setup.command_path(
            "vm",
            "developer",
            "pi",
            ssh=user_ssh.command,
            run_capture=lambda argv: subprocess.CompletedProcess(
                argv, 255, stdout="", stderr="denied"
            ),
        )
    assert user_ssh.selected == [("vm", "developer")]


def test_ssh_username_uses_supported_smolvm_api(tmp_path, monkeypatch):
    calls = []

    class VM:
        info = SimpleNamespace(config=object())

        @classmethod
        def from_id(cls, name, **kwargs):
            calls.append(kwargs)
            vm = cls()
            vm.user = kwargs.get("ssh_user", "root")
            return vm

        def _ssh_direct_command(self):
            return ["ssh", f"{self.user}@host"]

        def close(self):
            pass

    monkeypatch.setattr(runtime.smolvm.facade, "SmolVM", VM)
    monkeypatch.setattr(runtime, "guest_ssh_key_path", lambda *a: tmp_path / "existing-key")
    assert runtime.ssh_command("vm", user="developer") == ["ssh", "developer@host"]
    assert calls == [{}, {"ssh_key_path": str(tmp_path / "existing-key"), "ssh_user": "developer"}]
