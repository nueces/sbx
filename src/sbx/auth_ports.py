"""Resolve forwarding hints without reading or changing agent model configuration."""

import argparse
from collections.abc import Mapping
from typing import Any

from sbx.runtime import ConfigError

PROFILES = ("auto", "openai", "anthropic", "both", "none")
PROVIDERS = ("openai", "anthropic")
PORTS = {"openai": 1455, "anthropic": 53692}


def add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auth-port",
        nargs="?",
        const="auto",
        choices=PROFILES,
        help="OAuth callback forwarding profile (bare flag: auto).",
    )
    parser.add_argument(
        "--auth-provider",
        choices=PROVIDERS,
        help="Forwarding hint only; does not configure the agent's model.",
    )


def resolve(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    agent: str,
) -> tuple[bool | None, list[tuple[int, int]]]:
    sbx = config.get("sbx", {})

    def value(attr: str, key: str | None = None) -> Any:
        cli = getattr(args, attr, None)
        return cli if cli is not None else sbx.get(key or attr)

    profile = value("auth_port")
    enabled = None if profile is None else True
    if profile is None:
        profile = "auto"
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ConfigError("auth_port must be one of: " + ", ".join(PROFILES))
    provider = value("auth_provider")
    if provider is not None:
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ConfigError("auth_provider must be openai or anthropic")
        fixed = {"claude": "anthropic", "codex": "openai"}.get(agent)
        if fixed is not None and provider != fixed:
            raise ConfigError(f"auth_provider {provider!r} is incompatible with agent {agent!r}")
    if profile == "auto":
        profile = provider or {"claude": "anthropic", "codex": "openai"}.get(agent, "both")

    host = value("host_port", "auth_host_port")
    guest = value("guest_port", "auth_guest_port")
    for port in (host, guest):
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ConfigError("auth ports must be integers between 1 and 65535")
    if profile == "both" and (host is not None or guest is not None):
        raise ConfigError("numeric auth port overrides require a single profile, not both")
    if profile == "none":
        return False, []
    defaults = list(PORTS.values()) if profile == "both" else [PORTS[profile]]
    return enabled, [(host or port, guest or port) for port in defaults]
