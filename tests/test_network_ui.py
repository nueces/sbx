import json
import os
import subprocess
import webbrowser
from pathlib import Path

import pytest

from sbx import cli, gateway, network

TOKEN = "ui-secret-marker"
URL = f"http://127.0.0.1:12345/?token={TOKEN}"


@pytest.fixture
def ui_state(tmp_path, monkeypatch):
    config = gateway.parse_config({"network": {"mitmweb": True}}, platform="linux")
    directory = gateway.create_state(config, "vm", "current", root=tmp_path)
    gateway.update_state(directory, status="ready", pids={"mediator": 123})
    gateway._atomic_json(directory, "web.json", {"port": 12345})
    gateway._atomic_json(directory, "web-auth.json", {"url": URL})
    monkeypatch.setattr(gateway, "GATEWAY_STATE_DIR", tmp_path)
    calls = []

    def control(vm_id, operation, **kwargs):
        calls.append((vm_id, operation))
        return {
            "generation": "current",
            "status": "ready",
            "counters": {},
            "last_failure": None,
            "config_digest": config.digest("vm", "current"),
        }

    monkeypatch.setattr(gateway, "control_request", control)
    monkeypatch.setattr(cli, "load_config", lambda _: {})
    return directory, calls


def test_ui_returns_current_url_without_mutation_or_external_processes(
    ui_state, monkeypatch, capsys
):
    directory, calls = ui_state
    before = {path: path.read_bytes() for path in directory.parent.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("UI lookup must not launch, probe, or mutate anything")

    monkeypatch.setattr(gateway, "_atomic_json", forbidden)
    monkeypatch.setattr(gateway, "GatewayController", forbidden)
    monkeypatch.setattr(network, "run_smolvm_capture", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    monkeypatch.setattr(gateway.socket, "socket", forbidden)
    assert cli.main(["network", "ui", "vm"]) == 0
    assert capsys.readouterr().out == URL + "\n"
    assert calls == [("vm", "status"), ("vm", "status")]
    assert before == {
        path: path.read_bytes() for path in directory.parent.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("debug", [False, True])
def test_ui_command_resolves_name_and_prints_only_url(
    ui_state, monkeypatch, capsys, explicit, debug
):
    monkeypatch.setattr(
        cli, "load_config", lambda _: {"sbx": {"name": "other" if explicit else "vm"}}
    )
    # No requested [network] setting: the running gateway is authoritative.
    args = [*(["--debug"] if debug else []), "network", "ui", *(["vm"] if explicit else [])]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == URL + "\n"
    assert TOKEN not in captured.err
    assert bool(captured.err) == debug


def test_ui_command_uses_explicit_config(ui_state, monkeypatch, capsys):
    paths = []

    def load(path):
        paths.append(path)
        return {"sbx": {"name": "vm"}, "network": {"mitmweb": False}}

    monkeypatch.setattr(cli, "load_config", load)
    assert cli.main(["--config", "demo.toml", "network", "ui"]) == 0
    assert paths == ["demo.toml"]
    assert capsys.readouterr().out == URL + "\n"


def test_ui_requires_name(ui_state, capsys):
    assert cli.main(["network", "ui"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requires a VM name" in captured.err


def test_ui_help_warns_that_url_is_a_credential(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["network", "ui", "--help"])
    assert exc.value.code == 0
    assert "token" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("status", ["stopped", "failed", "recovering", "draining"])
def test_ui_requires_live_ready_supervisor(ui_state, monkeypatch, capsys, status):
    directory, calls = ui_state
    if status == "stopped":
        gateway.update_state(directory, status="stopped")
    else:
        monkeypatch.setattr(
            gateway, "control_request", lambda *a, **k: {"generation": "current", "status": status}
        )
    assert cli.main(["network", "ui", "vm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err
    assert ("start" if status == "stopped" else "ready") in captured.err
    assert not calls


@pytest.mark.parametrize("kind", ["missing", "null", "unresponsive", "wrong-generation"])
def test_ui_never_trusts_retained_files_without_live_identity(ui_state, monkeypatch, capsys, kind):
    directory, _ = ui_state
    name = "vm"
    if kind == "missing":
        name = "unknown"
    elif kind == "null":
        gateway._write_current(directory.parent, None)
    elif kind == "unresponsive":

        def unavailable(*args, **kwargs):
            raise gateway.GatewayLifecycleError(TOKEN)

        monkeypatch.setattr(gateway, "control_request", unavailable)
    else:
        monkeypatch.setattr(
            gateway, "control_request", lambda *a, **k: {"generation": "old", "status": "ready"}
        )
    assert cli.main(["--debug", "network", "ui", name]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err


@pytest.mark.parametrize("filename", ["web.json", "web-auth.json"])
@pytest.mark.parametrize(
    "kind", ["missing", "malformed", "oversized", "permissions", "symlink", "owner"]
)
def test_ui_rejects_missing_or_unsafe_private_files(ui_state, monkeypatch, capsys, filename, kind):
    directory, _ = ui_state
    path = directory / filename
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text("{" + TOKEN)
    elif kind == "oversized":
        path.write_text(" " * 4097)
    elif kind == "permissions":
        path.chmod(0o644)
    elif kind == "symlink":
        target = directory / "elsewhere.json"
        target.write_bytes(path.read_bytes())
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        original = Path.lstat

        def wrong_owner(self):
            info = original(self)
            if self == path:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    assert cli.main(["--debug", "network", "ui", "vm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err
    if kind == "missing":
        assert "disabled or unavailable" in captured.err
        assert "mitmweb" in captured.err and "stop/start" in captured.err


@pytest.mark.parametrize("port", [True, 0, 65536, "12345", None])
def test_ui_rejects_invalid_advertised_port(ui_state, port):
    directory, _ = ui_state
    gateway._atomic_json(directory, "web.json", {"port": port})
    with pytest.raises(gateway.GatewayStateError):
        gateway.ui_url("vm")


@pytest.mark.parametrize(
    "url",
    [
        None,
        [],
        "https://127.0.0.1:12345/?token=x",
        "http://localhost:12345/?token=x",
        "http://127.0.0.2:12345/?token=x",
        "http://example.com:12345/?token=x",
        "http://127.0.0.1:54321/?token=x",
        "http://127.0.0.1:bad/?token=x",
        "http://127.0.0.1:12345/other?token=x",
        "http://127.0.0.1:12345/?token=",
        "http://127.0.0.1:12345/",
        "http://127.0.0.1:12345/?token=x&token=y",
        "http://127.0.0.1:12345/?token=x&other=y",
        "http://user@127.0.0.1:12345/?token=x",
        "http://127.0.0.1:12345/?token=x#fragment",
        "http://127.0.0.1:12345/?token=x\n",
        "\thttp://127.0.0.1:12345/?token=x",
        "http://127.0.0.1:12345/?token=\x1b[31m",
        "http://127.0.0.1:12345/?token=%0a",
    ],
)
def test_ui_rejects_invalid_urls_without_exposing_contents(ui_state, capsys, url):
    directory, _ = ui_state
    gateway._atomic_json(directory, "web-auth.json", {"url": url})
    assert cli.main(["--debug", "network", "ui", "vm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err
    assert "\x1b" not in captured.err
    assert "?token=" not in captured.err


@pytest.mark.parametrize("filename", ["web.json", "web-auth.json"])
def test_ui_rejects_unknown_file_fields(ui_state, filename):
    directory, _ = ui_state
    data = {"port": 12345} if filename == "web.json" else {"url": URL}
    gateway._atomic_json(directory, filename, {**data, "extra": TOKEN})
    with pytest.raises(gateway.GatewayStateError):
        gateway.ui_url("vm")


@pytest.mark.parametrize("kind", ["generation", "mediator", "port", "token", "recovering"])
def test_ui_detects_replacement_without_retry_and_next_invocation_can_succeed(
    ui_state, monkeypatch, capsys, kind
):
    directory, _ = ui_state
    calls = []
    current = [directory]
    new_url = [URL]

    def control(vm_id, operation, **kwargs):
        calls.append(operation)
        if len(calls) == 2:
            if kind == "generation":
                config = gateway.parse_config({"network": {"mitmweb": True}})
                current[0] = gateway.create_state(
                    config, "vm", "replacement", root=directory.parent.parent
                )
                gateway.update_state(current[0], status="ready", pids={"mediator": 456})
            elif kind == "mediator":
                gateway.update_state(directory, pids={"mediator": 456})
            elif kind == "port":
                new_url[0] = URL.replace(":12345/", ":54321/")
            elif kind == "token":
                new_url[0] = URL + "-replacement"
            else:
                return {"generation": "current", "status": "recovering"}
            gateway._atomic_json(
                current[0], "web.json", {"port": 54321 if kind == "port" else 12345}
            )
            gateway._atomic_json(current[0], "web-auth.json", {"url": new_url[0]})
        return {"generation": current[0].name, "status": "ready"}

    monkeypatch.setattr(gateway, "control_request", control)
    assert cli.main(["network", "ui", "vm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err
    assert calls == ["status", "status"]
    assert cli.main(["network", "ui", "vm"]) == 0
    assert capsys.readouterr().out == new_url[0] + "\n"


@pytest.mark.parametrize("filename", ["current.json", "state.json"])
@pytest.mark.parametrize("kind", ["malformed", "permissions", "symlink"])
def test_ui_rejects_unsafe_generation_metadata(ui_state, capsys, filename, kind):
    directory, _ = ui_state
    path = directory.parent / filename if filename == "current.json" else directory / filename
    if kind == "malformed":
        path.write_text("{" + TOKEN)
    elif kind == "permissions":
        path.chmod(0o644)
    else:
        path.unlink()
        path.symlink_to(directory / "web-auth.json")
    assert cli.main(["--debug", "network", "ui", "vm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and TOKEN not in captured.err


def test_ui_requires_recorded_mediator_and_valid_config(ui_state, monkeypatch, capsys):
    directory, _ = ui_state
    gateway.update_state(directory, pids={})
    assert cli.main(["network", "ui", "vm"]) == 1
    assert "not ready" in capsys.readouterr().err
    monkeypatch.setattr(cli, "load_config", lambda _: {"network": {"mitmweb": "true"}})
    assert cli.main(["network", "ui", "vm"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["status", "diagnostics"])
def test_status_and_diagnostics_never_read_ui_authentication(
    ui_state, monkeypatch, capsys, command
):
    original = gateway._read_json

    def read(path, **kwargs):
        assert path.name != "web-auth.json", "Only the explicit ui command may read authentication"
        return original(path, **kwargs)

    monkeypatch.setattr(gateway, "_read_json", read)
    monkeypatch.setattr(network, "_tracked_auth_tunnels", lambda _: [])
    monkeypatch.setattr(network, "_localhost_port_is_listening", lambda _: False)
    vm = {
        "name": "vm",
        "status": "running",
        "backend": "qemu",
        "ip_address": "192.0.2.2",
        "ssh_port": 2200,
    }
    monkeypatch.setattr(
        network,
        "run_smolvm_capture",
        lambda _: subprocess.CompletedProcess([], 0, json.dumps({"data": {"vm": vm}}), ""),
    )
    assert cli.main(["network", command, "vm"]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "web-auth.json" in captured.out


def test_ui_does_not_fall_back_to_older_generation(ui_state, capsys):
    directory, _ = ui_state
    config = gateway.parse_config({"network": {"mitmweb": True}})
    old = gateway.create_state(config, "vm", "old", root=directory.parent.parent)
    gateway._atomic_json(old, "web.json", {"port": 12345})
    gateway._atomic_json(old, "web-auth.json", {"url": URL})
    gateway._write_current(directory.parent, directory.name)
    (directory / "web-auth.json").unlink()
    assert cli.main(["network", "ui", "vm"]) == 1
    assert capsys.readouterr().out == ""
