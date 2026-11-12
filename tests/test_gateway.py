import base64
import json
import os
import socket
import stat
import uuid
from itertools import permutations
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
                "policy": [
                    {
                        "id": "registry",
                        "mode": "http-mediated",
                        "hosts": ["Registry.NPMJS.org."],
                        "ports": [443],
                        "methods": ["head", "GET"],
                        "path_prefixes": ["/package/"],
                        "hooks": ["package-audit"],
                    },
                    {
                        "id": "tcp",
                        "mode": "tcp-forward",
                        "hosts": ["github.com"],
                        "ports": [22],
                    },
                    {
                        "id": "tls",
                        "mode": "tls-passthrough",
                        "hosts": ["pinned.example.com"],
                        "ports": [443],
                    },
                ],
                "credential": [
                    {
                        "id": "npm",
                        "environment": "NPM_TOKEN",
                        "method": "command",
                        "options": {"argv": ["get-npm-token"]},
                        "bindings": [
                            {
                                "policies": ["registry"],
                                "methods": ["GET"],
                                "path_prefixes": ["/package/"],
                                "header": "Authorization: Bearer {placeholder}",
                            }
                        ],
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
    assert (
        gateway._resolve_artifact(
            "SBX_TEST_MISSING_OVERRIDE",
            (None, artifact),
            label="test artifact",
        )
        == artifact
    )
    assert gateway._hardening_ready("no_new_privs,seccomp,landlock-unavailable,rlimits", "linux")
    assert not gateway._hardening_ready("seccomp", "linux")
    assert gateway._hardening_ready("unverified", "darwin")


def test_gateway_defaults_are_deny_and_bounded() -> None:
    config = gateway.parse_config({"network": {}}, platform="linux")

    assert config == gateway.GatewayConfig(
        rules=(),
        credentials=(),
        ingress=(),
        quotas=gateway.Quotas(),
        reconnection=gateway.Reconnection(),
        platform="linux",
    )


def test_gateway_full_config_normalizes_and_preserves_legacy_ingress(
    gateway_config: gateway.GatewayConfig,
) -> None:
    assert [rule.id for rule in gateway_config.rules] == ["registry", "tcp", "tls"]
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
        ({"http": [{"host": "*.com", "ports": [443]}]}, "unknown field"),
        (
            {
                "http": [{"host": "example.com", "ports": [443]}],
                "tls_passthrough": [{"host": "example.com", "ports": [443]}],
            },
            "unknown field",
        ),
        (
            {"http": [{"host": "example.com", "ports": [443], "hooks": ["module:run"]}]},
            "unknown field",
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
            "unknown field",
        ),
        (
            {"tls_passthrough": [{"host": "*.example.com", "ports": [443]}]},
            "unknown field",
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
            {
                "ingress": [{"guest_port": 80}, {"guest_port": 81}],
                "quotas": {"ingress_flows": 1},
            },
            "listeners exceed",
        ),
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


def test_credential_requires_exact_http_policy() -> None:
    with pytest.raises(ConfigError, match="exact hosts"):
        gateway.parse_config(
            {
                "network": {
                    "policy": [
                        {
                            "id": "api",
                            "mode": "http-mediated",
                            "hosts": ["*.example.com"],
                            "ports": [443],
                        }
                    ],
                    "credential": [
                        {
                            "id": "token",
                            "environment": "TOKEN",
                            "method": "command",
                            "options": {"argv": ["token"]},
                            "bindings": [
                                {"policies": ["api"], "header": "Authorization: {placeholder}"}
                            ],
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


def test_path_canonicalization_matches_the_worker() -> None:
    assert gateway.canonical_path("/demo/") == "/demo"


def test_gateway_runtime_paths_do_not_include_vm_or_state_names() -> None:
    runtime_root = Path("/tmp/sbx-runtime-test")
    gateway.validate_runtime_paths("v" * 128, runtime_root=runtime_root)
    generation = uuid.uuid4().hex
    directory = gateway._select_runtime_directory(generation, root=runtime_root)

    assert directory == runtime_root / generation
    assert all(
        len(os.fsencode(directory / name)) <= gateway.GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
        for name in ("qemu-net.sock", "control.sock")
    )


def test_gateway_compacts_only_the_runtime_generation_when_needed() -> None:
    generation = uuid.uuid4().hex
    runtime_root = Path("/tmp") / ("r" * 55)
    directory = gateway._select_runtime_directory(generation, root=runtime_root)

    assert directory.name != generation
    assert gateway._compact_runtime_generation(generation) == directory.name
    assert uuid.UUID(bytes=base64.urlsafe_b64decode(directory.name + "==")).hex == generation


def test_gateway_runtime_root_selection_and_portable_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = uuid.uuid4().hex
    assert not gateway._safe_existing_runtime_root(Path("relative"))
    assert not gateway._safe_existing_runtime_root(tmp_path / "missing")
    with pytest.raises(ConfigError, match="must be absolute"):
        gateway._select_runtime_directory(generation, root=Path("relative"))
    with pytest.raises(ConfigError, match="exceed 103"):
        gateway._select_runtime_directory(generation, root=Path("/") / ("x" * 100))

    xdg = Path("/tmp") / f"sbx-xdg-test-{uuid.uuid4().hex}"
    xdg.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    assert gateway._select_runtime_directory(generation) == xdg / "sbx" / generation

    os.chmod(xdg, 0o755)
    fallback = gateway._select_runtime_directory(generation)
    assert fallback.parent == Path("/tmp") / f"sbx-{os.getuid()}"

    os.chmod(xdg, 0o700)
    linked = tmp_path / "linked-runtime"
    linked.symlink_to(xdg, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(linked))
    assert gateway._select_runtime_directory(generation).parent == fallback.parent

    overlong = tmp_path / ("x" * 80)
    overlong.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(overlong))
    assert gateway._select_runtime_directory(generation).parent == fallback.parent

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    assert gateway._select_runtime_directory(generation).parent == (
        Path("/private/tmp") / f"sbx-{os.getuid()}"
    )
    xdg.rmdir()


def test_gateway_runtime_state_is_separate_private_and_cleaned_up(
    gateway_config: gateway.GatewayConfig, tmp_path: Path
) -> None:
    generation = uuid.uuid4().hex
    runtime_root = Path("/tmp") / f"sbx-task35-{uuid.uuid4().hex}"
    runtime_directory = gateway._create_runtime_directory(generation, root=runtime_root)
    state_root = tmp_path / ("state-" + "s" * 80)
    directory = gateway.create_state(
        gateway_config,
        "v" * 128,
        generation,
        root=state_root,
        runtime_directory=runtime_directory,
    )
    listeners = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2)]
    try:
        paths = {
            "qemu": runtime_directory / "qemu-net.sock",
            "control": runtime_directory / "control.sock",
        }
        for listener, path in zip(listeners, paths.values(), strict=True):
            listener.bind(str(path))
            os.chmod(path, 0o600)
        gateway.update_state(directory, sockets=paths)
        state = gateway.read_state(directory)
        assert state["generation"] == generation
        assert state["runtime_directory"] == str(runtime_directory)
        assert all(
            str(state_root) not in value
            and "v" * 128 not in value
            and len(os.fsencode(value)) <= gateway.GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
            for value in state["sockets"].values()
        )
    finally:
        for listener in listeners:
            listener.close()
        gateway._remove_state_directory(directory)
    assert not runtime_directory.exists()


def test_gateway_runtime_cleanup_refuses_unexpected_files(tmp_path: Path) -> None:
    runtime_directory = gateway._create_runtime_directory(
        uuid.uuid4().hex, root=Path("/tmp") / f"sbx-cleanup-test-{uuid.uuid4().hex}"
    )
    with pytest.raises(gateway.GatewayStateError, match="already exists"):
        gateway._create_runtime_directory(runtime_directory.name, root=runtime_directory.parent)

    unexpected = runtime_directory / "control.sock"
    unexpected.write_text("not a socket")

    with pytest.raises(gateway.GatewayStateError, match="unsafe gateway runtime socket"):
        gateway._remove_runtime_directory(runtime_directory)

    assert unexpected.read_text() == "not a socket"
    unexpected.unlink()
    gateway._remove_runtime_directory(runtime_directory)


def test_platform_constraint_is_checked_before_state() -> None:
    with pytest.raises(ConfigError, match="supported only on Linux"):
        gateway.parse_config({"network": {}}, platform="win32")


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: gateway._mapping([], "value"), "TOML table"),
        (lambda: gateway._tables({}, "value"), "array of TOML tables"),
        (lambda: gateway._strings(None, "value", required=True), "array of strings"),
        (lambda: gateway._ports([], "value"), "non-empty"),
        (lambda: gateway._ports([0], "value"), "between 1"),
        (lambda: gateway._ports([1, 1], "value"), "duplicate port"),
        (lambda: gateway._port(0, "value"), "between 1"),
        (lambda: gateway._normalize_host("bad\n", "host"), "hostname"),
        (lambda: gateway._normalize_host("*.example.com", "host", exact=True), "exact"),
        (lambda: gateway._normalize_host("*.com", "host"), "too broad"),
        (lambda: gateway._normalize_host("127.0.0.1", "host", exact=True), "DNS hostname"),
        (lambda: gateway._methods(["bad method"], "methods"), "invalid HTTP method"),
        (lambda: gateway.canonical_path("relative"), "unsafe path"),
        (lambda: gateway.canonical_path("/%zz"), "unsafe path"),
        (lambda: gateway.canonical_path("/%25252525252f"), "decoding limit"),
        (lambda: gateway.canonical_path("/a/../b"), "unsafe path"),
        (lambda: gateway._hooks(["package-audit", "package-audit"], "hooks"), "duplicate"),
        (lambda: gateway._hooks(["unknown"], "hooks"), "unapproved"),
        (lambda: gateway.validate_header_descriptor([]), "descriptor"),
        (
            lambda: gateway.validate_header_descriptor(
                {"name": "bad name", "format": "template", "value": "{placeholder}"}
            ),
            "descriptor",
        ),
        (
            lambda: gateway.validate_header_descriptor(
                {"name": "Authorization", "format": "template", "value": "literal"}
            ),
            "descriptor",
        ),
        (
            lambda: gateway.validate_header_descriptor(
                {
                    "name": "Authorization",
                    "format": "basic-password",
                    "username": "bad:user",
                }
            ),
            "descriptor",
        ),
        (
            lambda: gateway.validate_header_descriptor(
                {"name": "Authorization", "format": "unknown"}
            ),
            "descriptor",
        ),
        (
            lambda: gateway._binding(
                {"policies": ["api", "api"], "header": "Authorization: {placeholder}"},
                "binding",
            ),
            "invalid or duplicate",
        ),
        (lambda: gateway._header("Authorization: literal", "header"), "structural"),
        (lambda: gateway._header("bad name: {placeholder}", "header"), "field name"),
        (lambda: gateway._listen("", "listen"), "ADDRESS:PORT"),
        (lambda: gateway._listen("missing-port", "listen"), "ADDRESS:PORT"),
        (lambda: gateway._listen("[::1", "listen"), "ADDRESS:PORT"),
        (lambda: gateway._listen("host:port", "listen"), "IP address"),
        (lambda: gateway._reconnection({"enabled": "yes"}), "boolean"),
        (lambda: gateway._reconnection({"timeout_seconds": 0}), "between 0.1"),
        (lambda: gateway.parse_port_forward(":1:2"), "host address is empty"),
        (lambda: gateway.parse_port_forward("a:b"), "ports must be numbers"),
        (lambda: gateway.parse_port_forward("1:2:3:4"), "use GUEST_PORT"),
    ],
)
def test_gateway_schema_helpers_reject_unsafe_values(operation, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        operation()


def _policy(policy_id: str = "api", **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": policy_id,
        "mode": "http-mediated",
        "hosts": ["api.example.com"],
        "ports": [443],
        "methods": ["GET"],
        "path_prefixes": ["/v1"],
    }
    for key, update in updates.items():
        if update is None:
            value.pop(key, None)
        else:
            value[key] = update
    return value


@pytest.mark.parametrize(
    "modes", list(permutations(("http-mediated", "tls-passthrough", "tcp-forward"), 2))
)
@pytest.mark.parametrize("wildcard", [False, True])
def test_tcp_policy_modes_cannot_overlap(modes, wildcard):
    hosts = ["db.example.com", "db.example.com"]
    if wildcard:
        hosts[0 if modes[0] != "tls-passthrough" else 1] = "*.example.com"
    policies = [
        _policy(id=f"rule-{index}", mode=mode, hosts=[host], methods=None, path_prefixes=None)
        for index, (mode, host) in enumerate(zip(modes, hosts, strict=True))
    ]
    with pytest.raises(ConfigError):
        gateway.parse_config({"network": {"policy": policies}})


@pytest.mark.parametrize(
    "second_host,second_port", [("db.example.com", 8443), ("other.example.net", 443)]
)
def test_distinct_tcp_endpoints_can_use_different_modes(second_host, second_port):
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    _policy(hosts=["db.example.com"], methods=None, path_prefixes=None),
                    _policy(
                        id="raw",
                        mode="tcp-forward",
                        hosts=[second_host],
                        ports=[second_port],
                        methods=None,
                        path_prefixes=None,
                    ),
                ]
            }
        }
    )
    assert config is not None and len(config.rules) == 2


