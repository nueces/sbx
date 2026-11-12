from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from sbx import (
    gateway,
    gateway_credentials,
    gateway_provider_helper,
    gateway_supervisor,
    guest_setup,
)
from sbx.runtime import ConfigError


def credential_config(method: str = "plugin:aws-codeartifact") -> gateway.GatewayConfig:
    options: dict[str, object]
    if method == "command":
        options = {"argv": ["credential-helper"], "format": "text"}
    else:
        options = {
            "domain": "domain",
            "domain_owner": "123456789012",
            "region": "us-east-1",
            "inherit_environment": ["AWS_ACCESS_KEY_ID"],
        }
    parsed = gateway.parse_config(
        {
            "network": {
                "policy": [
                    {
                        "id": "packages",
                        "mode": "http-mediated",
                        "hosts": ["domain.d.codeartifact.us-east-1.amazonaws.com"],
                        "ports": [443],
                        "methods": ["GET", "HEAD"],
                        "path_prefixes": ["/pypi/repo/"],
                    }
                ],
                "credential": [
                    {
                        "id": "packages",
                        "environment": "CODEARTIFACT_AUTH_TOKEN",
                        "method": method,
                        "options": options,
                        "bindings": [{"policies": ["packages"]}]
                        if method != "command"
                        else [
                            {
                                "policies": ["packages"],
                                "header": "Authorization: Bearer {placeholder}",
                            }
                        ],
                    }
                ],
            }
        },
        platform="linux",
    )
    assert parsed is not None
    return parsed


@pytest.mark.parametrize("field,scope", [("methods", ["GET"]), ("path_prefixes", ["/private"])])
@pytest.mark.parametrize("parent", [None, []], ids=["omitted", "empty"])
def test_credential_binding_can_narrow_unrestricted_policy(field, scope, parent):
    policy = {"id": "api", "mode": "http-mediated", "hosts": ["api.example.com"], "ports": [443]}
    if parent is not None:
        policy[field] = parent
    config = gateway.parse_config(
        {
            "network": {
                "policy": [policy],
                "credential": [
                    {
                        "id": "token",
                        "environment": "API_TOKEN",
                        "method": "command",
                        "options": {"argv": ["credential-helper"]},
                        "bindings": [
                            {
                                "policies": ["api"],
                                "header": "Authorization: Bearer {placeholder}",
                                field: scope,
                            }
                        ],
                    }
                ],
            }
        },
        platform="linux",
    )
    assert config.rules[0].methods == () and config.rules[0].paths == ()
    binding = config.credentials[0].bindings[0]
    assert (binding.methods if field == "methods" else binding.paths) == tuple(scope)
    assert gateway.finalize_credentials(config, config.credentials) == config


def test_unified_schema_rejects_old_forms_and_binding_widening() -> None:
    for old in ("http", "tls_passthrough", "tcp", "secret"):
        with pytest.raises(ConfigError, match="unknown field"):
            gateway.parse_config({"network": {old: []}}, platform="linux")
    config = credential_config()
    assert config.rules[0].policy_id == "packages"
    assert config.credentials[0].inherit_environment == ("AWS_ACCESS_KEY_ID",)
    with pytest.raises(ConfigError, match="unsafe path"):
        gateway.parse_config(
            {
                "network": {
                    "policy": [
                        {
                            "id": "bad",
                            "mode": "http-mediated",
                            "hosts": ["example.com"],
                            "ports": [443],
                            "path_prefixes": ["/%zz"],
                        }
                    ]
                }
            },
            platform="linux",
        )
    with pytest.raises(ConfigError, match="collides"):
        gateway.parse_config(
            {
                "sbx": {"env": ["TOKEN"]},
                "network": {
                    "credential": [
                        {
                            "id": "token",
                            "environment": "TOKEN",
                            "method": "command",
                            "options": {"argv": ["token"]},
                        }
                    ]
                },
            },
            platform="linux",
        )


