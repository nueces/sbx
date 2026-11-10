import tomllib
from contextlib import contextmanager
from itertools import product
from types import SimpleNamespace

import pytest

from sbx import bootstrap, cli, guest_setup, vm_metadata

_POST_START_ACTIONS = cli._post_start_actions
_BOOTSTRAP_SESSION = guest_setup.bootstrap_session


@pytest.fixture
def host_env(monkeypatch):
    monkeypatch.setenv("SOURCE", "host-secret")
    monkeypatch.setenv("EMPTY", "")
    monkeypatch.setenv("X", "host-x")
    monkeypatch.delenv("MISSING", raising=False)


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([], {}),
        (["SOURCE"], {"SOURCE": "host-secret"}),
        (["MISSING"], {"MISSING": None}),
        (["EMPTY"], {"EMPTY": ""}),
        (["X=1"], {"X": "1"}),
        (["X="], {"X": ""}),
        (["X=${SOURCE}"], {"X": "host-secret"}),
        (["X=${MISSING}"], {"X": None}),
        (["X=${EMPTY}"], {"X": ""}),
        (["X=${X}"], {"X": "host-x"}),
        (["X=a=b"], {"X": "a=b"}),
        (["X= value "], {"X": " value "}),
        (["X='quoted'\\path"], {"X": "'quoted'\\path"}),
        (["X=$HOME"], {"X": "$HOME"}),
        (["X=$(command)"], {"X": "$(command)"}),
        (["X=op://vault/item"], {"X": "op://vault/item"}),
        (["X=a{b}"], {"X": "a{b}"}),
        (["X=first\nsecond\rthird"], {"X": "first\nsecond\rthird"}),
        (["X=${SOURCE}", "Y=${SOURCE}"], {"X": "host-secret", "Y": "host-secret"}),
        (["X=assigned", "Y=${X}"], {"X": "assigned", "Y": "host-x"}),
        (["X=upper", "x=lower"], {"X": "upper", "x": "lower"}),
    ],
)
def test_resolve_entries(host_env, entries, expected):
    guest_setup.validate_env_entries(entries)
    assert guest_setup.resolve_env_entries(entries) == expected


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "=value",
        "1BAD=value",
        "BAD-NAME=value",
        " X=value",
        "X =value",
        "X=${}",
        "X=${BAD-NAME}",
        "X=${X:-default}",
        "X=${X:?required}",
        "X=${X}${Y}",
        "X=prefix${X}",
        "X=${X}suffix",
        "X=${X",
        "X=${{X}}",
        "X=before\0after",
    ],
)
def test_invalid_entries_have_safe_indexed_errors(entry):
    with pytest.raises(cli.ConfigError) as error:
        guest_setup.validate_env_entries(["VALID=ok", entry])
    assert str(error.value).startswith("[sbx].env entry 2:")
    assert "before" not in str(error.value)
    assert "value" not in str(error.value) or "NUL" in str(error.value)


@pytest.mark.parametrize(
    "first, second", list(product(["X", "X=literal", "X=${SOURCE}"], repeat=2))
)
def test_duplicate_destinations_are_rejected(first, second):
    with pytest.raises(cli.ConfigError, match=r"entry 2: duplicate destination"):
        guest_setup.validate_env_entries([first, second])


def test_validation_does_not_resolve_host_and_expansion_is_not_recursive(monkeypatch):
    monkeypatch.setattr(guest_setup.os, "environ", {})
    guest_setup.validate_env_entries(["X=${MISSING}", "Y=first\nsecond"])
    monkeypatch.setattr(guest_setup.os, "environ", {"SOURCE": "${MISSING}"})
    assert guest_setup.resolve_env_entries(["X=${SOURCE}"]) == {"X": "${MISSING}"}
    monkeypatch.setattr(guest_setup.os, "environ", {"SOURCE": "before\0after"})
    with pytest.raises(cli.ConfigError, match="entry 1: NUL") as error:
        guest_setup.resolve_env_entries(["X=${SOURCE}"])
    assert "before" not in str(error.value)