def _command_credential(credential_id: str = "token", **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": credential_id,
        "environment": "TOKEN_" + credential_id.upper().replace("-", "_"),
        "method": "command",
        "options": {"argv": ["token"]},
        "bindings": [
            {
                "policies": ["api"],
                "methods": ["GET"],
                "path_prefixes": ["/v1"],
                "header": "Authorization: Bearer {placeholder}",
            }
        ],
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("network", "message"),
    [
        ({"policy": [_policy(extra=True)]}, "unknown field"),
        ({"policy": [_policy(id=None)]}, "id is invalid"),
        ({"policy": [_policy(mode="invalid")]}, "mode must be"),
        ({"policy": [_policy(hosts=["api.example.com", "API.EXAMPLE.COM."])]}, "duplicate"),
        (
            {"policy": [_policy(mode="tcp-forward", methods=["GET"])]},
            "only in http-mediated",
        ),
        ({"policy": [_policy(allow_ranges=["10.0.0.0/8"])]}, "not permitted"),
        (
            {
                "policy": [
                    _policy(
                        mode="tcp-forward",
                        methods=None,
                        path_prefixes=None,
                        allow_ranges=["invalid"],
                    )
                ]
            },
            "invalid network",
        ),
        (
            {
                "policy": [
                    _policy(
                        mode="tcp-forward",
                        methods=None,
                        path_prefixes=None,
                        allow_ranges=["10.0.0.1/8", "10.0.0.0/8"],
                    )
                ]
            },
            "duplicate network",
        ),
        ({"policy": [_policy(), _policy()]}, "IDs must be unique"),
        (
            {"policy": [_policy("wild", hosts=["*.example.com"]), _policy()]},
            "overlap",
        ),
        ({"credential": [_command_credential(extra=True)]}, "unknown field"),
        ({"credential": [_command_credential(id=None)]}, "id is invalid"),
        ({"credential": [_command_credential(environment="bad-name")]}, "environment is invalid"),
        ({"credential": [_command_credential(method="plugin:")]}, "method must be"),
        (
            {
                "credential": [
                    _command_credential(
                        options={"argv": ["token"], "inherit_environment": ["A", "A"]}
                    )
                ]
            },
            "unique environment",
        ),
        ({"credential": [_command_credential(options={"argv": []})]}, "non-empty argv"),
        (
            {"credential": [_command_credential(options={"argv": ["token"], "format": "xml"})]},
            "text or json",
        ),
        (
            {"credential": [_command_credential(bindings=[{"policies": ["api"]}])]},
            "explicit header",
        ),
        (
            {
                "policy": [_policy()],
                "credential": [_command_credential(), _command_credential()],
            },
            "IDs must be unique",
        ),
        (
            {
                "policy": [_policy()],
                "credential": [
                    _command_credential("one", environment="TOKEN"),
                    _command_credential("two", environment="TOKEN"),
                ],
            },
            "environment names must be unique",
        ),
        (
            {
                "policy": [_policy()],
                "credential": [
                    _command_credential(),
                    _command_credential("other", environment="OTHER_TOKEN"),
                ],
            },
            "overlap",
        ),
    ],
)
def test_unified_policy_schema_validation_matrix(network: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        gateway.parse_config({"network": network}, platform="linux")


def test_unified_policy_binding_scope_and_outer_config_rejections() -> None:
    cases = [
        (
            {"network": {"policy": [_policy()], "credential": [_command_credential()]}, "sbx": []},
            "TOML table",
        ),
        (
            {
                "network": {"policy": [_policy()], "credential": [_command_credential()]},
                "sbx": {"env": ["TOKEN_TOKEN"]},
            },
            "collides",
        ),
        (
            {
                "network": {
                    "policy": [_policy()],
                    "credential": [
                        _command_credential(
                            bindings=[
                                {"policies": ["missing"], "header": "Authorization: {placeholder}"}
                            ]
                        )
                    ],
                }
            },
            "unknown policy",
        ),
        (
            {
                "network": {
                    "policy": [_policy(mode="tcp-forward", methods=None, path_prefixes=None)],
                    "credential": [_command_credential()],
                }
            },
            "http-mediated",
        ),
        (
            {
                "network": {
                    "policy": [_policy()],
                    "credential": [
                        _command_credential(
                            bindings=[
                                {
                                    "policies": ["api"],
                                    "methods": ["POST"],
                                    "header": "Authorization: {placeholder}",
                                }
                            ]
                        )
                    ],
                }
            },
            "subset of policy methods",
        ),
        (
            {
                "network": {
                    "policy": [_policy()],
                    "credential": [
                        _command_credential(
                            bindings=[
                                {
                                    "policies": ["api"],
                                    "path_prefixes": ["/v2"],
                                    "header": "Authorization: {placeholder}",
                                }
                            ]
                        )
                    ],
                }
            },
            "subset of policy paths",
        ),
    ]
    for config, message in cases:
        with pytest.raises(ConfigError, match=message):
            gateway.parse_config(config, platform="linux")


def test_valid_policy_variants_and_finalized_plugin_headers() -> None:
    assert gateway._strings(None, "optional") == ()
    assert gateway._path_scopes_overlap((), ("/v1",))
    assert gateway.validate_header_descriptor(
        {
            "name": "Authorization",
            "format": "template",
            "value": "Bearer {placeholder}",
        }
    ) == {
        "name": "Authorization",
        "format": "template",
        "value": "Bearer {placeholder}",
    }
    assert gateway.validate_header_descriptor(None, allow_none=True) is None
    assert gateway._listen("localhost:1234", "listen") == ("127.0.0.1", 1234)
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    _policy(hosts=["one.example.com", "two.example.com"]),
                    _policy(
                        "tcp",
                        mode="tcp-forward",
                        hosts=["*.git.example.com"],
                        ports=[22],
                        methods=None,
                        path_prefixes=None,
                        allow_ranges=["10.0.0.1/8"],
                    ),
                ],
                "credential": [
                    {
                        "id": "plugin",
                        "environment": "PLUGIN_TOKEN",
                        "method": "plugin:test",
                        "bindings": [{"policies": ["api"]}],
                    }
                ],
                "ingress": [{"id": "ipv6", "listen": "[::1]:0", "guest_port": 80}],
            }
        },
        platform="linux",
    )
    assert config is not None
    assert [rule.id for rule in config.rules] == ["api-1", "api-2", "tcp"]
    assert config.rules[-1].allow_ranges == ("10.0.0.0/8",)
    assert config.ingress[0].listen_host == "::1"
    described = [
        gateway.replace(
            config.credentials[0],
            header={
                "name": "Authorization",
                "format": "basic-password",
                "username": "aws",
            },
        )
    ]
    finalized = gateway.finalize_credentials(config, described)
    assert finalized.credentials[0].header["username"] == "aws"
    with pytest.raises(ConfigError, match="do not match"):
        gateway.finalize_credentials(config, [])

    legacy = gateway.parse_config(
        {"network": {}, "sbx": {"env": "FORWARDED", "port_forwards": "1234"}},
        platform="linux",
    )
    assert legacy is not None
    assert legacy.ingress[0].host_port == 1234


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


