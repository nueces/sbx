import json
import multiprocessing
from types import SimpleNamespace

import pytest

from sbx import auth_ports, cli, doctor, guest_setup, network, session_state, vm_metadata
from sbx.runtime import ConfigError


@pytest.mark.parametrize(
    "agent,provider,ports",
    [
        ("claude", None, [53692]),
        ("codex", None, [1455]),
        ("pi", None, [1455, 53692]),
        ("opencode", None, [1455, 53692]),
        ("pi", "anthropic", [53692]),
        ("opencode", "openai", [1455]),
    ],
)
def test_auto_resolution(agent, provider, ports):
    enabled, forwards = auth_ports.resolve(
        SimpleNamespace(),
        {"sbx": {"auth_provider": provider}},
        agent,
    )
    assert enabled is None
    assert forwards == [(port, port) for port in ports]


@pytest.mark.parametrize("value,enabled", [(True, True), (False, False), ("none", False)])
def test_boolean_compatibility(value, enabled):
    actual, ports = auth_ports.resolve(SimpleNamespace(), {"sbx": {"auth_port": value}}, "pi")
    assert actual is enabled
    assert bool(ports) is enabled


def test_cli_precedence_and_numeric_defaults():
    config = {"sbx": {"auth_port": False, "auth_provider": "openai", "auth_guest_port": 7000}}
    args = SimpleNamespace(auth_port="auto", auth_provider="anthropic", host_port=8000)
    assert auth_ports.resolve(args, config, "pi") == (True, [(8000, 7000)])
    assert auth_ports.resolve(
        SimpleNamespace(), {"sbx": {"auth_port": "anthropic", "auth_host_port": 8000}}, "pi"
    ) == (True, [(8000, 53692)])


@pytest.mark.parametrize(
    "sbx",
    [
        {"auth_port": 1},
        {"auth_port": []},
        {"auth_port": "pi"},
        {"auth_provider": False},
        {"auth_provider": "unknown"},
        {"auth_host_port": True},
        {"auth_guest_port": "1455"},
        {"auth_guest_port": 0},
        {"auth_host_port": 65536},
        {"auth_port": "both", "auth_host_port": 1455},
    ],
)
def test_invalid_config(sbx):
    with pytest.raises(ConfigError):
        auth_ports.resolve(SimpleNamespace(), {"sbx": sbx}, "pi")


@pytest.mark.parametrize("agent,provider", [("claude", "openai"), ("codex", "anthropic")])
def test_fixed_agent_mismatch(agent, provider):
    with pytest.raises(ConfigError, match="incompatible"):
        auth_ports.resolve(SimpleNamespace(auth_provider=provider), {}, agent)


def test_cli_flags_and_installer_order():
    parser = cli.build_parser()
    for profile in auth_ports.PROFILES:
        args = parser.parse_args(["run", f"--auth-port={profile}"])
        assert args.auth_port == profile
    assert parser.parse_args(["run", "--auth-port"]).auth_port == "auto"
    assert parser.parse_args(["run"]).auth_port is None
    for tools in (["opencode", "claude"], ["claude", "opencode"]):
        config = {"sbx": {"runtime_install": tools, "agent": "opencode"}}
        assert cli._runtime_install(SimpleNamespace(), config) == tools
        assert cli._cfg_agent(config) == "opencode"
        assert auth_ports.resolve(SimpleNamespace(), config, "opencode")[1] == [
            (1455, 1455),
            (53692, 53692),
        ]


