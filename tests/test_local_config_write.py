from types import SimpleNamespace

import pytest

from sbx import cli, guest_setup, vm_metadata


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    source = parent / ".sbx.toml"
    source.write_text(
        '[sbx]\nname="parent-vm"\nproject_path="."\nrun_user="agent"\n'
        'mount=[".:/workspace"]\nmemory=2048\nstop_on_exit=false\nruntime_install=[]\n'
    )
    monkeypatch.chdir(child)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", ())
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda *args: None)
    monkeypatch.setattr(guest_setup, "host_username", lambda: "agent")
    monkeypatch.setattr(guest_setup, "credential_free_env", lambda *args, **kwargs: {})
    statuses, calls, creations, updates = {}, [], [], []
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: statuses.get(name))
    monkeypatch.setattr(cli, "_warn_running_mount_drift", lambda *a, **kw: None)

    def create(agent, **kwargs):
        calls.append("create")
        name = kwargs["vm_name"]
        kwargs["before_provision"](name)
        statuses[name] = "running"
        creations.append(kwargs)
        return SimpleNamespace(vm_id=name, close=lambda: None)

    def post(**kwargs):
        calls.append("post")
        vm_metadata.mark_provisioned(kwargs["vm_name"])
        return 0

    def delete(name, **kwargs):
        calls.append("delete")
        statuses.pop(name, None)
        vm_metadata.remove_vm(name)
        return 0

    monkeypatch.setattr(cli.smolvm_preset, "create_preset", create)
    monkeypatch.setattr(cli, "_post_start_actions", post)
    monkeypatch.setattr(cli, "_delete_vm", delete)
    monkeypatch.setattr(
        cli, "_start_existing_vm_if_needed", lambda *a, **kw: calls.append("start") or 0
    )
    monkeypatch.setattr(cli, "_sync_existing_vm_start_config", lambda *a, **kw: updates.append(a))
    return SimpleNamespace(
        parent=parent,
        child=child,
        source=source,
        statuses=statuses,
        calls=calls,
        creations=creations,
        updates=updates,
    )


@pytest.mark.parametrize("action", ["run", "create", "recreate"])
@pytest.mark.parametrize("explicit_source", [False, True])
def test_new_local_project_identity_is_used_from_first_invocation(
    local_project, action, explicit_source
):
    project = local_project
    original = project.source.read_text()
    prefix = ["--config", str(project.source)] if explicit_source else []
    flags = ["--force"] if action == "recreate" else []
    assert cli.main([*prefix, action, "local-vm", "--write-config", "--no-attach", *flags]) == 0
    local = project.child / ".sbx.toml"
    recorded = vm_metadata.load_vm("local-vm")
    assert recorded["project_root"] == str(project.child)
    assert recorded["config_path"] == str(local)
    assert local.is_file()
    assert project.source.read_text() == original
    config = cli.load_config()
    assert config["sbx"]["name"] == "local-vm"
    assert config["sbx"]["project_path"] == str(project.parent)
    assert config["sbx"]["mount"] == [f"{project.parent}:/workspace"]
    assert config["sbx"]["memory"] == 2048
    assert config["sbx"]["runtime_install"] == []
    assert config["sbx"]["stop_on_exit"] is False
    assert cli.main(["run", "--no-attach"]) == 0
    assert vm_metadata.load_vm("local-vm")["project_root"] == str(project.child)
    assert len(project.creations) == 1


@pytest.mark.parametrize("action", ["run", "create", "recreate"])
@pytest.mark.parametrize("explicit_name", [False, True])
def test_ancestor_vm_conflict_is_rejected_before_side_effects(
    local_project, action, explicit_name, capsys
):
    project = local_project
    identity = {"project_root": str(project.parent), "config_path": str(project.source)}
    vm_metadata.claim_vm("parent-vm", identity, "agent", created=True)
    before = vm_metadata.load_vm("parent-vm")
    project.statuses["parent-vm"] = "running"
    flags = ["parent-vm"] if explicit_name else []
    arguments = [action, *flags, "--write-config"]
    if action == "recreate":
        arguments.append("--force")
    assert cli.main(arguments) == 2
    assert project.calls == []
    assert not (project.child / ".sbx.toml").exists()
    assert vm_metadata.load_vm("parent-vm") == before
    assert "different VM name" in capsys.readouterr().err
    assert cli._project_identity(SimpleNamespace(config=None), cli.load_config()) == identity


def test_cli_values_override_inherited_defaults_in_new_local_file(local_project):
    project = local_project
    assert (
        cli.main(
            [
                "run",
                "local-vm",
                "--write-config",
                "--no-attach",
                "--memory",
                "4096",
                "--project-path",
                ".",
                "--mount",
                ".:/local",
                "--env",
                "SBX_TEST_ABSENT",
            ]
        )
        == 0
    )
    config = cli.load_config()["sbx"]
    assert config["memory"] == 4096
    assert config["project_path"] == str(project.child)
    assert config["mount"] == [f"{project.child}:/local"]
    assert config["env"] == ["SBX_TEST_ABSENT"]


def test_existing_local_config_symlink_keeps_canonical_identity(local_project):
    project = local_project
    (project.child / ".sbx.toml").symlink_to(project.source)
    args = SimpleNamespace(config=None, write_config=True, name="parent-vm")
    assert cli._project_identity(args, cli.load_config()) == {
        "project_root": str(project.parent),
        "config_path": str(project.source),
    }


def test_existing_local_config_still_adds_only_missing_values(local_project):
    project = local_project
    local = project.child / ".sbx.toml"
    local.write_text('[sbx]\nname="local-vm"\nmemory=1024\n')
    assert (
        cli.main(["create", "local-vm", "--write-config", "--memory", "4096", "--cpus", "2"]) == 0
    )
    assert cli.load_config()["sbx"]["memory"] == 1024
    assert cli.load_config()["sbx"]["cpus"] == 2
    assert vm_metadata.load_vm("local-vm")["config_path"] == str(local)