def test_shipped_codeartifact_description_fetch_cache_and_environment(tmp_path) -> None:
    aws = tmp_path / "aws"
    aws.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'test "$AWS_ACCESS_KEY_ID" = fake-source\n'
        "printf x >> calls\n"
        'printf \'%s\\n\' \'{"authorizationToken":"fake-token","expiration":1200}\'\n'
    )
    aws.chmod(aws.stat().st_mode | stat.S_IXUSR)
    now = [1000.0]
    config = credential_config()
    broker = gateway_supervisor.CredentialBroker(
        config.credentials,
        {
            "baseline": {"HOME": str(tmp_path), "PATH": str(tmp_path), "TZ": "UTC"},
            "inherited": {"AWS_ACCESS_KEY_ID": "fake-source"},
        },
        tmp_path,
        clock=lambda: now[0],
    )

    described = broker.describe()
    assert described[0].header == {
        "name": "Authorization",
        "format": "basic-password",
        "username": "aws",
    }
    assert not (tmp_path / "calls").exists()
    assert broker.fetch("packages") == "fake-token"
    assert broker.fetch("packages") == "fake-token"
    assert (tmp_path / "calls").read_text() == "x"
    now[0] = 1150
    assert broker.fetch("packages") == "fake-token"
    assert (tmp_path / "calls").read_text() == "xx"


def test_rootless_environment_drops_stale_sudo_identity(monkeypatch) -> None:
    monkeypatch.setattr(guest_setup.os, "geteuid", lambda: 1000)
    environment = guest_setup.sanitize_forwarded_env(
        {
            "HOME": "/home/agent",
            "SUDO_USER": "root",
            "SUDO_UID": "0",
            "SUDO_GID": "0",
            "SUDO_HOME": "/root",
            "SUDO_COMMAND": "shell",
        },
        [],
    )
    assert environment == {"HOME": "/home/agent"}


def test_guest_placeholder_manifest_removes_stale_names(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        guest_setup,
        "sync_forwarded_env_direct_ssh",
        lambda vm_id, values, missing, **kwargs: captured.update(
            vm_id=vm_id, values=values, missing=missing
        ),
    )
    guest_setup.sync_network_credentials(
        "vm1",
        {"NEW_TOKEN": "opaque"},
        run_capture=lambda _argv: subprocess.CompletedProcess(
            [], 0, "export OLD_TOKEN=old\nexport SBX_NETWORK_CREDENTIAL_NAMES=OLD_TOKEN\n", ""
        ),
        ssh=lambda _vm_id: ["ssh"],
    )
    assert captured == {
        "vm_id": "vm1",
        "values": {
            "NEW_TOKEN": "opaque",
            "SBX_NETWORK_CREDENTIAL_NAMES": "NEW_TOKEN",
        },
        "missing": ["OLD_TOKEN"],
    }


def test_supervisor_config_round_trip_and_broker_validation(tmp_path, monkeypatch) -> None:
    config = credential_config("command")
    assert (
        gateway_supervisor.config_from_payload(gateway_supervisor.config_payload(config)) == config
    )

    unbound = gateway.replace(config.credentials[0], bindings=())
    broker = gateway_supervisor.CredentialBroker(
        (unbound,),
        {"baseline": {"HOME": str(tmp_path), "PATH": "/bin", "TZ": "UTC"}, "inherited": {}},
        tmp_path,
        clock=lambda: 100.0,
    )
    assert broker.describe() == (unbound,)
    with pytest.raises(gateway.GatewayAuthenticationError, match="unknown"):
        broker.fetch("missing")

    bound_broker = gateway_supervisor.CredentialBroker(
        config.credentials,
        {"baseline": {"HOME": str(tmp_path), "PATH": "/bin", "TZ": "UTC"}, "inherited": {}},
        tmp_path,
        clock=lambda: 100.0,
    )
    monkeypatch.setattr(bound_broker, "_invoke", lambda *_args, **_kwargs: {"unexpected": None})
    with pytest.raises(gateway.GatewayLifecycleError, match="description"):
        bound_broker.describe()
    with pytest.raises(gateway.GatewayLifecycleError, match="provider failed"):
        bound_broker.fetch("packages")

    bound_broker.cache["packages"] = ("still-valid", 101.0)
    assert bound_broker.values == {"still-valid"}
    monkeypatch.setattr(
        bound_broker,
        "_invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret-bearing error")),
    )
    assert bound_broker.fetch("packages") == "still-valid"
    bound_broker.cache["packages"] = ("expired", 99.0)
    with pytest.raises(gateway.GatewayLifecycleError, match="provider failed"):
        bound_broker.fetch("packages")

    invalid_environment = gateway_supervisor.CredentialBroker(config.credentials, {}, tmp_path)
    with pytest.raises(gateway.GatewayLifecycleError, match="invalid inherited"):
        invalid_environment._environment(config.credentials[0], "fetch")

    inherited = gateway.replace(config.credentials[0], inherit_environment=("MISSING",))
    missing_environment = gateway_supervisor.CredentialBroker(
        (inherited,),
        {"baseline": {"HOME": str(tmp_path), "PATH": "/bin", "TZ": "UTC"}, "inherited": {}},
        tmp_path,
    )
    with pytest.raises(gateway.GatewayLifecycleError, match="missing host environment"):
        missing_environment._environment(inherited, "fetch")