@pytest.fixture
def auth_session(monkeypatch, tmp_path, fake_guest_lifecycle):
    monkeypatch.setattr(vm_metadata, "SBX_VMS_DIR", tmp_path / "vms")
    vm_metadata.claim_vm(
        "vm1",
        {"project_root": str(tmp_path), "config_path": str(tmp_path / ".sbx.toml")},
        "agent",
        created=True,
    )
    events = []
    for name in (
        "install_runtime_tools",
        "ensure_kvm_access",
        "install_git_config",
        "ensure_rootless_docker",
    ):
        monkeypatch.setattr(guest_setup, name, lambda *a, _name=name, **kw: events.append(_name))
    monkeypatch.setattr(cli, "_sync_forwarded_env_or_error", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_get_existing_vm_status", lambda name: "running")
    original_mark = vm_metadata.mark_provisioned

    def mark(name):
        original_mark(name)
        events.append("mark")

    monkeypatch.setattr(vm_metadata, "mark_provisioned", mark)
    created = [{"pid": 1234, "host_port": 1455, "guest_port": 1455}]

    def expose(name, forwards):
        assert vm_metadata.load_vm(name)["provisioned"]
        events.append("open")
        return 0, created

    monkeypatch.setattr(network, "expose_auth_ports", expose)
    monkeypatch.setattr(network, "close_session_auth_ports", lambda *a: events.append("close"))
    monkeypatch.setattr(guest_setup, "attach", lambda *a, **kw: events.append("attach") or 0)
    monkeypatch.setattr(cli, "_stop_vm_if_last_session", lambda *a, **kw: None)

    def run(**kwargs):
        options = dict(
            vm_name="vm1",
            started_now=False,
            command="pi",
            attach=True,
            run_user="agent",
            auth_port=None,
            auth_forwards=[(1455, 1455), (53692, 53692)],
            stop_on_exit=False,
        )
        options.update(kwargs)
        return cli._post_start_actions(**options)

    return SimpleNamespace(run=run, events=events, created=created)


@pytest.mark.parametrize(
    "was_provisioned,provision,explicit,opens",
    [
        (False, True, None, True),
        (True, False, None, False),
        (True, True, None, False),
        (True, False, True, True),
        (False, True, False, False),
    ],
)
def test_session_policy(auth_session, was_provisioned, provision, explicit, opens):
    if was_provisioned:
        vm_metadata.mark_provisioned("vm1")
        auth_session.events.clear()
    assert (
        auth_session.run(provision=provision, was_provisioned=was_provisioned, auth_port=explicit)
        == 0
    )
    events = auth_session.events
    assert ("open" in events) is opens
    assert ("close" in events) is opens
    if opens:
        assert events.index("open") < events.index("attach") < events.index("close")
    if provision and opens:
        assert events.index("install_runtime_tools") < events.index("mark") < events.index("open")


@pytest.mark.parametrize("kwargs", [{"attach": False}, {"diagnostic_root": True}])
def test_no_headless_or_root_tunnels(auth_session, kwargs):
    vm_metadata.mark_provisioned("vm1")
    assert auth_session.run(auth_port=True, **kwargs) == 0
    assert "open" not in auth_session.events


def test_headless_first_provision_does_not_defer_forwarding(auth_session):
    assert auth_session.run(provision=True, attach=False) == 0
    assert auth_session.run(was_provisioned=True) == 0
    assert "open" not in auth_session.events


def test_failed_provision_never_opens(auth_session, monkeypatch):
    def fail(*a, **kw):
        raise ConfigError("install failed")

    monkeypatch.setattr(guest_setup, "install_runtime_tools", fail)
    assert auth_session.run(provision=True) == 1
    assert not {"mark", "open", "attach"}.intersection(auth_session.events)


@pytest.mark.parametrize("failure", ["nonzero", "error", "interrupt", "register"])
def test_failed_attach_or_startup_cleans_session_tunnels(auth_session, monkeypatch, failure):
    def attach(*a, **kw):
        if failure == "error":
            raise ConfigError("attach failed")
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 7

    monkeypatch.setattr(guest_setup, "attach", attach)
    if failure == "register":
        monkeypatch.setattr(
            session_state,
            "register_session",
            lambda *a: (_ for _ in ()).throw(ConfigError("registration failed")),
        )
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            auth_session.run(provision=True)
    else:
        assert auth_session.run(provision=True) != 0
    assert auth_session.events[-1] == "close"
    assert vm_metadata.load_vm("vm1")["provisioned"] is True


def test_tunnel_failure_keeps_provisioning_success(auth_session, monkeypatch, capsys):
    monkeypatch.setattr(network, "expose_auth_ports", lambda *a: (9, []))
    assert auth_session.run(provision=True) == 9
    assert vm_metadata.load_vm("vm1")["provisioned"] is True
    assert "--auth-port" in capsys.readouterr().err
    assert "attach" not in auth_session.events


@pytest.fixture
def fake_auth_ssh(monkeypatch):
    state = SimpleNamespace(next_pid=100, killed=[], opened=[], listening=set(), fail=None)
    monkeypatch.setattr(network, "pid_is_alive", lambda pid: pid not in state.killed)
    monkeypatch.setattr(
        network, "_localhost_port_is_listening", lambda port: port in state.listening
    )
    monkeypatch.setattr(network, "ssh_command", lambda vm: ["ssh", vm])
    monkeypatch.setattr(network.time, "sleep", lambda _: None)

    class Process:
        stderr = None
        returncode = 9

        def __init__(self, argv, **kwargs):
            state.next_pid += 1
            self.pid = state.next_pid
            forward = argv[argv.index("-L") + 1]
            self.host = int(forward.split(":")[1])
            state.opened.append((self.pid, argv))
            if state.fail != self.host:
                state.listening.add(self.host)

        def poll(self):
            return 9 if state.fail == self.host else None

    def kill(pid, sig):
        state.killed.append(pid)
        for process_pid, argv in state.opened:
            if process_pid == pid:
                state.listening.discard(int(argv[argv.index("-L") + 1].split(":")[1]))

    monkeypatch.setattr(network.subprocess, "Popen", Process)
    monkeypatch.setattr(network.os, "killpg", kill)
    return state


def test_two_tunnels_reuse_and_session_ownership(fake_auth_ssh):
    assert network.expose_auth_port("vm1", 1455, 1455) == 0  # persistent manual tunnel
    manual = network._tracked_auth_tunnel("vm1", 1455)
    rc, created = network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)])
    assert rc == 0 and len(created) == 1
    assert len(network._tracked_auth_tunnels("vm1")) == 2
    network.close_session_auth_ports("vm1", created)
    assert network._tracked_auth_tunnels("vm1") == [manual]
    assert network.close_auth_ports("vm1")
    assert network._load_tunnels() == {}