@pytest.fixture
def project(tmp_path, monkeypatch, fake_guest_lifecycle):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", ())
    monkeypatch.setattr(cli.runtime, "DEBUG", False)
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "metadata")
    monkeypatch.setattr(guest_setup, "host_username", lambda: "root")
    monkeypatch.setattr(guest_setup, "host_git_config", lambda *args: None)
    monkeypatch.setattr(guest_setup, "credential_free_env", lambda *args, **kwargs: {})
    state = SimpleNamespace(config=tmp_path / ".sbx.toml", calls=[], posts=[], statuses={})

    def configure(entries, extra=""):
        state.config.write_text(
            '[sbx]\nname="vm1"\nrun_user="root"\nauth_port=false\n'
            + "env="
            + cli._toml_value(entries)
            + "\n"
            + extra
        )

    def create(agent, **kwargs):
        state.calls.append("create")
        kwargs["before_provision"]("vm1")
        state.statuses["vm1"] = "running"
        return SimpleNamespace(vm_id="vm1", close=lambda: None)

    def delete(name, **kwargs):
        state.calls.append("delete")
        state.statuses.pop(name, None)
        vm_metadata.remove_vm(name)
        return 0

    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: state.statuses.get(name))
    monkeypatch.setattr(cli, "_start_existing_vm_if_needed", lambda *a, **kw: 0)
    monkeypatch.setattr(cli, "_sync_existing_vm_start_config", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_warn_running_mount_drift", lambda *a, **kw: None)
    monkeypatch.setattr(cli.smolvm_preset, "create_preset", create)
    monkeypatch.setattr(cli, "_delete_vm", delete)
    monkeypatch.setattr(cli, "_post_start_actions", lambda **kw: state.posts.append(kw) or 0)
    state.configure = configure
    configure([])
    return state


@pytest.mark.parametrize("entries", [["X=${BROKEN"], ["X=one", "X=two"], ["X=secret\0"], 1, [1]])
@pytest.mark.parametrize("action", ["run", "create", "recreate", "shell", "remove", "doctor"])
def test_invalid_config_fails_at_load_even_with_overrides(project, entries, action, capsys):
    project.configure(entries)
    flags = ["--env", "SOURCE"] if action in {"run", "create", "recreate"} else []
    if action in {"recreate", "remove"}:
        flags.append("--force")
    assert cli.main(["--debug", action, *flags]) == 2
    error = capsys.readouterr().err
    assert str(project.config) in error and "[sbx].env" in error
    if isinstance(entries, list) and entries and isinstance(entries[0], str):
        assert "entry " in error
    assert "BROKEN" not in error and "secret" not in error
    assert project.calls == project.posts == []


def test_each_config_layer_is_validated_before_merge(project, monkeypatch, capsys):
    lower = project.config.parent / "global.toml"
    lower.write_text('[sbx]\nenv=["BAD-NAME=hidden-secret"]\n')
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (lower,))
    project.configure(["X=valid"])
    assert cli.main(["--debug", "create", "--env", "SOURCE"]) == 2
    error = capsys.readouterr().err
    assert str(lower) in error and "entry 1" in error and "hidden-secret" not in error
    lower.write_text('[sbx]\nenv=["X=lower"]\n')
    assert cli.load_config()["sbx"]["env"] == ["X=valid"]


@pytest.mark.parametrize("entries", ["X=${MISSING}", ["X=${MISSING}"], []])
def test_load_validates_syntax_without_resolving_host(project, monkeypatch, entries):
    project.configure(entries)
    monkeypatch.setattr(
        guest_setup, "resolve_env_entries", lambda *args: pytest.fail("must not resolve at load")
    )
    assert cli.load_config()["sbx"]["env"] == entries


@pytest.mark.parametrize("action", ["run", "create", "recreate", "shell"])
def test_commands_resolve_configured_values(project, host_env, action):
    project.configure(["X=literal", "TOKEN=${SOURCE}", "EMPTY=", "MISSING"])
    flags = ["--force"] if action == "recreate" else []
    if action == "shell":
        project.statuses["vm1"] = "running"
        vm_metadata.claim_vm(
            "vm1",
            cli._project_identity(SimpleNamespace(config=None), cli.load_config()),
            "root",
            created=True,
        )
    assert cli.main([action, *flags]) == 0
    assert project.posts[-1]["env_values"] == {
        "X": "literal",
        "TOKEN": "host-secret",
        "EMPTY": "",
        "MISSING": None,
    }


def test_cli_selection_replaces_config_and_allows_repeated_names(project, host_env):
    project.configure(["X=literal", "TOKEN=${SOURCE}"])
    assert cli.main(["run", "--env", "EMPTY", "--env", "EMPTY"]) == 0
    assert project.posts[-1]["env_values"] == {"EMPTY": ""}


def test_preset_and_post_setup_share_snapshot_but_next_invocation_refreshes(
    project,
    host_env,
    monkeypatch,
):
    project.configure(["TOKEN=${SOURCE}"])
    create = cli.smolvm_preset.create_preset

    def change_host_during_creation(*args, **kwargs):
        result = create(*args, **kwargs)
        monkeypatch.setenv("SOURCE", "changed")
        return result

    monkeypatch.setattr(cli.smolvm_preset, "create_preset", change_host_during_creation)
    assert cli.main(["create"]) == 0
    assert project.posts[-1]["env_values"] == {"TOKEN": "host-secret"}
    assert cli.main(["run", "--no-attach"]) == 0
    assert project.posts[-1]["env_values"] == {"TOKEN": "changed"}