@pytest.mark.parametrize(
    "cached_expiry,result_expiry,provider_fails,expected",
    [
        (1001.0, None, True, None),
        (1002.0, None, True, None),
        (1010.0, None, True, "cached"),
        (None, 1001.0, False, None),
        (None, 1002.0, False, None),
        (None, 1010.0, False, "new"),
        (1010.0, 1001.0, False, "cached"),
    ],
)
def test_provider_completion_rechecks_expiration(
    tmp_path, monkeypatch, cached_expiry, result_expiry, provider_fails, expected
):
    config = credential_config("command")
    now = [1000.0]
    broker = gateway_supervisor.CredentialBroker(
        config.credentials,
        {"baseline": {"HOME": str(tmp_path), "PATH": "/bin", "TZ": "UTC"}, "inherited": {}},
        tmp_path,
        clock=lambda: now[0],
    )
    if cached_expiry is not None:
        broker.cache["packages"] = ("cached", cached_expiry)

    def invoke(*args, **kwargs):
        now[0] = 1002.0
        if provider_fails:
            raise gateway.GatewayLifecycleError("simulated refresh failure")
        return {"value": "new", "expires_at": result_expiry}

    monkeypatch.setattr(broker, "_invoke", invoke)
    if expected is None:
        with pytest.raises(gateway.GatewayLifecycleError):
            broker.fetch("packages")
    else:
        assert broker.fetch("packages") == expected
    if expected != "new":
        assert broker.cache.get("packages") == (
            ("cached", cached_expiry) if cached_expiry is not None else None
        )


def test_broker_correlates_concurrent_requests(tmp_path) -> None:
    helper = tmp_path / "credential-helper"
    helper.write_text("#!/bin/sh\nprintf 'token\\n'\n")
    helper.chmod(0o700)
    config = credential_config("command")
    broker = gateway_supervisor.CredentialBroker(
        config.credentials,
        {
            "baseline": {"HOME": str(tmp_path), "PATH": str(tmp_path), "TZ": "UTC"},
            "inherited": {},
        },
        tmp_path,
    )
    broker.describe()
    server, client = socket.socketpair()
    thread = threading.Thread(target=broker.serve, args=(server, "incarnation"))
    thread.start()
    try:
        gateway_supervisor.send_frame(
            client,
            {
                "version": 1,
                "request_id": "one",
                "incarnation": "incarnation",
                "credential": "packages",
            },
        )
        response = gateway_supervisor.recv_frame(client, 10)
        assert response == {
            "version": 1,
            "request_id": "one",
            "incarnation": "incarnation",
            "value": "token",
        }
        gateway_supervisor.send_frame(
            client,
            {
                "version": 1,
                "request_id": "one",
                "incarnation": "incarnation",
                "credential": "packages",
            },
        )
        with pytest.raises(gateway.GatewayLifecycleError, match="closed"):
            gateway_supervisor.recv_frame(client, 2)
    finally:
        client.close()
        thread.join(2)