def test_session_cleanup_removes_its_already_dead_record(fake_auth_ssh):
    _, created = network.expose_auth_ports("vm1", [(1455, 1455)])
    fake_auth_ssh.killed.append(created[0]["pid"])
    network.close_session_auth_ports("vm1", created)
    assert network._load_tunnels() == {}
    assert fake_auth_ssh.killed == [created[0]["pid"]]


def test_replacement_is_targeted_and_old_cleanup_is_safe(fake_auth_ssh):
    _, old = network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)])
    assert network.expose_auth_port("vm2", 1455, 1455, replace=True) == 0
    network.close_session_auth_ports("vm1", old[:1])
    assert len(network._tracked_auth_tunnels("vm1")) == 1
    assert len(network._tracked_auth_tunnels("vm2")) == 1
    # Replacement within the same VM must also survive the original session's cleanup.
    assert network.expose_auth_port("vm1", 53692, 6000, replace=True) == 0
    network.close_session_auth_ports("vm1", old)
    assert network._tracked_auth_tunnel("vm1", 53692)["guest_port"] == 6000


def test_conflicts_do_not_overwrite_or_kill(fake_auth_ssh, capsys):
    network.expose_auth_port("vm1", 1455, 1455)
    assert network.expose_auth_ports("vm2", [(1455, 1455)]) == (0, [])
    fake_auth_ssh.listening.add(53692)
    assert network.expose_auth_ports("vm2", [(53692, 53692)], replace=True) == (0, [])
    assert fake_auth_ssh.killed == []
    assert len(network._tracked_auth_tunnels("vm1")) == 1
    assert "not tracked" in capsys.readouterr().err


def test_partial_failure_rolls_back_only_new_tunnels(fake_auth_ssh):
    fake_auth_ssh.fail = 53692
    assert network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)]) == (9, [])
    assert network._tracked_auth_tunnels("vm1") == []
    assert {pid for pid, _ in fake_auth_ssh.opened} == set(fake_auth_ssh.killed)
    fake_auth_ssh.fail = None
    network.expose_auth_port("vm1", 1455, 1455)
    manual = network._tracked_auth_tunnel("vm1", 1455)
    fake_auth_ssh.fail = 53692
    assert network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)]) == (9, [])
    assert network._tracked_auth_tunnels("vm1") == [manual]


def test_interrupted_dual_startup_closes_new_siblings(fake_auth_ssh, monkeypatch):
    original = network._expose_auth_port

    def expose(vm, host, guest, **kwargs):
        if host == 53692:
            raise KeyboardInterrupt
        return original(vm, host, guest, **kwargs)

    monkeypatch.setattr(network, "_expose_auth_port", expose)
    with pytest.raises(KeyboardInterrupt):
        network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)])
    assert network._tracked_auth_tunnels("vm1") == []
    assert fake_auth_ssh.killed == [fake_auth_ssh.opened[0][0]]