def test_mediator_policy_uses_opaque_placeholders_without_values(tmp_path: Path) -> None:
    config = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {
                        "id": "api",
                        "mode": "http-mediated",
                        "hosts": ["api.example.com"],
                        "ports": [443],
                        "methods": ["GET"],
                        "path_prefixes": ["/v1"],
                        "hooks": ["package-audit"],
                    }
                ],
                "credential": [
                    {
                        "id": "token",
                        "environment": "TOKEN",
                        "method": "command",
                        "options": {"argv": ["get-token"]},
                        "bindings": [
                            {
                                "policies": ["api"],
                                "methods": ["GET"],
                                "path_prefixes": ["/v1"],
                                "header": "Authorization: Bearer {placeholder}",
                            }
                        ],
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config is not None
    controller = gateway.GatewayController(config, "vm1", root=tmp_path / "state")
    payload = json.loads(controller._mediator_policy())
    placeholder = controller.credential_placeholders["TOKEN"]
    assert len(placeholder) >= 32 and "SBX" not in placeholder
    assert payload["rules"][0]["hooks"] == ["package-audit"]
    assert payload["credentials"][0]["placeholder"] == placeholder
    assert "value" not in payload["credentials"][0]
    directory = gateway.create_state(config, "vm1", "generation1", root=tmp_path / "state")
    persisted = b"".join(path.read_bytes() for path in directory.rglob("*") if path.is_file())
    assert placeholder.encode() not in persisted


def test_missing_inherited_environment_fails_before_state_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TASK14_MISSING", raising=False)
    config = gateway.parse_config(
        {
            "network": {
                "credential": [
                    {
                        "id": "token",
                        "environment": "TOKEN",
                        "method": "command",
                        "options": {"argv": ["token"], "inherit_environment": ["TASK14_MISSING"]},
                    }
                ]
            }
        },
        platform="linux",
    )
    assert config is not None
    controller = gateway.PersistentGatewayController(config, "vm1", root=tmp_path / "state")
    with pytest.raises(gateway.GatewayLifecycleError, match="missing host environment"):
        controller.prepare()
    assert not any((tmp_path / "state").rglob("state.json"))


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
    runtime_directory = tmp_path / "runtime" / "generation-1"
    directory = gateway.create_state(
        gateway_config,
        "vm-1",
        "generation-1",
        root=tmp_path / "gateway",
        runtime_directory=runtime_directory,
    )
    gateway.update_state(
        directory,
        status="ready",
        pids={"worker": os.getpid(), "mediator": os.getpid()},
        sockets={
            "qemu": runtime_directory / "qemu-net.sock",
            "control": runtime_directory / "control.sock",
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

    assert state["runtime_directory"] == str(runtime_directory)
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
    runtime_directory = tmp_path / "runtime" / "generation-1"
    directory = gateway.create_state(
        gateway_config,
        "vm-1",
        "generation-1",
        root=root,
        runtime_directory=runtime_directory,
    )
    gateway.update_state(
        directory,
        status="ready",
        pids={"worker": 999999, "mediator": 999998},
        sockets={"qemu": runtime_directory / "qemu-net.sock"},
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