@pytest.mark.parametrize("streams", ["stdout", "stderr", "combined"])
def test_supervisor_bounds_provider_output_and_kills_descendants(tmp_path, monkeypatch, streams):
    config = credential_config("command")
    broker = gateway_supervisor.CredentialBroker(
        config.credentials,
        {"baseline": {"HOME": str(tmp_path), "PATH": os.defpath, "TZ": "UTC"}, "inherited": {}},
        tmp_path,
    )
    monkeypatch.setattr(gateway_supervisor, "PROVIDER_TIMEOUT_SECONDS", 2)
    size = gateway_supervisor.PROVIDER_OUTPUT_BYTES
    writes = {
        "stdout": f"os.write(1, b'x' * {size + 1})",
        "stderr": f"os.write(2, b'x' * {size + 1})",
        "combined": f"os.write(1, b'x' * {size // 2}); os.write(2, b'x' * {size // 2 + 1})",
    }
    script = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('descendant.pid', 'w').write(str(child.pid))\n"
        f"{writes[streams]}\n"
        "time.sleep(60)\n"
    )
    popen = subprocess.Popen
    processes = []

    def noisy_helper(_command, **kwargs):
        process = popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(gateway_supervisor.subprocess, "Popen", noisy_helper)
    with pytest.raises(gateway.GatewayLifecycleError) as failed:
        broker._invoke(config.credentials[0], "fetch")
    assert type(failed.value.__cause__) is ValueError
    assert processes[0].poll() is not None
    descendant = int((tmp_path / "descendant.pid").read_text())
    deadline = time.monotonic() + 2
    while gateway._pid_is_running(descendant):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    for _ in range(gateway_supervisor.PROVIDER_CONCURRENCY):
        assert broker.slots.acquire(blocking=False)
    for _ in range(gateway_supervisor.PROVIDER_CONCURRENCY):
        broker.slots.release()


def test_bounded_provider_communication_transfers_large_input():
    payload = b"x" * (128 * 1024)
    prefix = b"prefix" * 16384
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'prefix' * 16384); sys.stdout.buffer.flush(); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = gateway_credentials.communicate_bounded(
        process, input=payload, timeout=2, maximum=len(prefix) + len(payload)
    )
    assert result.stdout == prefix + payload and result.stderr == b"" and result.returncode == 0
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed


def test_bounded_provider_execution_covers_success_limits_and_timeout() -> None:
    result = gateway_credentials.run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('value')"]
    )
    assert result.stdout == b"value" and result.returncode == 0

    with pytest.raises(ValueError, match="too large"):
        gateway_credentials.run_bounded([sys.executable, "-c", "print('12345', end='')"], maximum=4)
    with pytest.raises(subprocess.TimeoutExpired):
        gateway_credentials.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.01
        )


def test_codeartifact_method_validates_describes_and_fetches(monkeypatch) -> None:
    provider = gateway_credentials.aws_codeartifact()
    options = {
        "domain": "domain",
        "domain_owner": "123456789012",
        "region": "us-east-1",
        "profile": "developer",
    }
    assert provider.describe("credential", options) == {
        "header": {"name": "Authorization", "format": "basic-password", "username": "aws"}
    }
    captured: list[str] = []

    def run(argv, **_kwargs):
        captured.extend(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {"authorizationToken": "token", "expiration": "2030-01-02T03:04:05Z"}
            ).encode(),
            b"discarded secret stderr",
        )

    monkeypatch.setattr(gateway_credentials, "run_bounded", run)
    fetched = provider.fetch("credential", options)
    assert fetched["value"] == "token"
    assert fetched["expires_at"] == gateway_credentials._expiration("2030-01-02T03:04:05Z")
    assert captured == [
        "aws",
        "--profile",
        "developer",
        "codeartifact",
        "get-authorization-token",
        "--domain",
        "domain",
        "--domain-owner",
        "123456789012",
        "--region",
        "us-east-1",
        "--output",
        "json",
    ]
    assert gateway_credentials._expiration(12) == 12.0

    for invalid in ({}, {"domain": "d", "domain_owner": "o", "region": "r", "extra": 1}):
        with pytest.raises(ValueError, match="options"):
            provider.describe("credential", invalid)
    with pytest.raises(ValueError, match="profile"):
        provider.describe(
            "credential", {"domain": "d", "domain_owner": "o", "region": "r", "profile": ""}
        )
    with pytest.raises(ValueError, match="expiration"):
        gateway_credentials._expiration(None)

    monkeypatch.setattr(
        gateway_credentials,
        "run_bounded",
        lambda argv: subprocess.CompletedProcess(argv, 1, b"", b"secret"),
    )
    with pytest.raises(RuntimeError, match="token request failed"):
        provider.fetch("credential", {"domain": "d", "domain_owner": "o", "region": "r"})