def test_state_write_failure_terminates_unrecorded_ssh(fake_auth_ssh, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("state write failed")

    monkeypatch.setattr(network, "_record_auth_tunnel", fail)
    with pytest.raises(OSError, match="state write failed"):
        network.expose_auth_ports("vm1", [(1455, 1455)])
    assert fake_auth_ssh.killed == [fake_auth_ssh.opened[0][0]]
    assert not fake_auth_ssh.listening


def test_legacy_migration_and_doctor(fake_auth_ssh):
    legacy = {"pid": 88, "host_port": 1455, "guest_port": 1455}
    network._save_tunnels({"vm1": {"auth_port": legacy}, "bad": []})
    assert network._tracked_auth_tunnel("vm1", 1455) == legacy
    assert "auth_port" in json.loads(network.TUNNELS_FILE.read_text())["vm1"]
    network.expose_auth_port("vm1", 53692, 53692)
    raw = json.loads(network.TUNNELS_FILE.read_text())
    assert "auth_port" not in raw["vm1"]
    assert len(raw["vm1"]["auth_ports"]) == 2
    fake_auth_ssh.killed.append(88)
    doctor.doctor_tunnels(fix=False)
    assert len(network._load_tunnels()["vm1"]["auth_ports"]) == 2
    doctor.doctor_tunnels(fix=True)
    assert len(network._tracked_auth_tunnels("vm1")) == 1


def test_malformed_tunnels_are_not_signalled(fake_auth_ssh):
    network._save_tunnels(
        {
            "vm1": {
                "auth_ports": {
                    "1455": {"pid": -1},
                    "53692": None,
                    "bad": "invalid",
                }
            }
        }
    )
    assert network.close_auth_ports("vm1") is False
    assert fake_auth_ssh.killed == []
    assert network._load_tunnels() == {}


def test_status_lists_both_tunnels(fake_auth_ssh, monkeypatch, capsys):
    network.expose_auth_ports("vm1", [(1455, 1455), (53692, 53692)])
    vm = dict(name="vm1", status="running", backend="qemu", ip_address="10.0.2.15", ssh_port=2200)
    monkeypatch.setattr(
        network,
        "run_smolvm_capture",
        lambda *a: SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"vm": vm}})),
    )
    assert network.cmd_status(SimpleNamespace(name="vm1", host_port=1455, json=True)) == 0
    status = json.loads(capsys.readouterr().out)["auth_callback"]
    assert status["status"] == "active"
    assert {t["host_port"] for t in status["tunnels"]} == {1455, 53692}
    assert "53692" in status["detail"]


@pytest.mark.parametrize(
    "profile,provider,host,expected",
    [
        ("anthropic", None, None, [(53692, 53692)]),
        ("auto", "openai", 8000, [(8000, 1455)]),
        ("none", None, None, []),
    ],
)
def test_manual_command_uses_cli_over_config(profile, provider, host, expected, monkeypatch):
    captured = []
    monkeypatch.setattr(
        network,
        "expose_auth_ports",
        lambda name, ports, **kw: captured.append((name, ports, kw)) or (0, []),
    )
    args = SimpleNamespace(
        name="vm1",
        auth_port=profile,
        auth_provider=provider,
        host_port=host,
        replace=True,
        config_data={"sbx": {"auth_port": "openai"}},
    )
    assert network.cmd_auth_port(args) == 0
    assert captured == [("vm1", expected, {"replace": True})]


def test_auth_options_are_retained_in_written_config():
    args = SimpleNamespace(auth_port="both", auth_provider="openai")
    result = cli._project_config_values(args, {}, vm_name="vm1", agent="pi")
    assert result["auth_port"] == "both"
    assert result["auth_provider"] == "openai"


def test_all_shell_completions_include_new_values():
    for shell in ("bash", "zsh", "fish"):
        completion = cli.completion_script(shell)
        for value in (
            "opencode",
            "claude",
            "auth-port",
            "auth-provider",
            "anthropic",
            "openai",
            "both",
            "none",
        ):
            assert value in completion


def _record_in_child(vm, port):
    with network.tunnel_lock():
        network._record_auth_tunnel(vm, pid=100, host_port=port, guest_port=port)


def test_concurrent_state_updates_are_serialized():
    context = multiprocessing.get_context("fork")
    children = [
        context.Process(target=_record_in_child, args=("vm1", port)) for port in (1455, 53692)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=10)
        assert child.exitcode == 0
    assert set(network._load_tunnels()["vm1"]["auth_ports"]) == {"1455", "53692"}


@pytest.mark.parametrize("command", ["run", "network"])
def test_numeric_both_rejected_before_vm_work(command, monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[sbx]\nauth_port="both"\nauth_host_port=1455\n')
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", ())
    monkeypatch.chdir(tmp_path)
    argv = ["run", "vm1"] if command == "run" else ["network", "auth-port", "vm1"]
    assert cli.main(["--config", str(config), *argv]) == 2
