import json
import os
import stat
from pathlib import Path

import pytest

from sbx import cli, gateway
from sbx.runtime import ConfigError


@pytest.fixture
def gateway_config() -> gateway.GatewayConfig:
    parsed = gateway.parse_config(
        {
            "network": {
                "default": "deny",
                "http": [
                    {
                        "id": "registry",
                        "host": "Registry.NPMJS.org.",
                        "ports": [443],
                        "methods": ["head", "GET"],
                        "paths": ["/package/"],
                        "hooks": ["package-audit"],
                    }
                ],
                "tcp": [{"host": "github.com", "ports": [22]}],
                "tls_passthrough": [{"host": "pinned.example.com", "ports": [443]}],
                "secret": [
                    {
                        "name": "NPM_TOKEN",
                        "provider": "env:NPM_TOKEN",
                        "hosts": ["registry.npmjs.org"],
                        "ports": [443],
                        "methods": ["GET"],
                        "path_prefix": "/package/",
                        "header": "Authorization: Bearer {placeholder}",
                    }
                ],
                "ingress": [{"listen": "127.0.0.1:0", "guest_port": 3000}],
                "quotas": {
                    "tcp_flows": 100,
                    "flows_per_destination": 20,
                    "dns_mappings": 500,
                    "open_files": 1000,
                },
                "reconnection": {"attempts": 2, "timeout_seconds": 5},
            },
            "sbx": {"port_forwards": ["8080:80"]},
        },
        platform="linux",
    )
    assert parsed is not None
    return parsed


def test_gateway_absence_preserves_disabled_behavior() -> None:
    assert gateway.parse_config({"sbx": {"port_forwards": ["3000"]}}) is None