def test_provider_helper_command_and_plugin_boundaries(monkeypatch) -> None:
    assert gateway_provider_helper._command("describe", {}) == {"header": None}

    def completed(stdout: bytes, returncode: int = 0):
        return subprocess.CompletedProcess(["helper"], returncode, stdout, b"secret stderr")

    monkeypatch.setattr(gateway_provider_helper, "run_bounded", lambda _argv: completed(b"token\n"))
    assert gateway_provider_helper._command("fetch", {"argv": ["helper"], "format": "text"}) == {
        "value": "token",
        "expires_at": None,
    }
    monkeypatch.setattr(
        gateway_provider_helper,
        "run_bounded",
        lambda _argv: completed(b'{"value":"token","expires_at":null}'),
    )
    assert gateway_provider_helper._command("fetch", {"argv": ["helper"], "format": "json"}) == {
        "value": "token",
        "expires_at": None,
    }

    monkeypatch.setattr(gateway_provider_helper, "run_bounded", lambda _argv: completed(b"", 1))
    with pytest.raises(RuntimeError, match="command failed"):
        gateway_provider_helper._command("fetch", {"argv": ["helper"], "format": "text"})
    monkeypatch.setattr(gateway_provider_helper, "run_bounded", lambda _argv: completed(b"a\nb"))
    with pytest.raises(ValueError, match="invalid"):
        gateway_provider_helper._command("fetch", {"argv": ["helper"], "format": "text"})
    monkeypatch.setattr(gateway_provider_helper, "run_bounded", lambda _argv: completed(b"[]"))
    with pytest.raises(ValueError, match="invalid"):
        gateway_provider_helper._command("fetch", {"argv": ["helper"], "format": "json"})

    class Provider:
        def describe(self, credential_id, options):
            return {"credential_id": credential_id, "options": options}

    class EntryPoint:
        def load(self):
            return lambda: Provider()

    class EntryPoints(tuple):
        def select(self, **_kwargs):
            return self

    monkeypatch.setattr(
        gateway_provider_helper.importlib.metadata,
        "entry_points",
        lambda **_kwargs: EntryPoints((EntryPoint(),)),
    )
    assert gateway_provider_helper._plugin(
        "method", "describe", {"credential_id": "id", "options": {"x": 1}}
    ) == {"credential_id": "id", "options": {"x": 1}}
    monkeypatch.setattr(
        gateway_provider_helper.importlib.metadata,
        "entry_points",
        lambda **_kwargs: EntryPoints(()),
    )
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        gateway_provider_helper._plugin(
            "method", "describe", {"credential_id": "id", "options": {}}
        )


def test_provider_helper_main_reports_only_generic_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["helper", "--method", "bad", "--operation", "fetch", "--lifetime-fd", "0"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(
        gateway_provider_helper.threading,
        "Thread",
        lambda **_kwargs: type("T", (), {"start": lambda self: None})(),
    )
    with pytest.raises(SystemExit) as error:
        gateway_provider_helper.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == "credential provider failed\n"