@pytest.mark.parametrize("flag", [["--env", "X=CLI_SECRET"], ["--env=X=${CLI_SECRET}"]])
def test_rejected_cli_assignments_are_not_logged(project, flag, capsys):
    assert cli.main(["--debug", "create", *flag]) == 2
    error = capsys.readouterr().err
    assert "--env entry 1" in error and "CLI_SECRET" not in error
    assert project.posts == []


def test_debug_selection_logs_names_not_literals_or_alias_values(project, host_env, capsys, caplog):
    project.configure(["X=LITERAL_SECRET", "TOKEN=${SOURCE}"])
    assert cli.main(["--debug", "create"]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert "env_names=['X', 'TOKEN']" in output
    assert "LITERAL_SECRET" not in output and "host-secret" not in output


def test_recreate_reports_creation_failure_without_rollback(project, monkeypatch, capsys):
    project.configure(["X=LITERAL_SECRET"])
    project.statuses["vm1"] = "running"

    def fail(*args, **kwargs):
        project.calls.append("create")
        raise RuntimeError("image unavailable LITERAL_SECRET; correct the image configuration")

    monkeypatch.setattr(cli.smolvm_preset, "create_preset", fail)
    assert cli.main(["--debug", "recreate", "--force"]) == 1
    assert project.calls == ["delete", "create"]
    error = capsys.readouterr().err
    assert "failed to create preset" in error and "correct the image configuration" in error
    assert "LITERAL_SECRET" not in error
    assert "vm1" not in project.statuses


@pytest.mark.parametrize("failure", ["sync", "boot", "provision", "readiness"])
@pytest.mark.parametrize("error_type", [cli.ConfigError, RuntimeError])
def test_failure_boundaries_redact_resolved_values(
    project,
    monkeypatch,
    capsys,
    failure,
    error_type,
):
    vm_metadata.claim_vm(
        "vm1", {"project_root": "/p", "config_path": "/p/.sbx.toml"}, "root", created=True
    )

    def fail(*args, **kwargs):
        raise error_type("LITERAL_SECRET ALIAS_SECRET detail")

    attribute = {
        "sync": "sync_forwarded_env",
        "boot": "prepare_boot",
        "provision": "prepare_boot",
        "readiness": "bootstrap_session",
    }[failure]
    monkeypatch.setattr(guest_setup, attribute, fail)
    assert (
        _POST_START_ACTIONS(
            vm_name="vm1",
            started_now=failure != "sync",
            command=None,
            attach=False,
            run_user="root",
            auth_port=False,
            stop_on_exit=False,
            require_provisioned=False,
            provision=failure == "provision",
            env_values={"X": "LITERAL_SECRET", "TOKEN": "ALIAS_SECRET"},
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "LITERAL_SECRET" not in error and "ALIAS_SECRET" not in error
    assert "detail" in error and "[redacted]" in error


@pytest.mark.parametrize("error_type", [cli.ConfigError, RuntimeError])
def test_local_image_creation_failure_is_redacted(project, monkeypatch, capsys, error_type):
    project.configure(["X=LITERAL_SECRET"], 'image="/synthetic/image"\n')
    monkeypatch.setattr(cli.lifecycle_warnings, "local_image_manifest", lambda path: {})

    def fail(**kwargs):
        raise error_type("unavailable image LITERAL_SECRET")

    monkeypatch.setattr(cli, "_start_local_image", fail)
    assert cli.main(["--debug", "create"]) == (2 if error_type is cli.ConfigError else 1)
    error = capsys.readouterr().err
    assert "unavailable image" in error and "LITERAL_SECRET" not in error


@pytest.fixture
def managed_guest(monkeypatch):
    stored = {"UNRELATED": "keep", "MISSING": "stale"}
    calls = []

    def set_values(values):
        calls.append(("set", values))
        stored.update(values)

    def unset_values(names):
        calls.append(("unset", names))
        for name in names:
            stored.pop(name, None)

    vm = SimpleNamespace(
        info=SimpleNamespace(config=SimpleNamespace(workspace_mounts=[], env_vars={})),
        wait_for_ready=lambda **kw: None,
        set_env_vars=set_values,
        unset_env_vars=unset_values,
        run=lambda *a, **kw: SimpleNamespace(exit_code=0),
    )

    @contextmanager
    def open_guest(vm_id):
        yield vm

    monkeypatch.setattr(guest_setup, "open_guest", open_guest)
    return SimpleNamespace(vm=vm, stored=stored, calls=calls)


def test_sync_uses_snapshot_sets_empty_unsets_destination_and_preserves_data(
    host_env,
    monkeypatch,
    managed_guest,
):
    values = guest_setup.resolve_env_entries(
        [
            "X=literal",
            "TOKEN=${SOURCE}",
            "EMPTY=",
            "MISSING",
            "LINES=first\nsecond",
        ]
    )
    monkeypatch.setenv("SOURCE", "changed")
    monkeypatch.setenv("MISSING", "now-present")
    guest_setup.sync_forwarded_env("vm1", values)
    assert managed_guest.calls == [
        ("set", {"X": "literal", "TOKEN": "host-secret", "EMPTY": "", "LINES": "first\nsecond"}),
        ("unset", ["MISSING"]),
    ]
    assert managed_guest.stored == {
        "UNRELATED": "keep",
        "X": "literal",
        "TOKEN": "host-secret",
        "EMPTY": "",
        "LINES": "first\nsecond",
    }
    guest_setup.sync_forwarded_env("vm1", {})
    assert len(managed_guest.calls) == 2
    guest_setup.sync_forwarded_env(
        "vm1", guest_setup.resolve_env_entries(["TOKEN=${SOURCE}", "MISSING"])
    )
    assert managed_guest.stored["TOKEN"] == "changed"
    assert managed_guest.stored["MISSING"] == "now-present"
    assert managed_guest.stored["X"] == "literal"  # Deselection does not unset it.


def test_boot_reapplies_vm_config_before_selected_overrides(managed_guest, monkeypatch):
    managed_guest.vm.info.config.env_vars = {"X": "configured", "MISSING": "stale"}
    for name in ("prepare_run_user", "set_hostname", "sync_guest_clock", "mount_workspaces"):
        monkeypatch.setattr(guest_setup, name, lambda *a, **kw: None)
    guest_setup.prepare_boot(
        "vm1",
        "root",
        {"X": "literal", "MISSING": None},
        guest=SimpleNamespace(vm=managed_guest.vm),
    )
    assert managed_guest.calls == [
        ("set", {"X": "configured", "MISSING": "stale"}),
        ("set", {"X": "literal"}),
        ("unset", ["MISSING"]),
    ]
    assert managed_guest.stored == {"X": "literal", "UNRELATED": "keep"}


@pytest.mark.parametrize("operation", ["set_env_vars", "unset_env_vars"])
def test_actual_sync_api_failures_are_redacted(managed_guest, monkeypatch, capsys, operation):
    def fail(*args):
        raise RuntimeError("LITERAL_SECRET ALIAS_SECRET transport failure")

    monkeypatch.setattr(managed_guest.vm, operation, fail)
    values = {"X": "LITERAL_SECRET", "TOKEN": "ALIAS_SECRET", "MISSING": None}
    assert cli._sync_forwarded_env_or_error("vm1", values) is False
    message = capsys.readouterr().err
    assert "transport failure" in message and "[redacted]" in message
    assert "LITERAL_SECRET" not in message and "ALIAS_SECRET" not in message
    if operation == "unset_env_vars":
        assert managed_guest.stored["X"] == "LITERAL_SECRET"  # No transactional rollback.


def test_bootstrap_registers_values_before_first_phase(monkeypatch, managed_guest, capsys):
    def phase(guest, name):
        assert guest.env_names == ["X", "TOKEN", "EMPTY"]
        assert guest.sensitive_values == {"SECRET-long", "SECRET"}
        message = guest.sanitized("SECRET-long SECRET detail")
        assert message == "[redacted] [redacted] detail"
        raise cli.ConfigError(message)

    monkeypatch.setattr(bootstrap.GuestBootstrap, "phase", phase)
    with (
        pytest.raises(cli.ConfigError, match="redacted"),
        _BOOTSTRAP_SESSION(
            "vm1",
            "root",
            env_values={"X": "SECRET-long", "TOKEN": "SECRET", "EMPTY": ""},
        ),
    ):
        pytest.fail("first phase should fail")
    assert "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", 'quotes"\\path', "a\nb\rc\td", "\b\f\x01\x7f", "😀"])
def test_toml_string_serialization_round_trips(value):
    assert tomllib.loads("value=" + cli._toml_value(value))["value"] == value


def test_config_writer_keeps_references_and_raw_literals(project, host_env):
    entries = ["X=${SOURCE}", "EMPTY=", "LINES=first\nsecond", 'TEXT=a"\\b', "CTRL=\x01\x7f"]
    path = project.config.parent / "generated.toml"
    cli._write_project_config_values(path, {"env": entries})
    assert tomllib.loads(path.read_text())["sbx"]["env"] == entries
    assert "host-secret" not in path.read_text()
    assert cli._write_project_config_values(path, {"env": ["X=replacement"]}) == []
    assert tomllib.loads(path.read_text())["sbx"]["env"] == entries