def test_hardening_status_and_portable_artifact_resolution(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_text("data")
    artifact.chmod(0o600)
    assert gateway._resolve_artifact(
        "SBX_TEST_MISSING_OVERRIDE",
        (None, artifact),
        executable=False,
        label="test artifact",
    ) == artifact
    assert gateway._hardening_ready(
        "no_new_privs,seccomp,landlock-unavailable,rlimits", "linux"
    )
    assert not gateway._hardening_ready("seccomp", "linux")
    assert gateway._hardening_ready("unverified", "darwin")


def test_gateway_defaults_are_deny_and_bounded() -> None:
    config = gateway.parse_config({"network": {}}, platform="linux")

    assert config == gateway.GatewayConfig(
        rules=(),
        secrets=(),
        ingress=(),
        quotas=gateway.Quotas(),
        reconnection=gateway.Reconnection(),
        platform="linux",
    )


def test_gateway_full_config_normalizes_and_preserves_legacy_ingress(
    gateway_config: gateway.GatewayConfig,
) -> None:
    assert [rule.id for rule in gateway_config.rules] == ["registry", "tcp-1", "tls-1"]
    http = gateway_config.rules[0]
    assert http.host == "registry.npmjs.org"
    assert http.methods == ("GET", "HEAD")
    assert http.paths == ("/package",)
    assert gateway_config.ingress == (
        gateway.IngressRule("ingress-1", "127.0.0.1", 0, "192.0.2.2", 3000),
        gateway.IngressRule("legacy-1", "127.0.0.1", 8080, "192.0.2.2", 80, "sbx.port_forwards"),
    )
    worker = gateway_config.worker_config("vm-1", "generation-1")
    assert worker["digest"]
    assert "secrets" not in json.dumps(worker)
    assert worker["rules"][0]["host"] == "registry.npmjs.org"
    assert [rule["id"] for rule in worker["ingress"]] == ["ingress-1", "legacy-1"]
    assert worker["quotas"]["ingress_flows"] == 64


@pytest.mark.parametrize(
    ("network", "message"),
    [
        ({"unknown": True}, "unknown field"),
        ({"default": "allow"}, "must be 'deny'"),
        ({"http": [{"host": "*.com", "ports": [443]}]}, "wildcard is too broad"),
        (
            {
                "http": [{"host": "example.com", "ports": [443]}],
                "tls_passthrough": [{"host": "example.com", "ports": [443]}],
            },
            "conflict",
        ),
        (
            {"http": [{"host": "example.com", "ports": [443], "hooks": ["module:run"]}]},
            "unapproved hook",
        ),
        (
            {
                "http": [{"host": "example.com", "ports": [443]}],
                "secret": [
                    {
                        "name": "TOKEN",
                        "provider": "command:print-token",
                        "hosts": ["example.com"],
                        "ports": [443],
                        "header": "Authorization: {placeholder}",
                    }
                ],
            },
            "must use env",
        ),
        (
            {"tls_passthrough": [{"host": "*.example.com", "ports": [443]}]},
            "exact hostname",
        ),
        (
            {"tls_passthrough": [{"host": "example.com", "ports": [443], "hooks": []}]},
            "unknown field",
        ),
        (
            {"ingress": [{"listen": "example.com:1", "guest_port": 80}]},
            "IP address",
        ),
        ({"quotas": {"tcp_flows": 513}}, "between 1 and 512"),
        (
            {"reconnection": {"enabled": False, "attempts": 1}},
            "when enabled",
        ),
    ],
)
def test_gateway_config_rejects_every_unsafe_input(
    network: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        gateway.parse_config({"network": network}, platform="linux")


def test_secret_must_match_exact_http_policy() -> None:
    with pytest.raises(ConfigError, match="exact HTTP policy"):
        gateway.parse_config(
            {
                "network": {
                    "http": [{"host": "*.example.com", "ports": [443]}],
                    "secret": [
                        {
                            "name": "TOKEN",
                            "provider": "env:TOKEN",
                            "hosts": ["api.example.com"],
                            "ports": [443],
                            "header": "Authorization: {placeholder}",
                        }
                    ],
                }
            },
            platform="linux",
        )


def test_duplicate_new_and_legacy_ingress_is_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate network ingress"):
        gateway.parse_config(
            {
                "network": {"ingress": [{"listen": "127.0.0.1:3000", "guest_port": 80}]},
                "sbx": {"port_forwards": ["3000:81"]},
            },
            platform="linux",
        )


def test_platform_constraint_is_checked_before_state() -> None:
    with pytest.raises(ConfigError, match="supported only on Linux"):
        gateway.parse_config({"network": {}}, platform="win32")


def test_merged_toml_precedence_applies_to_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = tmp_path / "default.toml"
    local = tmp_path / "local.toml"
    explicit = tmp_path / "explicit.toml"
    default.write_text(
        '[network]\ndefault = "deny"\n[network.quotas]\ntcp_flows = 300\nopen_files = 1000\n',
        encoding="utf-8",
    )
    local.write_text("[network.quotas]\ntcp_flows = 200\n", encoding="utf-8")
    explicit.write_text("[network.quotas]\ntcp_flows = 100\n", encoding="utf-8")
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", (default,))
    monkeypatch.setattr(cli, "LOCAL_CONFIG_PATHS", (local,))

    parsed = gateway.parse_config(cli.load_config(str(explicit)), platform="linux")

    assert parsed is not None
    assert parsed.quotas.tcp_flows == 100
    assert parsed.quotas.open_files == 1000


def test_invalid_gateway_config_stops_before_command_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[network]\ndefault = "allow"\n', encoding="utf-8")
    called = False

    def mutate(_args: object) -> int:
        nonlocal called
        called = True
        return 0

    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    parser.parse_args(["doctor"]).func = mutate

    assert cli.main(["--config", str(config), "doctor"]) == 2
    assert called is False


def test_mediator_policy_uses_ephemeral_placeholders_and_inherited_secret_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_secret = "fake-real-secret-task14"
    monkeypatch.setenv("TASK14_TOKEN", fake_secret)
    config = gateway.parse_config(
        {
            "network": {
                "http": [
                    {
                        "id": "api",
                        "host": "api.example.com",
                        "ports": [443],
                        "methods": ["GET"],
                        "paths": ["/v1"],
                        "hooks": ["package-audit"],
                    }
                ],
                "secret": [
                    {
                        "name": "TOKEN",
                        "provider": "env:TASK14_TOKEN",
                        "hosts": ["api.example.com"],
                        "ports": [443],
                        "methods": ["GET"],
                        "path_prefix": "/v1",
                        "header": "Authorization: Bearer {placeholder}",
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    controller = gateway.GatewayController(config, "vm1", root=tmp_path / "state")

    payload = json.loads(controller._mediator_policy())

    placeholder = controller.secret_placeholders["TOKEN"]
    assert placeholder.startswith("SBX_SECRET_TOKEN_")
    assert len(placeholder) >= 32
    assert payload["rules"][0]["hooks"] == ["package-audit"]
    assert payload["secrets"][0]["placeholder"] == placeholder
    assert payload["secrets"][0]["value"] == fake_secret
    directory = gateway.create_state(config, "vm1", "generation1", root=tmp_path / "state")
    persisted = b"".join(path.read_bytes() for path in directory.rglob("*") if path.is_file())
    assert fake_secret.encode() not in persisted
    assert placeholder.encode() not in persisted


def test_missing_secret_provider_fails_before_state_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TASK14_MISSING", raising=False)
    config = gateway.parse_config(
        {
            "network": {
                "http": [{"host": "api.example.com", "ports": [443]}],
                "secret": [
                    {
                        "name": "TOKEN",
                        "provider": "env:TASK14_MISSING",
                        "hosts": ["api.example.com"],
                        "ports": [443],
                        "header": "Authorization: Bearer {placeholder}",
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    controller = gateway.GatewayController(config, "vm1", root=tmp_path / "state")

    with pytest.raises(gateway.GatewayLifecycleError, match="provider is unavailable"):
        controller.prepare()

    assert not (tmp_path / "state").exists()


def test_state_is_owner_only_atomic_and_secret_free(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    root = tmp_path / "gateway"
    directory = gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in (directory / "state.json", directory / "policy.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    combined = (directory / "state.json").read_text() + (directory / "policy.json").read_text()
    assert "env:NPM_TOKEN" not in combined
    assert "Authorization" not in combined
    assert "SBX_SECRET" not in combined
    assert not list(directory.glob("*.tmp"))


def test_state_records_only_approved_runtime_metadata(
    gateway_config: gateway.GatewayConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = gateway.create_state(
        gateway_config, "vm-1", "generation-1", root=tmp_path / "gateway"
    )
    gateway.update_state(
        directory,
        status="ready",
        pids={"worker": os.getpid(), "mediator": os.getpid()},
        sockets={
            "qemu": directory / "qemu-net.sock",
            "control": directory / "control.sock",
        },
        listeners=gateway_config.ingress,
        counters={"tcp_flows": 3, "denied": 1},
        failure=("worker", "quota_exhausted"),
    )

    state = gateway.read_state(directory)
    assert state["status"] == "ready"
    assert state["pids"]["worker"] == os.getpid()
    assert state["listeners"][0]["guest_port"] == 3000
    assert state["last_failure"]["code"] == "quota_exhausted"
    assert not any(path.name.startswith(".state.json") for path in directory.iterdir())

    with pytest.raises(gateway.GatewayStateError, match="crosses"):
        gateway.update_state(directory, sockets={"qemu": tmp_path / "other.sock"})
    with pytest.raises(gateway.GatewayStateError, match="classification"):
        gateway.update_state(directory, failure=("worker", "TOKEN_VALUE"))


def test_state_rejects_symlinks_at_every_owned_boundary(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = tmp_path / "gateway"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)
    assert gateway.diagnostic_lines("vm-1", configured=gateway_config, root=root) == [
        "Gateway: error",
        "Gateway state: unsafe or corrupt (details redacted)",
    ]

    root.unlink()
    root.mkdir(mode=0o700)
    (root / "vm-1").symlink_to(target, target_is_directory=True)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)


def test_state_rejects_nested_symlink_ancestor_and_oversized_state(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.create_state(
            gateway_config,
            "vm-1",
            "generation-1",
            root=linked / "nested" / "gateway",
        )

    directory = gateway.create_state(
        gateway_config, "vm-1", "generation-1", root=tmp_path / "gateway"
    )
    state = directory / "state.json"
    with state.open("ab") as destination:
        destination.truncate((1 << 20) + 1)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.read_state(directory)


def test_state_rejects_non_owner_only_permissions(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    root = tmp_path / "gateway"
    root.mkdir(mode=0o755)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)

    os.chmod(root, 0o700)
    directory = gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)
    os.chmod(directory / "state.json", 0o644)
    with pytest.raises(gateway.GatewayStateError, match="unsafe"):
        gateway.read_state(directory)


def test_state_refuses_symlink_replacement(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    directory = gateway.create_state(
        gateway_config, "vm-1", "generation-1", root=tmp_path / "gateway"
    )
    state = directory / "state.json"
    state.unlink()
    state.symlink_to(tmp_path / "victim")

    with pytest.raises(gateway.GatewayStateError, match="unsafe|corrupt"):
        gateway.update_state(directory, status="ready")


def test_corrupt_and_cross_vm_state_are_redacted(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    root = tmp_path / "gateway"
    directory = gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)
    state_path = directory / "state.json"
    state_path.write_text('not-json SUPER_SECRET_TOKEN="hunter2"', encoding="utf-8")
    os.chmod(state_path, 0o600)

    lines = gateway.diagnostic_lines("vm-1", configured=gateway_config, root=root)

    assert lines == ["Gateway: error", "Gateway state: unsafe or corrupt (details redacted)"]
    assert "hunter2" not in "\n".join(lines)

    state_path.write_text(json.dumps(gateway._new_state(gateway_config, "vm-2", "generation-1")))
    os.chmod(state_path, 0o600)
    lines = gateway.diagnostic_lines("vm-1", configured=gateway_config, root=root)
    assert "identity" not in "\n".join(lines).lower()
    assert "redacted" in lines[1]


def test_stale_state_diagnostics_do_not_expose_pids_or_sockets(
    gateway_config: gateway.GatewayConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gateway"
    directory = gateway.create_state(gateway_config, "vm-1", "generation-1", root=root)
    gateway.update_state(
        directory,
        status="ready",
        pids={"worker": 999999, "mediator": 999998},
        sockets={"qemu": directory / "qemu-net.sock"},
        listeners=gateway_config.ingress,
        counters={"allowed": 4},
        failure=("worker", "worker_failed"),
    )
    monkeypatch.setattr(gateway, "pid_is_alive", lambda _pid: False)

    output = "\n".join(gateway.diagnostic_lines("vm-1", root=root))

    assert "Gateway state: stale" in output
    assert "999999" not in output
    assert "qemu-net.sock" not in output
    assert "Gateway sockets: qemu=recorded" in output
    assert "worker/worker_failed" in output
    assert "env:NPM_TOKEN" not in output


def test_cross_vm_paths_and_invalid_ids_are_rejected(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="invalid VM identity"):
        gateway.create_state(gateway_config, "../vm-2", "generation-1", root=tmp_path)
    with pytest.raises(ConfigError, match="generation"):
        gateway.create_state(gateway_config, "vm-1", "../generation", root=tmp_path)


def test_network_diagnostics_reports_disabled_without_touching_smolvm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gateway, "_latest_state", lambda *args, **kwargs: (None, None))

    assert cli.main(["network", "diagnostics", "vm-1"]) == 0
    assert capsys.readouterr().out == "Gateway: disabled\n"
