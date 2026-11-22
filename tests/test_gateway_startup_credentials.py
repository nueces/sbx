import subprocess
from dataclasses import replace

import pytest

from sbx import gateway, gateway_provider_helper, gateway_supervisor
from sbx.runtime import ConfigError
from test_gateway_credentials import credential_config


@pytest.mark.parametrize("fetch", ["lazy", "startup"])
def test_startup_fetch_schema_roundtrip_and_digest(fetch):
    config = gateway.parse_config(
        {
            "network": {
                "credential": [
                    {
                        "id": "token",
                        "environment": "TOKEN",
                        "method": "command",
                        "options": {"argv": ["credential-helper"]},
                        "fetch": fetch,
                    }
                ]
            }
        }
    )
    assert config.credentials[0].fetch == fetch
    assert (
        gateway_supervisor.config_from_payload(gateway_supervisor.config_payload(config)) == config
    )
    canonical = config.canonical("vm1", "generation1")
    if fetch == "startup":
        assert canonical["credentials"][0]["fetch"] == "startup"
    else:
        assert "fetch" not in canonical["credentials"][0]
        legacy_payload = gateway_supervisor.config_payload(config)
        del legacy_payload["credentials"][0]["fetch"]
        assert gateway_supervisor.config_from_payload(legacy_payload) == config
    other_fetch = "lazy" if fetch == "startup" else "startup"
    other = replace(config, credentials=(replace(config.credentials[0], fetch=other_fetch),))
    assert config.digest("vm1", "generation1") != other.digest("vm1", "generation1")
    assert credential_config("command").credentials[0].fetch == "lazy"


@pytest.mark.parametrize(
    "invalid",
    [0, 1, True, False, "true", "", "STARTUP", "manual", None, [], {}, ["startup", "lazy"]],
)
def test_fetch_requires_a_known_mode(invalid):
    with pytest.raises(ConfigError, match='fetch must be "startup" or "lazy"'):
        gateway.parse_config(
            {
                "network": {
                    "credential": [
                        {
                            "id": "token",
                            "environment": "TOKEN",
                            "method": "command",
                            "options": {"argv": ["credential-helper"]},
                            "fetch": invalid,
                        }
                    ]
                }
            }
        )


@pytest.mark.parametrize("fetch", ["lazy", "startup"])
def test_startup_fetch_executes_once_and_reuses_cache(tmp_path, monkeypatch, fetch):
    helper = tmp_path / "credential-helper"
    helper.write_text("#!/bin/sh\nprintf x >> calls\nprintf 'fake-startup-token\\n'\n")
    helper.chmod(0o700)
    credential = replace(credential_config("command").credentials[0], fetch=fetch)
    unbound = replace(credential, id="unused", bindings=(), fetch="startup")
    broker = gateway_supervisor.CredentialBroker(
        (credential, unbound),
        {"baseline": {"HOME": str(tmp_path), "PATH": str(tmp_path), "TZ": "UTC"}, "inherited": {}},
        tmp_path,
    )
    broker.describe()
    assert not (tmp_path / "calls").exists()
    if fetch == "startup":
        monkeypatch.setattr(gateway_supervisor, "PROVIDER_TIMEOUT_SECONDS", 0.01)
    broker.prefetch()
    assert (tmp_path / "calls").exists() is (fetch == "startup")
    assert "unused" not in broker.cache
    assert broker.fetch("packages") == broker.fetch("packages") == "fake-startup-token"
    assert (tmp_path / "calls").read_text() == "x"


def test_startup_fetch_has_auth_budget_but_refresh_uses_normal_budget(tmp_path, monkeypatch):
    credential = replace(credential_config("command").credentials[0], fetch="startup")
    now = [1000.0]
    broker = gateway_supervisor.CredentialBroker((credential,), {}, tmp_path, clock=lambda: now[0])
    calls = []

    def invoke(selected, operation, **kwargs):
        calls.append((selected.id, operation, kwargs.get("timeout")))
        return {"value": "fake-token", "expires_at": now[0] + 120}

    monkeypatch.setattr(broker, "_invoke", invoke)
    broker.prefetch()
    assert calls == [("packages", "fetch", gateway_supervisor.STARTUP_PROVIDER_TIMEOUT_SECONDS)]
    assert broker.fetch("packages") == "fake-token" and len(calls) == 1
    now[0] += 100
    assert broker.fetch("packages") == "fake-token"
    assert calls[-1] == ("packages", "fetch", None)


def test_startup_fetch_failure_is_redacted_and_not_cached(tmp_path, monkeypatch):
    credential = replace(credential_config("command").credentials[0], fetch="startup")
    broker = gateway_supervisor.CredentialBroker((credential,), {}, tmp_path)

    def fail(*args, **kwargs):
        raise gateway.GatewayLifecycleError("private-provider-output")

    monkeypatch.setattr(broker, "_invoke", fail)
    with pytest.raises(
        gateway.GatewayLifecycleError, match="startup credential 'packages'"
    ) as failure:
        broker.prefetch()
    assert "private-provider-output" not in str(failure.value)
    assert not broker.cache


def test_command_startup_budget_reaches_child_execution(monkeypatch):
    observed = []

    def run(argv, *, timeout):
        observed.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, b"fake-token\n", b"")

    monkeypatch.setattr(gateway_provider_helper, "run_bounded", run)
    assert gateway_provider_helper._command(
        "fetch", {"argv": ["op", "read", "fake-vault-item"], "format": "text", "timeout": 60}
    ) == {"value": "fake-token", "expires_at": None}
    assert observed == [(["op", "read", "fake-vault-item"], 60)]
