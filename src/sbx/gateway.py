from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import posixpath
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote

from sbx import guest_setup
from sbx.constants import GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES, SBX_STATE_DIR
from sbx.runtime import ConfigError, pid_is_alive

GATEWAY_STATE_DIR = SBX_STATE_DIR / "gateway"
GATEWAY_SOURCE_DIR = Path(__file__).resolve().parents[2] / "gateway"
GATEWAY_RUNTIME_DIR = Path(__file__).with_name("gateway_runtime")
GATEWAY_MEDIATOR_DIR = GATEWAY_RUNTIME_DIR / "mediator"
_GATEWAY_ARCH = platform.machine().lower().replace("x86_64", "amd64").replace("aarch64", "arm64")
GATEWAY_WORKER = GATEWAY_RUNTIME_DIR / "bin" / f"sbx-gateway-{sys.platform}-{_GATEWAY_ARCH}"
GATEWAY_DEV_WORKER = GATEWAY_SOURCE_DIR / "sbx-gateway"
GATEWAY_MEDIATOR_LAUNCHER = GATEWAY_MEDIATOR_DIR / "launch.py"
MAX_CONFIG_BYTES = 1 << 20
MAX_LOG_BYTES = 100 * 1024 * 1024
MAX_LOG_TOTAL_BYTES = MAX_LOG_BYTES
MAX_LOG_EVENTS_PER_SECOND = 1000
MAX_LOG_EVENT_BURST = 2000
MAX_LOG_EVENT_BYTES = 64 * 1024
MAX_AUDIT_EVENT_BYTES = 1 << 20
MAX_DNS_JOURNAL_BYTES = 1 << 20
MAX_DATA_PLANE_RSS = 512 * 1024 * 1024
MAX_DATA_PLANE_PROCESSES = 16
MAX_DATA_PLANE_THREADS = 256
GUEST_ADDRESS = "192.0.2.2"
APPROVED_HOOKS = frozenset({"package-audit"})
_ALLOWED_NETWORK_KEYS = frozenset(
    {"default", "mitmweb", "policy", "credential", "ingress", "quotas", "reconnection"}
)
_ALLOWED_FAILURES = frozenset(
    {
        "audit_failed",
        "cleanup_failed",
        "config_invalid",
        "mediator_failed",
        "policy_denied",
        "qemu_failed",
        "quota_exhausted",
        "reconnection_failed",
        "startup_failed",
        "state_unsafe",
        "worker_failed",
    }
)
_ACTIVE_STATES = frozenset({"starting", "ready", "recovering", "draining"})
_ALLOWED_STATES = _ACTIVE_STATES | {"prepared", "failed", "stopped"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class PolicyRule:
    id: str
    protocol: str
    host: str
    ports: tuple[int, ...]
    methods: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    allow_ranges: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    policy_id: str = ""


@dataclass(frozen=True)
class CredentialBinding:
    policies: tuple[str, ...]
    header: str | None = None
    methods: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Credential:
    id: str
    environment: str
    method: str
    options: Mapping[str, Any]
    inherit_environment: tuple[str, ...]
    bindings: tuple[CredentialBinding, ...]
    header: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IngressRule:
    id: str
    listen_host: str
    host_port: int
    guest_address: str
    guest_port: int
    source: str = "network"


@dataclass(frozen=True)
class Quotas:
    tcp_flows: int = 512
    flows_per_destination: int = 64
    ingress_flows: int = 64
    dns_mappings: int = 1024
    open_files: int = 4096


@dataclass(frozen=True)
class Reconnection:
    enabled: bool = True
    attempts: int = 1
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class GatewayConfig:
    rules: tuple[PolicyRule, ...]
    credentials: tuple[Credential, ...]
    ingress: tuple[IngressRule, ...]
    quotas: Quotas
    reconnection: Reconnection
    platform: str
    mitmweb: bool = False

    def canonical(self, vm_id: str, generation: str) -> dict[str, Any]:
        _validate_id(vm_id, "VM identity")
        _validate_id(generation, "gateway generation")
        return {
            "version": 1,
            "vm_id": vm_id,
            "generation": generation,
            "default": "deny",
            "platform": self.platform,
            "mitmweb": self.mitmweb,
            "rules": [asdict(rule) for rule in self.rules],
            "credentials": [asdict(credential) for credential in self.credentials],
            "ingress": [asdict(rule) for rule in self.ingress if rule.source != "controller"],
            "quotas": asdict(self.quotas),
            "reconnection": asdict(self.reconnection),
        }

    def digest(self, vm_id: str, generation: str) -> str:
        encoded = json.dumps(
            self.canonical(vm_id, generation), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def worker_config(self, vm_id: str, generation: str) -> dict[str, Any]:
        _validate_id(vm_id, "VM identity")
        _validate_id(generation, "gateway generation")
        rules = []
        for rule in self.rules:
            item: dict[str, Any] = {
                "id": rule.id,
                "protocol": rule.protocol,
                "host": rule.host,
                "ports": list(rule.ports),
            }
            if rule.methods:
                item["methods"] = list(rule.methods)
            if rule.paths:
                item["paths"] = list(rule.paths)
            if rule.allow_ranges:
                item["allow_ranges"] = list(rule.allow_ranges)
            rules.append(item)
        data: dict[str, Any] = {
            "version": 1,
            "vm_id": vm_id,
            "generation": generation,
            "rules": rules,
            "ingress": [
                {
                    "id": rule.id,
                    "listen_host": rule.listen_host,
                    "host_port": rule.host_port,
                    "guest_address": rule.guest_address,
                    "guest_port": rule.guest_port,
                }
                for rule in sorted(self.ingress, key=lambda rule: rule.id)
            ],
            "quotas": asdict(self.quotas),
        }
        digest = hashlib.sha256(_compact_json(data)).hexdigest()
        return {**dict(list(data.items())[:3]), "digest": digest, **dict(list(data.items())[3:])}


class GatewayStateError(RuntimeError):
    pass


def _compact_json(value: object) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        encoded = encoded.replace(character, escape)
    return encoded.encode()


def _validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ConfigError(f"invalid {label}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a TOML table")
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _tables(value: object, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ConfigError(f"{label} must be an array of TOML tables")
    return list(value)


def _strings(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if (
        not isinstance(value, list)
        or (required and not value)
        or not all(isinstance(item, str) for item in value)
    ):
        raise ConfigError(f"{label} must be an array of strings")
    return tuple(value)


def _ports(value: object, label: str, *, allow_zero: bool = False) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty array of ports")
    minimum = 0 if allow_zero else 1
    if any(type(port) is not int or not minimum <= port <= 65535 for port in value):
        raise ConfigError(f"{label} ports must be between {minimum} and 65535")
    if len(set(value)) != len(value):
        raise ConfigError(f"{label} contains a duplicate port")
    return tuple(sorted(value))


def _port(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= 65535:
        raise ConfigError(f"{label} must be between {minimum} and 65535")
    return value


def _normalize_host(value: object, label: str, *, exact: bool = False) -> str:
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise ConfigError(f"{label} must be a hostname")
    host = value.strip().rstrip(".").lower()
    wildcard = host.startswith("*.")
    if wildcard:
        if exact:
            raise ConfigError(f"{label} requires an exact hostname")
        host = host[2:]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigError(f"{label} is invalid") from exc
        labels = host.split(".")
        if (
            not host
            or len(host) > 253
            or any(
                not part
                or len(part) > 63
                or part.startswith("-")
                or part.endswith("-")
                or re.fullmatch(r"[a-z0-9-]+", part) is None
                for part in labels
            )
        ):
            raise ConfigError(f"{label} is invalid") from None
        if wildcard and len(labels) < 2:
            raise ConfigError(f"{label} wildcard is too broad") from None
        return f"*.{host}" if wildcard else host
    if wildcard or exact:
        raise ConfigError(f"{label} requires an exact DNS hostname")
    return address.compressed


def _host_pattern_matches(pattern: str, host: str) -> bool:
    suffix = pattern[1:]
    prefix = host.removesuffix(suffix)
    return host.endswith(suffix) and bool(prefix) and "." not in prefix


def _path_subset(path: str, parent: str) -> bool:
    base = parent.rstrip("/") or "/"
    return base == "/" or path == base or path.startswith(base + "/")


def _path_scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return True
    return any(_path_subset(a, b) or _path_subset(b, a) for a in left for b in right)


def _methods(value: object, label: str) -> tuple[str, ...]:
    methods = _strings(value, label) if value is not None else ()
    normalized = tuple(sorted({method.upper() for method in methods}))
    if any(_TOKEN_RE.fullmatch(method) is None for method in normalized):
        raise ConfigError(f"{label} contains an invalid HTTP method")
    return normalized


def canonical_path(value: str, label: str = "path") -> str:
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
        raise ConfigError(f"{label} contains an unsafe path")
    path = value
    for _ in range(4):
        if re.search(r"%(?![0-9A-Fa-f]{2})", path):
            raise ConfigError(f"{label} contains an unsafe path")
        try:
            decoded = unquote(path, errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConfigError(f"{label} contains an unsafe path") from exc
        if decoded == path:
            break
        path = decoded
    else:
        raise ConfigError(f"{label} exceeds the percent-decoding limit")
    if (
        "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ConfigError(f"{label} contains an unsafe path")
    return posixpath.normpath(path)


def _paths(value: object, label: str) -> tuple[str, ...]:
    paths = _strings(value, label) if value is not None else ()
    return tuple(sorted({canonical_path(path, label) for path in paths}))


def _hooks(value: object, label: str) -> tuple[str, ...]:
    hooks = _strings(value, label) if value is not None else ()
    if len(set(hooks)) != len(hooks):
        raise ConfigError(f"{label} contains a duplicate hook")
    unknown = sorted(set(hooks) - APPROVED_HOOKS)
    if unknown:
        raise ConfigError(f"{label} names unapproved hook(s): {', '.join(unknown)}")
    return hooks


def validate_header_descriptor(value: object, *, allow_none: bool = False) -> dict[str, Any] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise ConfigError("credential header descriptor is invalid")
    name = value["name"]
    if _TOKEN_RE.fullmatch(name) is None:
        raise ConfigError("credential header descriptor is invalid")
    if value.get("format") == "template" and set(value) == {"name", "format", "value"}:
        template = value.get("value")
        if not isinstance(template, str) or template.count("{placeholder}") != 1:
            raise ConfigError("credential header descriptor is invalid")
        _header(f"{name}: {template}", "credential header descriptor")
        return {"name": name, "format": "template", "value": template}
    if value.get("format") == "basic-password" and set(value) == {"name", "format", "username"}:
        username = value.get("username")
        if not isinstance(username, str) or not username or any(c in username for c in ":\r\n\0"):
            raise ConfigError("credential header descriptor is invalid")
        return {"name": name, "format": "basic-password", "username": username}
    raise ConfigError("credential header descriptor is invalid")


def _header(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value.count("{placeholder}") != 1
        or ":" not in value
        or any(character in value for character in "\r\n\x00")
        or len(value.encode()) > 8192
    ):
        raise ConfigError(f"{label} must contain one structural {{placeholder}}")
    name, template = (part.strip() for part in value.split(":", 1))
    if _TOKEN_RE.fullmatch(name) is None or not template:
        raise ConfigError(f"{label} has an invalid field name or value")
    return f"{name}: {template}"


def _policy(item: Mapping[str, Any], index: int) -> list[PolicyRule]:
    label = "[[network.policy]]"
    _reject_unknown(
        item,
        {"id", "mode", "hosts", "ports", "methods", "path_prefixes", "hooks", "allow_ranges"},
        label,
    )
    policy_id = item.get("id")
    mode = item.get("mode")
    if not isinstance(policy_id, str) or _RULE_ID_RE.fullmatch(policy_id) is None:
        raise ConfigError(f"{label}.id is invalid")
    protocols = {"http-mediated": "http", "tls-passthrough": "tls", "tcp-forward": "tcp"}
    if mode not in protocols:
        raise ConfigError(f"{label}.mode must be http-mediated, tls-passthrough, or tcp-forward")
    protocol = protocols[mode]
    hosts = tuple(
        _normalize_host(host, f"{label}.hosts", exact=protocol == "tls")
        for host in _strings(item.get("hosts"), f"{label}.hosts", required=True)
    )
    if len(set(hosts)) != len(hosts):
        raise ConfigError(f"{label}.hosts contains a duplicate hostname")
    ports = _ports(item.get("ports"), f"{label}.ports")
    if protocol != "http" and any(key in item for key in ("methods", "path_prefixes", "hooks")):
        raise ConfigError(
            f"{label} permits methods, path_prefixes, and hooks only in http-mediated mode"
        )
    if protocol == "http" and "allow_ranges" in item:
        raise ConfigError(f"{label}.allow_ranges is not permitted in http-mediated mode")
    methods = _methods(item.get("methods"), f"{label}.methods") if protocol == "http" else ()
    paths = (
        _paths(item.get("path_prefixes"), f"{label}.path_prefixes") if protocol == "http" else ()
    )
    hooks = _hooks(item.get("hooks"), f"{label}.hooks") if protocol == "http" else ()
    ranges = (
        _strings(item.get("allow_ranges"), f"{label}.allow_ranges")
        if "allow_ranges" in item
        else ()
    )
    normalized_ranges = []
    for value in ranges:
        try:
            normalized_ranges.append(str(ipaddress.ip_network(value, strict=False)))
        except ValueError as exc:
            raise ConfigError(f"{label}.allow_ranges contains an invalid network") from exc
    if len(set(normalized_ranges)) != len(normalized_ranges):
        raise ConfigError(f"{label}.allow_ranges contains a duplicate network")
    return [
        PolicyRule(
            policy_id if len(hosts) == 1 else f"{policy_id}-{host_index}",
            protocol,
            host,
            ports,
            methods,
            paths,
            tuple(sorted(normalized_ranges)),
            hooks,
            policy_id,
        )
        for host_index, host in enumerate(hosts, 1)
    ]


def _binding(item: Mapping[str, Any], label: str) -> CredentialBinding:
    _reject_unknown(item, {"policies", "header", "methods", "path_prefixes"}, label)
    policies = _strings(item.get("policies"), f"{label}.policies", required=True)
    if len(set(policies)) != len(policies) or any(
        _RULE_ID_RE.fullmatch(value) is None for value in policies
    ):
        raise ConfigError(f"{label}.policies contains an invalid or duplicate policy ID")
    return CredentialBinding(
        tuple(policies),
        _header(item["header"], f"{label}.header") if "header" in item else None,
        _methods(item.get("methods"), f"{label}.methods"),
        _paths(item.get("path_prefixes"), f"{label}.path_prefixes"),
    )


def _credential(item: Mapping[str, Any], index: int) -> Credential:
    label = "[[network.credential]]"
    _reject_unknown(item, {"id", "environment", "method", "options", "bindings"}, label)
    credential_id = item.get("id")
    environment = item.get("environment")
    method = item.get("method")
    if not isinstance(credential_id, str) or _RULE_ID_RE.fullmatch(credential_id) is None:
        raise ConfigError(f"{label}.id is invalid")
    if not isinstance(environment, str) or _ENV_RE.fullmatch(environment) is None:
        raise ConfigError(f"{label}.environment is invalid")
    if method != "command" and not (
        isinstance(method, str)
        and method.startswith("plugin:")
        and _RULE_ID_RE.fullmatch(method[7:])
    ):
        raise ConfigError(f"{label}.method must be command or plugin:NAME")
    options = dict(_mapping(item.get("options", {}), f"{label}.options"))
    inherited = options.pop("inherit_environment", [])
    if (
        not isinstance(inherited, list)
        or not all(isinstance(name, str) and _ENV_RE.fullmatch(name) for name in inherited)
        or len(set(inherited)) != len(inherited)
    ):
        raise ConfigError(
            f"{label}.options.inherit_environment must contain unique environment names"
        )
    if method == "command":
        _reject_unknown(options, {"argv", "format"}, f"{label}.options")
        argv = options.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv)
        ):
            raise ConfigError(f"{label}.options.argv must be a non-empty argv array")
        if options.get("format", "text") not in {"text", "json"}:
            raise ConfigError(f"{label}.options.format must be text or json")
        options.setdefault("format", "text")
    bindings = tuple(
        _binding(binding, f"{label}.bindings")
        for binding in _tables(item.get("bindings"), f"{label}.bindings")
    )
    if method == "command" and any(binding.header is None for binding in bindings):
        raise ConfigError("command credential bindings require an explicit header")
    return Credential(credential_id, environment, str(method), options, tuple(inherited), bindings)


def _listen(value: object, label: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be ADDRESS:PORT")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)]:(\d+)", value)
        if match is None:
            raise ConfigError(f"{label} must be ADDRESS:PORT")
        host, raw_port = match.groups()
    else:
        try:
            host, raw_port = value.rsplit(":", 1)
        except ValueError as exc:
            raise ConfigError(f"{label} must be ADDRESS:PORT") from exc
    if host == "localhost":
        host = "127.0.0.1"
    try:
        host = ipaddress.ip_address(host).compressed
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"{label} must contain an IP address and numeric port") from exc
    return host, _port(port, label, allow_zero=True)


def _ingress(item: Mapping[str, Any], index: int) -> IngressRule:
    label = "[[network.ingress]]"
    _reject_unknown(item, {"id", "listen", "guest_port"}, label)
    rule_id = item.get("id", f"ingress-{index}")
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise ConfigError(f"{label}.id is invalid")
    host, host_port = _listen(item.get("listen", "127.0.0.1:0"), f"{label}.listen")
    return IngressRule(
        rule_id,
        host,
        host_port,
        GUEST_ADDRESS,
        _port(item.get("guest_port"), f"{label}.guest_port"),
    )


def _quota_int(table: Mapping[str, Any], key: str, default: int, minimum: int = 1) -> int:
    value = table.get(key, default)
    if type(value) is not int or not minimum <= value <= default:
        raise ConfigError(f"[network.quotas].{key} must be between {minimum} and {default}")
    return value


def _quotas(value: object) -> Quotas:
    if value is None:
        return Quotas()
    table = _mapping(value, "[network.quotas]")
    _reject_unknown(
        table,
        {
            "tcp_flows",
            "flows_per_destination",
            "ingress_flows",
            "dns_mappings",
            "open_files",
        },
        "[network.quotas]",
    )
    return Quotas(
        _quota_int(table, "tcp_flows", 512),
        _quota_int(table, "flows_per_destination", 64),
        _quota_int(table, "ingress_flows", 64),
        _quota_int(table, "dns_mappings", 1024),
        _quota_int(table, "open_files", 4096, 64),
    )


def _reconnection(value: object) -> Reconnection:
    if value is None:
        return Reconnection()
    table = _mapping(value, "[network.reconnection]")
    _reject_unknown(table, {"enabled", "attempts", "timeout_seconds"}, "[network.reconnection]")
    enabled = table.get("enabled", True)
    attempts = table.get("attempts", 1 if enabled else 0)
    timeout = table.get("timeout_seconds", 10.0)
    if type(enabled) is not bool:
        raise ConfigError("[network.reconnection].enabled must be a boolean")
    if (
        type(attempts) is not int
        or not 0 <= attempts <= 3
        or (enabled and attempts < 1)
        or (not enabled and attempts != 0)
    ):
        raise ConfigError(
            "[network.reconnection].attempts must be 1-3 when enabled and 0 when disabled"
        )
    if type(timeout) not in {int, float} or not 0.1 <= float(timeout) <= 60:
        raise ConfigError("[network.reconnection].timeout_seconds must be between 0.1 and 60")
    return Reconnection(enabled, attempts, float(timeout))


def _validate_port(value: str, spec: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigError(f"invalid port forward {spec!r}: ports must be numbers") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"invalid port forward {spec!r}: ports must be between 1 and 65535")
    return port


def parse_port_forward(spec: str) -> tuple[str, int, int]:
    parts = spec.split(":")
    if len(parts) == 1:
        port = _validate_port(parts[0], spec)
        return "127.0.0.1", port, port
    if len(parts) in {2, 3}:
        host = "127.0.0.1" if len(parts) == 2 else parts[0]
        if not host:
            raise ConfigError(f"invalid port forward {spec!r}: host address is empty")
        host_port, guest_port = (_validate_port(part, spec) for part in parts[-2:])
        return host, host_port, guest_port
    raise ConfigError(
        f"invalid port forward {spec!r}: use GUEST_PORT, HOST_PORT:GUEST_PORT, "
        "or BIND_HOST:HOST_PORT:GUEST_PORT"
    )


def port_forwards_from_specs(specs: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"host_address": host, "host_port": host_port, "guest_port": guest_port}
        for host, host_port, guest_port in (parse_port_forward(spec) for spec in specs)
    ]


def parse_config(config: Mapping[str, Any], *, platform: str | None = None) -> GatewayConfig | None:
    if "network" not in config:
        return None
    selected_platform = sys.platform if platform is None else platform
    if selected_platform not in {"linux", "darwin"}:
        raise ConfigError("gateway networking is supported only on Linux and unverified macOS")
    network = _mapping(config["network"], "[network]")
    _reject_unknown(network, _ALLOWED_NETWORK_KEYS, "[network]")
    if network.get("default", "deny") != "deny":
        raise ConfigError("[network].default must be 'deny'")

    mitmweb = network.get("mitmweb", False)
    if type(mitmweb) is not bool:
        raise ConfigError("[network].mitmweb must be a boolean")

    policy_tables = _tables(network.get("policy"), "[[network.policy]]")
    public_ids = [item.get("id") for item in policy_tables]
    if len(set(public_ids)) != len(public_ids):
        raise ConfigError("network policy IDs must be unique")
    rules = tuple(
        rule for index, item in enumerate(policy_tables, 1) for rule in _policy(item, index)
    )
    if len({rule.id for rule in rules}) != len(rules):
        raise ConfigError("normalized network policy rule IDs must be unique")

    def overlaps(left: str, right: str) -> bool:
        if left == right:
            return True
        if left.startswith("*."):
            return _host_pattern_matches(left, right)
        if right.startswith("*."):
            return _host_pattern_matches(right, left)
        return False

    for index, rule in enumerate(rules):
        for other in rules[index + 1 :]:
            # Every supported public policy mode uses TCP, regardless of mediation.
            if set(rule.ports) & set(other.ports) and overlaps(rule.host, other.host):
                raise ConfigError(
                    f"network policies {rule.policy_id!r} and {other.policy_id!r} overlap"
                )

    credentials = tuple(
        _credential(item, index)
        for index, item in enumerate(
            _tables(network.get("credential"), "[[network.credential]]"), 1
        )
    )
    ids = [credential.id for credential in credentials]
    environments = [credential.environment for credential in credentials]
    if len(set(ids)) != len(ids):
        raise ConfigError("network credential IDs must be unique")
    if len(set(environments)) != len(environments):
        raise ConfigError("network credential environment names must be unique")
    forwarded = (
        config.get("sbx", {}).get("env", []) if isinstance(config.get("sbx", {}), Mapping) else []
    )
    if isinstance(forwarded, str):
        forwarded = [forwarded]
    if not isinstance(forwarded, list) or not all(isinstance(entry, str) for entry in forwarded):
        raise ConfigError("[sbx].env must contain environment selections")
    guest_setup.validate_env_entries(forwarded)
    forwarded_names = {entry.partition("=")[0] for entry in forwarded}
    collisions = set(environments) & forwarded_names
    if (
        collisions
        or "SBX_NETWORK_CREDENTIAL_NAMES" in environments
        or "SBX_NETWORK_CREDENTIAL_NAMES" in forwarded_names
    ):
        raise ConfigError(
            "network credential environment collides with forwarded or reserved names"
        )
    policies = {rule.policy_id: rule for rule in rules}
    credential_policies: set[str] = set()
    scopes: list[tuple[str, str, frozenset[str], tuple[str, ...], str]] = []
    for credential in credentials:
        for binding in credential.bindings:
            for policy_id in binding.policies:
                policy = policies.get(policy_id)
                if policy is None:
                    raise ConfigError(
                        f"credential {credential.id!r} references unknown policy {policy_id!r}"
                    )
                if policy.protocol != "http":
                    raise ConfigError("credential bindings require http-mediated policies")
                if (
                    binding.methods
                    and policy.methods
                    and not set(binding.methods) <= set(policy.methods)
                ):
                    raise ConfigError(
                        "credential binding methods must be a subset of policy methods"
                    )
                if (
                    binding.paths
                    and policy.paths
                    and any(
                        not any(_path_subset(path, parent) for parent in policy.paths)
                        for path in binding.paths
                    )
                ):
                    raise ConfigError("credential binding paths must be a subset of policy paths")
                credential_policies.add(policy_id)
                if binding.header is not None:
                    header_name = binding.header.split(":", 1)[0].lower()
                    methods = frozenset(binding.methods or policy.methods)
                    paths = binding.paths or policy.paths
                    for old_policy, old_header, old_methods, old_paths, old_credential in scopes:
                        if (
                            old_policy == policy_id
                            and old_header == header_name
                            and (not old_methods or not methods or old_methods & methods)
                            and _path_scopes_overlap(old_paths, paths)
                        ):
                            raise ConfigError(
                                "credential bindings "
                                f"{old_credential!r} and {credential.id!r} overlap"
                            )
                    scopes.append((policy_id, header_name, methods, paths, credential.id))
    if any(rule.host.startswith("*.") and rule.policy_id in credential_policies for rule in rules):
        raise ConfigError("credential-bound policies require exact hosts")

    ingress = [
        _ingress(item, index)
        for index, item in enumerate(_tables(network.get("ingress"), "[[network.ingress]]"), 1)
    ]
    sbx = config.get("sbx", {})
    if not isinstance(sbx, Mapping):
        raise ConfigError("[sbx] must be a TOML table")
    legacy = sbx.get("port_forwards", [])
    if isinstance(legacy, str):
        legacy = [legacy]
    if not isinstance(legacy, list) or not all(isinstance(item, str) for item in legacy):
        raise ConfigError("[sbx].port_forwards must be a string or an array of strings")
    for index, spec in enumerate(legacy, 1):
        host, host_port, guest_port = parse_port_forward(spec)
        try:
            host = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host).compressed
        except ValueError as exc:
            raise ConfigError(
                f"invalid port forward {spec!r}: bind host must be an IP address"
            ) from exc
        ingress.append(
            IngressRule(
                f"legacy-{index}", host, host_port, GUEST_ADDRESS, guest_port, "sbx.port_forwards"
            )
        )
    ingress_ids = [rule.id for rule in ingress]
    if len(set(ingress_ids)) != len(ingress_ids):
        raise ConfigError("network ingress IDs must be unique")
    listeners: set[tuple[str, int]] = set()
    for rule in ingress:
        listener = (rule.listen_host, rule.host_port)
        if rule.host_port and listener in listeners:
            raise ConfigError(
                f"duplicate network ingress listener {rule.listen_host}:{rule.host_port}"
            )
        listeners.add(listener)

    quotas = _quotas(network.get("quotas"))
    if len(ingress) > quotas.ingress_flows or len(ingress) + 16 > quotas.open_files:
        raise ConfigError("network ingress listeners exceed the per-VM resource ceilings")
    return GatewayConfig(
        tuple(sorted(rules, key=lambda rule: rule.id)),
        tuple(sorted(credentials, key=lambda credential: credential.id)),
        tuple(ingress),
        quotas,
        _reconnection(network.get("reconnection")),
        selected_platform,
        mitmweb,
    )


parse_gateway_config = parse_config


def validate_runtime_paths(vm_id: str, *, runtime_root: Path | None = None) -> None:
    _validate_id(vm_id, "VM identity")
    _select_runtime_directory(uuid.UUID(int=0).hex, root=runtime_root)


def finalize_credentials(config: GatewayConfig, described: Sequence[Credential]) -> GatewayConfig:
    by_id = {credential.id: credential for credential in described}
    if set(by_id) != {credential.id for credential in config.credentials}:
        raise ConfigError("credential descriptions do not match configuration")
    policies = {rule.policy_id: rule for rule in config.rules}
    scopes: list[tuple[str, str, frozenset[str], tuple[str, ...], str]] = []
    for credential in described:
        for binding in credential.bindings:
            descriptor = (
                validate_header_descriptor(
                    {
                        "name": binding.header.split(":", 1)[0].strip(),
                        "format": "template",
                        "value": binding.header.split(":", 1)[1].strip(),
                    }
                )
                if binding.header is not None
                else credential.header
            )
            if descriptor is None:
                raise ConfigError(f"credential {credential.id!r} binding requires a header")
            header_name = str(descriptor["name"]).lower()
            for policy_id in binding.policies:
                policy = policies[policy_id]
                methods = frozenset(binding.methods or policy.methods)
                paths = binding.paths or policy.paths
                for old_policy, old_header, old_methods, old_paths, old_id in scopes:
                    if (
                        old_policy == policy_id
                        and old_header == header_name
                        and (not old_methods or not methods or old_methods & methods)
                        and _path_scopes_overlap(old_paths, paths)
                    ):
                        raise ConfigError(
                            f"credential bindings {old_id!r} and {credential.id!r} overlap"
                        )
                scopes.append((policy_id, header_name, methods, paths, credential.id))
    return replace(config, credentials=tuple(sorted(described, key=lambda item: item.id)))


def write_effective_policy(
    directory: Path, config: GatewayConfig, vm_id: str, generation: str
) -> None:
    state = _read_json(directory / "state.json")
    state["config_digest"] = config.digest(vm_id, generation)
    _atomic_json(directory, "state.json", state)


def _private_directory(path: Path, *, create: bool = False) -> None:
    try:
        if create:
            path.mkdir(mode=0o700)
        info = path.lstat()
    except FileExistsError:
        info = path.lstat()
    except OSError as exc:
        raise GatewayStateError(f"cannot create gateway state directory: {path}: {exc}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise GatewayStateError(f"unsafe gateway state directory: {path}")


def _reject_symlink_ancestors(path: Path) -> None:
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GatewayStateError(f"cannot inspect gateway path parent: {parent}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise GatewayStateError(f"unsafe gateway path parent: {parent}")


def _state_root(root: Path | None = None) -> Path:
    selected = GATEWAY_STATE_DIR if root is None else root
    parent = selected.parent
    _reject_symlink_ancestors(parent)
    parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(parent)
    _private_directory(selected, create=True)
    return selected


_SOCKET_NAMES = {"qemu": "qemu-net.sock", "control": "control.sock"}


def _compact_runtime_generation(generation: str) -> str:
    try:
        raw = uuid.UUID(hex=generation).bytes
    except ValueError:
        raw = hashlib.sha256(generation.encode()).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _runtime_directory_under(base: Path, generation: str) -> Path | None:
    for component in (generation, _compact_runtime_generation(generation)):
        directory = base / component
        if all(
            len(os.fsencode(directory / name)) <= GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
            for name in _SOCKET_NAMES.values()
        ):
            return directory
    return None


def _safe_existing_runtime_root(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        _reject_symlink_ancestors(path)
        info = path.lstat()
    except (GatewayStateError, OSError):
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _select_runtime_directory(generation: str, *, root: Path | None = None) -> Path:
    _validate_id(generation, "gateway generation")
    if root is not None:
        if not root.is_absolute():
            raise ConfigError("gateway runtime directory must be absolute")
        directory = _runtime_directory_under(root, generation)
        if directory is None:
            raise ConfigError(
                f"gateway runtime socket paths exceed {GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES} bytes"
            )
        return directory

    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        xdg_root = Path(configured)
        if _safe_existing_runtime_root(xdg_root):
            directory = _runtime_directory_under(xdg_root / "sbx", generation)
            if directory is not None:
                return directory

    temporary = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
    directory = _runtime_directory_under(temporary / f"sbx-{os.getuid()}", generation)
    if directory is None:
        raise ConfigError(
            f"gateway runtime socket paths exceed {GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES} bytes"
        )
    return directory


def _create_runtime_directory(generation: str, *, root: Path | None = None) -> Path:
    directory = _select_runtime_directory(generation, root=root)
    base = directory.parent
    parent = base.parent
    _reject_symlink_ancestors(parent)
    if root is not None:
        parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(parent)
    _private_directory(base, create=True)
    directory_fd = os.open(
        base,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.mkdir(directory.name, mode=0o700, dir_fd=directory_fd)
    except FileExistsError as exc:
        raise GatewayStateError(f"gateway runtime directory already exists: {directory}") from exc
    finally:
        os.close(directory_fd)
    _private_directory(directory)
    return directory


def _remove_runtime_directory(directory: Path) -> None:
    _reject_symlink_ancestors(directory)
    _private_directory(directory)
    for name in _SOCKET_NAMES.values():
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise GatewayStateError(f"unsafe gateway runtime socket: {path}")
        path.unlink()
    directory.rmdir()
    with suppress(OSError):
        directory.parent.rmdir()


def _atomic_json(directory: Path, name: str, data: Mapping[str, Any]) -> None:
    _private_directory(directory)
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    if len(encoded) > MAX_CONFIG_BYTES:
        raise GatewayStateError(f"gateway state is too large: {directory / name}")
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != os.getuid()
                or stat.S_IMODE(existing.st_mode) != 0o600
            ):
                raise GatewayStateError(f"unsafe gateway state file: {directory / name}")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise GatewayStateError(f"cannot write gateway state: {directory / name}: {exc}") from exc
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _new_state(
    config: GatewayConfig,
    vm_id: str,
    generation: str,
    runtime_directory: Path | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "vm_id": vm_id,
        "generation": generation,
        "runtime_directory": None if runtime_directory is None else str(runtime_directory),
        "config_digest": config.digest(vm_id, generation),
        "requested_config_digest": config.digest(vm_id, generation),
        "platform": config.platform,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "prepared",
        "pids": {},
        "sockets": {},
        "listeners": [],
        "counters": {},
        "last_failure": None,
    }


@contextmanager
def _state_lock(
    vm_directory: Path,
    owned_fd: int | None = None,
    *,
    timeout: float = 0,
) -> Iterator[int]:
    if owned_fd is not None:
        yield owned_fd
        return
    fd = _fallback_lock(vm_directory / "state", timeout=timeout)
    try:
        yield fd
    finally:
        os.close(fd)


def _read_vm_state(vm_directory: Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    _private_directory(vm_directory)
    generations = {}
    for entry in vm_directory.iterdir():
        if entry.name == "current.json":
            continue
        if entry.name == "ownership.lock":
            info = entry.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise GatewayStateError("unsafe gateway ownership lock")
            continue
        if entry.is_symlink() or not entry.is_dir() or _ID_RE.fullmatch(entry.name) is None:
            raise GatewayStateError("unsafe gateway incarnation entry")
        generations[entry.name] = read_state(entry, vm_id=vm_directory.name)
    pointer = vm_directory / "current.json"
    if not pointer.exists() and not pointer.is_symlink():
        if generations:
            raise GatewayStateError(
                "unsupported pointerless gateway state; explicit reset required"
            )
        return generations, None
    reference = _read_json(pointer)
    generation = reference.get("generation")
    if (
        set(reference) != {"version", "vm_id", "generation"}
        or type(reference.get("version")) is not int
        or reference["version"] != 1
        or reference.get("vm_id") != vm_directory.name
        or (
            generation is not None
            and (
                not isinstance(generation, str)
                or _ID_RE.fullmatch(generation) is None
                or generation not in generations
            )
        )
    ):
        raise GatewayStateError("invalid gateway current-generation reference")
    return generations, generation


def _write_current(vm_directory: Path, generation: str | None) -> None:
    if generation is not None:
        _validate_id(generation, "gateway generation")
        read_state(vm_directory / generation, vm_id=vm_directory.name)
    _atomic_json(
        vm_directory,
        "current.json",
        {
            "version": 1,
            "vm_id": vm_directory.name,
            "generation": generation,
        },
    )


def create_state(
    config: GatewayConfig,
    vm_id: str,
    generation: str,
    *,
    root: Path | None = None,
    runtime_directory: Path | None = None,
    ownership_fd: int | None = None,
) -> Path:
    _validate_id(vm_id, "VM identity")
    _validate_id(generation, "gateway generation")
    base = _state_root(root)
    vm_directory = base / vm_id
    _private_directory(vm_directory, create=True)
    with _state_lock(vm_directory, ownership_fd):
        _read_vm_state(vm_directory)
        if not (vm_directory / "current.json").exists():
            _write_current(vm_directory, None)
        directory = vm_directory / generation
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise GatewayStateError(
                f"gateway incarnation state already exists: {directory}"
            ) from exc
        _private_directory(directory)
        _atomic_json(directory, "policy.json", config.worker_config(vm_id, generation))
        _atomic_json(
            directory,
            "state.json",
            _new_state(config, vm_id, generation, runtime_directory),
        )
        _write_current(vm_directory, generation)
        return directory


def _read_json(path: Path) -> dict[str, Any]:
    _private_directory(path.parent)
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1 << 20
        ):
            raise GatewayStateError(f"unsafe gateway state file: {path}")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayStateError(f"corrupt gateway state: {path}") from exc
    if not isinstance(data, dict):
        raise GatewayStateError(f"corrupt gateway state: {path}")
    return data


def _valid_ip(value: object) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(value, str)


def _validated_state(data: Mapping[str, Any], vm_id: str, generation: str) -> dict[str, Any]:
    fields = {
        "version",
        "vm_id",
        "generation",
        "runtime_directory",
        "config_digest",
        "requested_config_digest",
        "platform",
        "created_at",
        "status",
        "pids",
        "sockets",
        "listeners",
        "counters",
        "last_failure",
    }
    if set(data) != fields or data.get("version") != 1:
        raise GatewayStateError("corrupt gateway state schema")
    if data.get("vm_id") != vm_id or data.get("generation") != generation:
        raise GatewayStateError("cross-VM gateway state identity mismatch")
    digests = (data.get("config_digest"), data.get("requested_config_digest"))
    if any(
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in digests
    ):
        raise GatewayStateError("corrupt gateway config digest")
    if data.get("platform") not in {"linux", "darwin"} or data.get("status") not in _ALLOWED_STATES:
        raise GatewayStateError("corrupt gateway state metadata")
    try:
        datetime.fromisoformat(str(data.get("created_at")))
    except ValueError as exc:
        raise GatewayStateError("corrupt gateway state timestamp") from exc
    pids = data.get("pids")
    if (
        not isinstance(pids, dict)
        or set(pids) - {"worker", "mediator", "qemu", "supervisor"}
        or any(type(pid) is not int or pid <= 0 for pid in pids.values())
    ):
        raise GatewayStateError("corrupt gateway process metadata")
    runtime_value = data.get("runtime_directory")
    runtime_directory = Path(runtime_value) if isinstance(runtime_value, str) else None
    if runtime_value is not None and (
        runtime_directory is None
        or "\0" in runtime_value
        or not runtime_directory.is_absolute()
        or runtime_directory != Path(os.path.abspath(runtime_directory))
        or runtime_directory.name not in {generation, _compact_runtime_generation(generation)}
    ):
        raise GatewayStateError("corrupt gateway runtime directory metadata")
    sockets = data.get("sockets")
    if (
        not isinstance(sockets, dict)
        or set(sockets) - _SOCKET_NAMES.keys()
        or any(not isinstance(value, str) for value in sockets.values())
        or (sockets and runtime_directory is None)
        or any(
            Path(value).parent != runtime_directory
            or Path(value).name != _SOCKET_NAMES[name]
            or len(os.fsencode(value)) > GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
            for name, value in sockets.items()
        )
    ):
        raise GatewayStateError("corrupt gateway socket metadata")
    listeners = data.get("listeners")
    if not isinstance(listeners, list) or any(
        not isinstance(item, dict)
        or set(item) != {"id", "listen_host", "host_port", "guest_address", "guest_port"}
        or not isinstance(item["id"], str)
        or _RULE_ID_RE.fullmatch(item["id"]) is None
        or not isinstance(item["listen_host"], str)
        or not _valid_ip(item["listen_host"])
        or type(item["host_port"]) is not int
        or not 0 <= item["host_port"] <= 65535
        or item["guest_address"] != GUEST_ADDRESS
        or type(item["guest_port"]) is not int
        or not 1 <= item["guest_port"] <= 65535
        for item in listeners
    ):
        raise GatewayStateError("corrupt gateway listener metadata")
    counters = data.get("counters")
    if not isinstance(counters, dict) or any(
        not isinstance(name, str)
        or _RULE_ID_RE.fullmatch(name) is None
        or type(value) is not int
        or value < 0
        for name, value in counters.items()
    ):
        raise GatewayStateError("corrupt gateway counters")
    failure = data.get("last_failure")
    if failure is not None and (
        not isinstance(failure, dict)
        or set(failure) != {"component", "code", "at"}
        or failure.get("component") not in {"controller", "worker", "mediator", "qemu", "ingress"}
        or failure.get("code") not in _ALLOWED_FAILURES
        or not isinstance(failure.get("at"), str)
    ):
        raise GatewayStateError("corrupt gateway failure metadata")
    return dict(data)


def read_state(directory: Path, *, vm_id: str | None = None) -> dict[str, Any]:
    generation = directory.name
    selected_vm = directory.parent.name if vm_id is None else vm_id
    _validate_id(selected_vm, "VM identity")
    _validate_id(generation, "gateway generation")
    return _validated_state(_read_json(directory / "state.json"), selected_vm, generation)


def update_state(
    directory: Path,
    *,
    status: str | None = None,
    pids: Mapping[str, int] | None = None,
    sockets: Mapping[str, Path | str] | None = None,
    listeners: Sequence[IngressRule] | None = None,
    counters: Mapping[str, int] | None = None,
    failure: tuple[str, str] | None = None,
) -> None:
    data = read_state(directory)
    if status is not None:
        if status not in _ALLOWED_STATES:
            raise GatewayStateError("invalid gateway state classification")
        data["status"] = status
    if pids is not None:
        if set(pids) - {"worker", "mediator", "qemu", "supervisor"} or any(
            type(pid) is not int or pid <= 0 for pid in pids.values()
        ):
            raise GatewayStateError("invalid gateway process metadata")
        data["pids"] = dict(pids)
    if sockets is not None:
        checked: dict[str, str] = {}
        runtime_value = data["runtime_directory"]
        runtime_directory = Path(runtime_value) if isinstance(runtime_value, str) else None
        if set(sockets) - _SOCKET_NAMES.keys() or runtime_directory is None:
            raise GatewayStateError("invalid gateway socket metadata")
        for name, raw_path in sockets.items():
            path = Path(raw_path)
            if (
                path.parent != runtime_directory
                or path.name != _SOCKET_NAMES[name]
                or len(os.fsencode(path)) > GATEWAY_UNIX_SOCKET_PATH_MAX_BYTES
            ):
                raise GatewayStateError("gateway socket path crosses its runtime directory")
            checked[name] = str(path)
        data["sockets"] = checked
    if listeners is not None:
        data["listeners"] = [
            {
                "id": rule.id,
                "listen_host": rule.listen_host,
                "host_port": rule.host_port,
                "guest_address": rule.guest_address,
                "guest_port": rule.guest_port,
            }
            for rule in listeners
        ]
    if counters is not None:
        if any(
            not isinstance(name, str)
            or _RULE_ID_RE.fullmatch(name) is None
            or type(value) is not int
            or value < 0
            for name, value in counters.items()
        ):
            raise GatewayStateError("invalid gateway counters")
        data["counters"] = dict(counters)
    if failure is not None:
        component, code = failure
        if (
            component not in {"controller", "worker", "mediator", "qemu", "ingress"}
            or code not in _ALLOWED_FAILURES
        ):
            raise GatewayStateError("invalid gateway failure classification")
        data["last_failure"] = {
            "component": component,
            "code": code,
            "at": datetime.now(UTC).isoformat(),
        }
    _validated_state(data, directory.parent.name, directory.name)
    _atomic_json(directory, "state.json", data)


class GatewayLifecycleError(RuntimeError):
    pass


class GatewayAuthenticationError(GatewayLifecycleError):
    pass


def _resolve_artifact(
    environment_name: str | None,
    candidates: Sequence[Path | str | None],
    *,
    label: str,
) -> Path:
    if environment_name and (override := os.environ.get(environment_name)):
        return Path(override).expanduser()
    for candidate in candidates:
        if candidate is not None and (path := Path(candidate).expanduser()).exists():
            return path
    raise GatewayLifecycleError(f"{label} is missing")


def _sanitized_child_environment(directory: Path, *, path: str = os.defpath) -> dict[str, str]:
    return {
        "HOME": str(directory),
        "LANG": "C.UTF-8",
        "PATH": path,
        "TZ": "UTC",
    }


def _hardening_ready(value: object, platform: str) -> bool:
    if platform == "darwin":
        return isinstance(value, str)
    return isinstance(value, str) and {"no_new_privs", "seccomp", "rlimits"} <= set(
        value.split(",")
    )


def _process_tree_usage(root_pids: Sequence[int]) -> tuple[int, int, int, int]:
    """Return RSS bytes, descriptors, processes, and threads for owned process trees."""
    pending = list(root_pids)
    seen: set[int] = set()
    rss = descriptors = threads = 0
    while pending:
        pid = pending.pop()
        if pid in seen or pid <= 0:
            continue
        seen.add(pid)
        process = Path("/proc") / str(pid)
        try:
            status = process.joinpath("status").read_text().splitlines()
        except OSError:
            continue
        for line in status:
            if line.startswith("VmRSS:"):
                rss += int(line.split()[1]) * 1024
            elif line.startswith("Threads:"):
                threads += int(line.split()[1])
        with suppress(OSError):
            descriptors += sum(1 for _ in process.joinpath("fd").iterdir())
        with suppress(OSError):
            tasks = process.joinpath("task").iterdir()
            for task in tasks:
                children = task.joinpath("children").read_text().split()
                pending.extend(int(value) for value in children)
    return rss, descriptors, len(seen), threads


class GatewayController:
    """Own one gateway incarnation from reserved listeners through teardown."""

    def __init__(
        self,
        config: GatewayConfig,
        vm_id: str,
        *,
        root: Path | None = None,
        runtime_root: Path | None = None,
        generation: str | None = None,
        runner: Callable[..., Any] = subprocess.Popen,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        timeout: float = 10.0,
    ) -> None:
        self.config = config
        self.vm_id = _validate_id(vm_id, "VM identity")
        self.generation = generation or uuid.uuid4().hex
        _validate_id(self.generation, "gateway generation")
        self.root = root
        self.runtime_root = runtime_root
        self.runner = runner
        self.socket_factory = socket_factory
        if timeout <= 0:
            raise ValueError("gateway lifecycle timeout must be positive")
        self.timeout = timeout
        self.directory: Path | None = None
        self.runtime_directory: Path | None = None
        self.listeners: list[socket.socket] = []
        self.listener_rules: tuple[IngressRule, ...] = ()
        self.qemu_listener: socket.socket | None = None
        self.processes: dict[str, Any] = {}
        self._worker_stdout: BinaryIO | None = None
        self._worker_buffer = bytearray()
        self._dns_state_fd: int | None = None
        self._dns_state_initialized = False
        self._logs: list[BinaryIO] = []
        self._log_threads: list[threading.Thread] = []
        self._log_lock = threading.Lock()
        self._log_tokens = float(MAX_LOG_EVENT_BURST)
        self._log_last = time.monotonic()
        self._log_bytes = 0
        self._closed = False
        self._draining = False
        self._lock = threading.Lock()
        self._supervisor_stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._supervisor_failure: GatewayLifecycleError | None = None
        self._mediator_policy_payload: bytes | None = None
        self._immutable_policy: bytes | None = None
        self._qemu_pid: int | None = None
        self._warned_hardening = False
        self._lifetime_read_fd: int | None = None
        self._lifetime_write_fd: int | None = None
        self._broker_socket: socket.socket | None = None
        self._credential_broker: Any | None = None
        self._owner_pid: int | None = None
        self._ownership_fd: int | None = None
        self._requested_config_digest = config.digest(vm_id, self.generation)
        self.mediator_address: str | None = None
        self._audit_stream: BinaryIO | None = None
        self._audit_directory_synced = False
        self._audit_failure: GatewayLifecycleError | None = None
        self._audit_write_fd: int | None = None
        self._audit_ack_read_fd: int | None = None
        self.worker_incarnation = uuid.uuid4().hex
        self._incarnation_token = secrets.token_bytes(32)
        self._placeholders: dict[str, str] = {}
        for credential in config.credentials:
            if not credential.bindings:
                continue
            placeholder = secrets.token_urlsafe(32)
            while placeholder in self._placeholders.values():
                placeholder = secrets.token_urlsafe(32)
            self._placeholders[credential.id] = placeholder
        random = bytearray(secrets.token_bytes(6))
        random[0] = (random[0] | 2) & 0xFE
        self.mac = ":".join(f"{value:02x}" for value in random)

    @property
    def qemu_socket(self) -> Path:
        if self.runtime_directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        return self.runtime_directory / "qemu-net.sock"

    @property
    def credential_placeholders(self) -> dict[str, str]:
        return {
            credential.environment: self._placeholders[credential.id]
            for credential in self.config.credentials
            if credential.id in self._placeholders
        }

    def _open_log(self, name: str) -> BinaryIO:
        assert self.directory is not None
        _private_directory(self.directory)
        if not name.endswith(".log") or "/" in name or "\\" in name:
            raise GatewayLifecycleError("invalid gateway log name")
        size = sum(
            entry.stat(follow_symlinks=False).st_size
            for entry in os.scandir(self.directory)
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".log")
        )
        if size >= MAX_LOG_BYTES:
            raise GatewayLifecycleError("gateway log limit is exhausted")
        self._log_bytes = size
        fd = os.open(
            self.directory / name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "wb", buffering=0)
        self._logs.append(stream)
        return stream

    def _emit_log(self, destination: BinaryIO, event: bytes, *, required: bool = False) -> None:
        values = list(self._placeholders.values())
        if self._credential_broker is not None:
            values.extend(tuple(self._credential_broker.values))
        try:
            record = json.loads(event)
        except (ValueError, UnicodeDecodeError):
            record = None
        if isinstance(record, dict) and all(
            not isinstance(v, (dict, list)) for v in record.values()
        ):
            for key, text in record.items():
                if isinstance(text, str):
                    for value in values:
                        text = text.replace(value, "[redacted]")
                    record[key] = text
            event = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
        else:
            for value in values:
                event = event.replace(value.encode(), b"[redacted]")
        now = time.monotonic()
        with self._log_lock:
            self._log_tokens = min(
                MAX_LOG_EVENT_BURST,
                self._log_tokens + (now - self._log_last) * MAX_LOG_EVENTS_PER_SECOND,
            )
            self._log_last = now
            if self._log_tokens < 1 or self._log_bytes >= MAX_LOG_BYTES:
                if required:
                    raise GatewayLifecycleError("gateway audit rate or storage limit exhausted")
                return
            self._log_tokens -= 1
            if required:
                if len(event) > MAX_AUDIT_EVENT_BYTES:
                    raise GatewayLifecycleError(
                        "gateway audit event exceeds transport memory limit"
                    )
                event = event.rstrip(b"\n") + b"\n"
            else:
                event = event[:MAX_LOG_EVENT_BYTES].rstrip(b"\n") + b"\n"
            if len(event) > MAX_LOG_BYTES - self._log_bytes:
                if required:
                    raise GatewayLifecycleError("gateway audit storage limit exhausted")
                return
            remaining = memoryview(event)
            while remaining:
                written = destination.write(remaining)
                if not written:
                    raise OSError("gateway log write failed")
                self._log_bytes += written
                remaining = remaining[written:]

    def _open_dns_state(self, policy: Mapping[str, Any]) -> int:
        assert self.directory is not None
        _private_directory(self.directory)
        path = self.directory / "dns-mappings.jsonl"
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        created = False
        if self._dns_state_initialized:
            fd = os.open(path, flags)
        else:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                # Existing generation state is opened unchanged, never reinitialized.
                fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_DNS_JOURNAL_BYTES
            ):
                raise GatewayStateError("unsafe gateway DNS state")
            if info.st_size == 0 and not created:
                raise GatewayStateError("empty gateway DNS state")
            if created:
                header = (
                    json.dumps(
                        {
                            "version": 1,
                            "vm_id": self.vm_id,
                            "generation": self.generation,
                            "digest": policy["digest"],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
                if os.write(fd, header) != len(header):
                    raise GatewayLifecycleError("incomplete gateway DNS state header")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            self._dns_state_initialized = True
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _sync_audit(self, destination: BinaryIO) -> None:
        destination.flush()
        os.fsync(destination.fileno())
        if sys.platform == "darwin":
            if not hasattr(fcntl, "F_FULLFSYNC"):
                raise GatewayLifecycleError("durable audit sync unavailable")
            fcntl.fcntl(destination.fileno(), fcntl.F_FULLFSYNC)
        assert self.directory is not None
        if not self._audit_directory_synced:
            # Persist newly created generation/state ancestors as well as the log entry.
            for directory in (self.directory, *self.directory.parents):
                directory_fd = os.open(
                    directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._audit_directory_synced = True

    def _route_audit(
        self, source: BinaryIO, destination: BinaryIO, ack_fd: int | None = None
    ) -> None:
        incarnation = self.worker_incarnation
        if ack_fd is not None:
            os.set_blocking(ack_fd, False)

        def route() -> None:
            buffered = bytearray()
            try:
                while chunk := os.read(source.fileno(), MAX_LOG_EVENT_BYTES):
                    buffered.extend(chunk)
                    while b"\n" in buffered:
                        event, _, remainder = bytes(buffered).partition(b"\n")
                        buffered.clear()
                        buffered.extend(remainder)
                        if len(event) > MAX_AUDIT_EVENT_BYTES:
                            raise GatewayLifecycleError(
                                "gateway audit event exceeds transport memory limit"
                            )
                        record = json.loads(event)
                        if not isinstance(record, dict) or record.get("version") not in (1, 2):
                            raise GatewayLifecycleError("invalid gateway audit record")
                        if "audit_error" in record:
                            raise GatewayLifecycleError("mediator audit delivery failed")
                        if record.get("version") == 2 and (
                            record.get("event") not in ("admitted", "outcome")
                            or record.get("generation") != self.generation
                            or record.get("incarnation") != incarnation
                            or not isinstance(record.get("request_id"), str)
                            or not 1 <= len(record["request_id"]) <= 128
                        ):
                            raise GatewayLifecycleError("invalid gateway audit identity")
                        admitted = record.get("event") == "admitted"
                        if admitted and (record.get("version") != 2 or ack_fd is None):
                            raise GatewayLifecycleError(
                                "audit admission acknowledgement unavailable"
                            )
                        self._emit_log(destination, event, required=True)
                        if admitted:
                            self._sync_audit(destination)
                            ack = {
                                key: record[key]
                                for key in ("version", "generation", "incarnation", "request_id")
                            }
                            raw = (json.dumps(ack, ensure_ascii=True) + "\n").encode()
                            if len(raw) > 4096 or not select.select([], [ack_fd], [], 1.0)[1]:
                                raise GatewayLifecycleError("audit acknowledgement timed out")
                            if os.write(ack_fd, raw) != len(raw):
                                raise GatewayLifecycleError("audit acknowledgement interrupted")
                    if len(buffered) > MAX_AUDIT_EVENT_BYTES:
                        raise GatewayLifecycleError(
                            "gateway audit event exceeds transport memory limit"
                        )
                if buffered:
                    raise GatewayLifecycleError("gateway audit record interrupted")
            except (OSError, ValueError, GatewayLifecycleError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, GatewayLifecycleError)
                    else "invalid audit record encoding"
                    if isinstance(exc, ValueError)
                    else "audit sink I/O failed"
                )
                self._audit_failure = GatewayLifecycleError(reason)
                print(
                    f"sbx: {reason}; stopping gateway (see network diagnostics)",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                source.close()
                if ack_fd is not None:
                    os.close(ack_fd)

        thread = threading.Thread(target=route, name=f"sbx-gateway-audit-{self.vm_id}", daemon=True)
        self._log_threads.append(thread)
        thread.start()

    def _route_log(self, source: BinaryIO | None, destination: BinaryIO) -> None:
        if source is None:
            return

        def route() -> None:
            buffered = bytearray()
            try:
                while chunk := os.read(source.fileno(), 4096):
                    buffered.extend(chunk)
                    while b"\n" in buffered:
                        event, _, remainder = bytes(buffered).partition(b"\n")
                        buffered.clear()
                        buffered.extend(remainder)
                        self._emit_log(destination, event)
                    if len(buffered) > MAX_LOG_EVENT_BYTES:
                        self._emit_log(destination, bytes(buffered))
                        buffered.clear()
                if buffered:
                    self._emit_log(destination, bytes(buffered))
            except (OSError, ValueError):
                return
            finally:
                with suppress(Exception):
                    source.close()

        thread = threading.Thread(target=route, name=f"sbx-gateway-log-{self.vm_id}", daemon=True)
        self._log_threads.append(thread)
        thread.start()

    def _reap_log_threads(self) -> None:
        for thread in self._log_threads:
            thread.join(timeout=0.1)
        self._log_threads = [thread for thread in self._log_threads if thread.is_alive()]

    def _reserve_qemu_listener(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.qemu_socket))
            os.chmod(self.qemu_socket, 0o600)
            info = self.qemu_socket.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise GatewayLifecycleError("unsafe gateway QEMU socket")
            listener.listen()
        except BaseException:
            listener.close()
            raise
        self.qemu_listener = listener

    def _reserve_listeners(self) -> None:
        rules = []
        for rule in self.config.ingress:
            family = socket.AF_INET6 if ":" in rule.listen_host else socket.AF_INET
            listener = self.socket_factory(family, socket.SOCK_STREAM)
            try:
                listener.bind((rule.listen_host, rule.host_port))
                listener.listen()
                host, port = listener.getsockname()[:2]
                host = ipaddress.ip_address(host).compressed
                if host != rule.listen_host or (rule.host_port and port != rule.host_port):
                    raise GatewayLifecycleError("ingress listener did not bind the exact address")
            except BaseException:
                listener.close()
                raise
            self.listeners.append(listener)
            rules.append(replace(rule, listen_host=host, host_port=int(port)))
            if not ipaddress.ip_address(host).is_loopback:
                print(
                    f"sbx: warning: gateway ingress {host}:{port} is reachable from "
                    "the corresponding host networks",
                    file=sys.stderr,
                )
        self.listener_rules = tuple(rules)

    @staticmethod
    def _pipe_with(value: bytes) -> int:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, value)
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        return read_fd

    def _mediator_policy(self) -> bytes:
        rules = [
            {
                "id": rule.id,
                "policy": rule.policy_id or rule.id,
                "host": rule.host,
                "ports": list(rule.ports),
                "methods": list(rule.methods),
                "paths": list(rule.paths),
                "hooks": list(rule.hooks),
            }
            for rule in self.config.rules
            if rule.protocol == "http"
        ]
        bindings = []
        for credential in self.config.credentials:
            for binding in credential.bindings:
                if binding.header is not None:
                    name, value = (part.strip() for part in binding.header.split(":", 1))
                    header = {"name": name, "format": "template", "value": value}
                else:
                    header = credential.header
                if header is None:
                    raise GatewayLifecycleError(
                        f"credential {credential.id!r} binding requires a header"
                    )
                for policy_id in binding.policies:
                    for rule in self.config.rules:
                        if rule.policy_id != policy_id:
                            continue
                        bindings.append(
                            {
                                "credential": credential.id,
                                "rule_id": rule.id,
                                "methods": list(binding.methods),
                                "paths": list(binding.paths),
                                "header": header,
                                "placeholder": self._placeholders[credential.id],
                            }
                        )
        payload = _compact_json({"version": 2, "rules": rules, "credentials": bindings})
        if len(payload) > 65536:
            raise GatewayLifecycleError("gateway mediator policy is too large")
        return payload

    def _mediator_command(
        self, key_fd: int, ready_fd: int, policy_fd: int, broker_fd: int
    ) -> list[str]:
        assert self.directory is not None
        launcher = _resolve_artifact(
            None,
            (GATEWAY_MEDIATOR_LAUNCHER, GATEWAY_SOURCE_DIR / "mediator" / "launch.py"),
            label="gateway mediator launcher",
        )
        return [
            sys.executable,
            str(launcher),
            "--vm-id",
            self.vm_id,
            "--incarnation",
            self.worker_incarnation,
            "--key-fd",
            str(key_fd),
            "--ready-fd",
            str(ready_fd),
            "--policy-fd",
            str(policy_fd),
            "--broker-fd",
            str(broker_fd),
            "--lifetime-fd",
            str(self._lifetime_read_fd if self._lifetime_read_fd is not None else -1),
            "--confdir",
            str(self.directory),
            "--open-files",
            str(self.config.quotas.open_files // 2),
            *(["--mitmweb"] if self.config.mitmweb else []),
            "--generation",
            self.generation,
            *(
                [
                    "--audit-fd",
                    str(self._audit_write_fd),
                    "--audit-ack-fd",
                    str(self._audit_ack_read_fd),
                ]
                if self._audit_write_fd is not None
                else []
            ),
        ]

    def _worker_command(self, key_fd: int | None, token_fd: int) -> list[str]:
        assert self.directory is not None
        worker = _resolve_artifact(
            "SBX_GATEWAY_BIN",
            (GATEWAY_WORKER, GATEWAY_DEV_WORKER, shutil.which("sbx-gateway")),
            label="gateway worker artifact",
        )
        policy = _read_json(self.directory / "policy.json")
        digest = policy.get("digest")
        if not isinstance(digest, str):
            raise GatewayLifecycleError("gateway worker configuration digest is missing")
        command = [
            str(worker),
            "--socket",
            str(self.qemu_socket),
            "--config",
            str((self.directory / "policy.json").resolve()),
            "--vm-id",
            self.vm_id,
            "--generation",
            self.generation,
            "--digest",
            digest,
            "--qemu-listener-fd",
            str(self.qemu_listener.fileno() if self.qemu_listener is not None else -1),
            "--worker-incarnation",
            self.worker_incarnation,
            *(
                ["--dns-state-fd", str(self._dns_state_fd)]
                if self._dns_state_fd is not None
                else []
            ),
            "--incarnation-token-fd",
            str(token_fd),
            "--supervisor-lifetime-fd",
            str(self._lifetime_read_fd if self._lifetime_read_fd is not None else -1),
        ]
        if key_fd is not None and self.mediator_address is not None:
            command.extend(
                (
                    "--capability-key-fd",
                    str(key_fd),
                    "--mediator-address",
                    self.mediator_address,
                )
            )
        for rule, listener in zip(self.listener_rules, self.listeners, strict=True):
            command.extend(
                (
                    "--ingress-fd",
                    f"{self.vm_id}/{self.generation}/{rule.id}={listener.fileno()}",
                )
            )
        return command

    def _read_json_fd(
        self,
        fd: int,
        process: Any,
        label: str,
        buffer: bytearray | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        data = bytearray() if buffer is None else buffer
        while (remaining := deadline - time.monotonic()) > 0:
            if self._draining:
                raise GatewayLifecycleError(f"cancelled {label} readiness")
            if b"\n" in data:
                line, _, remainder = bytes(data).partition(b"\n")
                data.clear()
                data.extend(remainder)
                try:
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GatewayLifecycleError(f"{label} returned invalid readiness data") from exc
                if not isinstance(payload, dict):
                    raise GatewayLifecycleError(f"{label} returned invalid readiness data")
                return payload
            if process.poll() is not None:
                raise GatewayLifecycleError(f"{label} exited before readiness")
            readable, _, _ = select.select([fd], [], [], min(0.05, remaining))
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                raise GatewayLifecycleError(f"{label} closed its readiness channel")
            data.extend(chunk)
            if len(data) > 4096:
                raise GatewayLifecycleError(f"{label} returned oversized readiness data")
        raise GatewayLifecycleError(f"timed out waiting for {label} readiness")

    def _read_worker_status(self, expected: str, deadline: float | None = None) -> dict[str, Any]:
        process = self.processes["worker"]
        stdout = self._worker_stdout
        if stdout is None:
            raise GatewayLifecycleError("worker readiness channel is unavailable")
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        while True:
            payload = self._read_json_fd(
                stdout.fileno(), process, "worker", self._worker_buffer, deadline
            )
            status = payload.get("status")
            if status == expected:
                return payload
            if status != "stats":
                raise GatewayLifecycleError("worker returned an unexpected readiness status")

    def _expected_incarnation_proof(self) -> str:
        message = "\0".join(
            (
                self.vm_id,
                self.generation,
                self._policy()["digest"],
                self.worker_incarnation,
            )
        ).encode()
        return hmac.new(self._incarnation_token, message, hashlib.sha256).hexdigest()

    def _policy(self) -> dict[str, Any]:
        assert self.directory is not None
        path = self.directory / "policy.json"
        raw = path.read_bytes()
        if self._immutable_policy is not None and raw != self._immutable_policy:
            raise GatewayAuthenticationError("replacement worker config mismatch")
        return _read_json(path)

    def _start_data_plane(self, deadline: float) -> None:
        assert self.directory is not None and self._mediator_policy_payload is not None
        policy = self._policy()
        key = secrets.token_bytes(32)
        lifetime_read, lifetime_write = os.pipe()
        self._lifetime_read_fd = lifetime_read
        self._lifetime_write_fd = lifetime_write
        self._broker_socket, mediator_broker = socket.socketpair()
        if self._credential_broker is not None:
            threading.Thread(
                target=self._credential_broker.serve,
                args=(self._broker_socket, self.worker_incarnation),
                daemon=True,
                name=f"sbx-credential-broker-{self.vm_id}",
            ).start()
        if self._audit_stream is None:
            self._audit_stream = self._open_log("audit.log")
        audit_read, audit_write = os.pipe()
        self._audit_write_fd = audit_write
        try:
            ack_read, ack_write = os.pipe()
        except OSError:
            os.close(audit_read)
            raise
        self._audit_ack_read_fd = ack_read
        self._route_audit(os.fdopen(audit_read, "rb", buffering=0), self._audit_stream, ack_write)
        mediator_key = self._pipe_with(key)
        mediator_policy_fd, mediator_policy_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            try:
                mediator_log = self._open_log(f"mediator-{self.worker_incarnation}.log")
                mediator = self.runner(
                    self._mediator_command(
                        mediator_key, ready_write, mediator_policy_fd, mediator_broker.fileno()
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    pass_fds=(
                        mediator_key,
                        ready_write,
                        mediator_policy_fd,
                        mediator_broker.fileno(),
                        lifetime_read,
                        audit_write,
                        ack_read,
                    ),
                    close_fds=True,
                    start_new_session=True,
                    env=_sanitized_child_environment(
                        self.directory, path=os.environ.get("PATH", os.defpath)
                    ),
                )
                self.processes["mediator"] = mediator
                self._route_log(getattr(mediator, "stdout", None), mediator_log)
            finally:
                os.close(audit_write)
                self._audit_write_fd = None
                os.close(ack_read)
                self._audit_ack_read_fd = None
                os.close(mediator_key)
                os.close(mediator_policy_fd)
                os.close(ready_write)
                mediator_broker.close()
            view = memoryview(self._mediator_policy_payload)
            os.set_blocking(mediator_policy_write, False)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GatewayLifecycleError("timed out transferring mediator policy")
                _, writable, _ = select.select([], [mediator_policy_write], [], remaining)
                if not writable:
                    raise GatewayLifecycleError("timed out transferring mediator policy")
                view = view[os.write(mediator_policy_write, view) :]
            os.close(mediator_policy_write)
            mediator_policy_write = -1
            try:
                ready = self._read_json_fd(ready_read, mediator, "mediator", deadline=deadline)
            except GatewayLifecycleError as exc:
                self._reap_log_threads()
                log_path = self.directory / f"mediator-{self.worker_incarnation}.log"
                detail = log_path.read_text(errors="replace").strip()[-1000:]
                if detail:
                    raise GatewayLifecycleError(f"{exc}: {detail}") from exc
                raise
        finally:
            if mediator_policy_write >= 0:
                os.close(mediator_policy_write)
            os.close(ready_read)
        if (
            ready.get("status") != "ready"
            or ready.get("host") != "127.0.0.1"
            or type(ready.get("port")) is not int
            or not 1 <= ready["port"] <= 65535
            or ready.get("pid") != mediator.pid
            or ready.get("vm_id") != self.vm_id
            or ready.get("incarnation") != self.worker_incarnation
            or not _hardening_ready(ready.get("hardening"), self.config.platform)
        ):
            raise GatewayAuthenticationError("mediator readiness identity mismatch")
        if (
            self.config.platform == "linux"
            and "landlock-unavailable" in ready["hardening"]
            and not self._warned_hardening
        ):
            print(
                "sbx: warning: Landlock is unavailable; gateway filesystem confinement is reduced",
                file=sys.stderr,
            )
            self._warned_hardening = True
        if self.config.mitmweb:
            web_port = ready.get("web_port")
            if type(web_port) is not int or not 1 <= web_port <= 65535:
                raise GatewayAuthenticationError("mediator web readiness mismatch")
            _atomic_json(self.directory, "web.json", {"port": web_port})
        self.mediator_address = f"127.0.0.1:{ready['port']}"
        deadline = time.monotonic() + self.timeout

        has_http = any(rule.protocol == "http" for rule in self.config.rules)
        worker_key = self._pipe_with(key) if has_http else None
        token_fd = self._pipe_with(self._incarnation_token)
        qemu_fd = self.qemu_listener.fileno() if self.qemu_listener is not None else -1
        ingress_fds = tuple(listener.fileno() for listener in self.listeners)
        try:
            try:
                self._dns_state_fd = self._open_dns_state(policy)
            except GatewayStateError as exc:
                raise GatewayAuthenticationError("unsafe worker DNS state") from exc
            except OSError as exc:
                # Translate filesystem failure at worker startup so recovery fails closed.
                raise GatewayLifecycleError("worker DNS state I/O failed") from exc
            worker_log = self._open_log(f"worker-{self.worker_incarnation}.log")
            worker = self.runner(
                self._worker_command(worker_key, token_fd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(qemu_fd, token_fd, lifetime_read, self._dns_state_fd, *ingress_fds)
                if worker_key is None
                else (
                    qemu_fd,
                    token_fd,
                    worker_key,
                    lifetime_read,
                    self._dns_state_fd,
                    *ingress_fds,
                ),
                close_fds=True,
                start_new_session=True,
                env=_sanitized_child_environment(self.directory),
            )
            self.processes["worker"] = worker
            self._worker_stdout = worker.stdout
            self._route_log(getattr(worker, "stderr", None), worker_log)
            self._worker_buffer.clear()
        finally:
            if self._dns_state_fd is not None:
                os.close(self._dns_state_fd)
                self._dns_state_fd = None
            os.close(token_fd)
            if worker_key is not None:
                os.close(worker_key)
            os.close(lifetime_read)
            self._lifetime_read_fd = None
        try:
            ready = self._read_worker_status("ready", deadline)
        except GatewayLifecycleError as exc:
            self._reap_log_threads()
            log_path = self.directory / f"worker-{self.worker_incarnation}.log"
            detail = log_path.read_text(errors="replace").strip()[-1000:]
            if detail:
                raise GatewayLifecycleError(f"{exc}: {detail}") from exc
            raise
        if (
            ready.get("version") != 1
            or ready.get("vm_id") != self.vm_id
            or ready.get("generation") != self.generation
            or ready.get("digest") != policy.get("digest")
            or ready.get("incarnation") != self.worker_incarnation
            or not hmac.compare_digest(
                str(ready.get("incarnation_proof", "")), self._expected_incarnation_proof()
            )
            or ready.get("mediator") is not has_http
            or ready.get("ingress") != len(self.listener_rules)
            or not _hardening_ready(ready.get("hardening"), self.config.platform)
        ):
            raise GatewayAuthenticationError("worker readiness identity mismatch")

    def _discard_data_plane(self) -> None:
        if self._audit_ack_read_fd is not None:
            with suppress(OSError):
                os.close(self._audit_ack_read_fd)
            self._audit_ack_read_fd = None
        if self._audit_write_fd is not None:
            with suppress(OSError):
                os.close(self._audit_write_fd)
            self._audit_write_fd = None
        if self._lifetime_write_fd is not None:
            with suppress(OSError):
                os.close(self._lifetime_write_fd)
            self._lifetime_write_fd = None
        if self._lifetime_read_fd is not None:
            with suppress(OSError):
                os.close(self._lifetime_read_fd)
            self._lifetime_read_fd = None
        if self._broker_socket is not None:
            # Wake the broker reader so disconnected lock/slot waiters can cancel.
            with suppress(OSError):
                self._broker_socket.shutdown(socket.SHUT_RDWR)
            self._broker_socket.close()
            self._broker_socket = None
        for name in ("worker", "mediator"):
            process = self.processes.pop(name, None)
            if process is not None:
                self._terminate(process)
        if self._worker_stdout is not None:
            with suppress(Exception):
                self._worker_stdout.close()
        self._worker_stdout = None
        self._worker_buffer.clear()
        self.mediator_address = None
        self._reap_log_threads()

    def prepare(self) -> None:
        """Reserve controller endpoints and authenticate the initial data plane."""
        if self.directory is not None:
            raise GatewayLifecycleError("gateway is already prepared")
        self._mediator_policy_payload = self._mediator_policy()
        try:
            self.runtime_directory = _create_runtime_directory(
                self.generation, root=self.runtime_root
            )
            self.directory = create_state(
                self.config,
                self.vm_id,
                self.generation,
                root=self.root,
                runtime_directory=self.runtime_directory,
            )
            self._immutable_policy = (self.directory / "policy.json").read_bytes()
            update_state(self.directory, status="starting")
            self._reserve_qemu_listener()
            self._reserve_listeners()
            update_state(self.directory, listeners=self.listener_rules)
            self._start_data_plane(time.monotonic() + self.timeout)
            update_state(
                self.directory,
                pids={name: process.pid for name, process in self.processes.items()},
                sockets={"qemu": self.qemu_socket},
            )
        except BaseException:
            self.rollback("startup_failed")
            raise

    def _process_ids(self) -> dict[str, int]:
        pids = {name: process.pid for name, process in self.processes.items()}
        if self._qemu_pid is not None:
            pids["qemu"] = self._qemu_pid
        if self._owner_pid is not None:
            pids["supervisor"] = self._owner_pid
        return pids

    def vm_network_ready(self, qemu_pid: int, timeout: float | None = None) -> None:
        """Complete readiness only after QEMU exchanges a valid framed packet."""
        if self.directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            ready = self._read_worker_status("network-ready", deadline)
            if self.config.platform == "linux" and ready.get("qemu_pid") != qemu_pid:
                raise GatewayAuthenticationError("QEMU peer process identity mismatch")
            if any(process.poll() is not None for process in self.processes.values()):
                raise GatewayLifecycleError("gateway component exited during startup")
            self._qemu_pid = qemu_pid
            update_state(self.directory, status="ready", pids=self._process_ids())
            self._supervisor = threading.Thread(
                target=self._supervise, name=f"sbx-gateway-{self.vm_id}", daemon=True
            )
            self._supervisor.start()
        except BaseException:
            self.rollback("startup_failed")
            raise

    def _fail_recovery(self, error: GatewayLifecycleError) -> None:
        self._supervisor_failure = error
        self._draining = True
        self._discard_data_plane()
        self._close_endpoints()
        if self.directory is not None:
            with suppress(Exception):
                update_state(
                    self.directory,
                    status="failed",
                    pids=self._process_ids(),
                    failure=("worker", "reconnection_failed"),
                )

    def recover(self) -> None:
        """Replace a failed worker within the configured retry and time bounds."""
        with self._lock:
            if self._closed or self._draining:
                raise GatewayLifecycleError("gateway is not recoverable")
            if not self.config.reconnection.enabled:
                error = GatewayLifecycleError("gateway reconnection is disabled")
                self._fail_recovery(error)
                raise error
            assert self.directory is not None
            update_state(self.directory, status="recovering", pids=self._process_ids())
            deadline = time.monotonic() + self.config.reconnection.timeout_seconds
            last_error: GatewayLifecycleError = GatewayLifecycleError(
                "replacement worker did not become ready"
            )
            for _ in range(self.config.reconnection.attempts):
                self._discard_data_plane()
                self.worker_incarnation = uuid.uuid4().hex
                self._incarnation_token = secrets.token_bytes(32)
                try:
                    if time.monotonic() >= deadline:
                        raise GatewayLifecycleError("replacement worker readiness timed out")
                    self._start_data_plane(deadline)
                    ready = self._read_worker_status("network-ready", deadline)
                    if (
                        self.config.platform == "linux"
                        and self._qemu_pid is not None
                        and ready.get("qemu_pid") != self._qemu_pid
                    ):
                        raise GatewayAuthenticationError("QEMU peer process identity mismatch")
                    if any(process.poll() is not None for process in self.processes.values()):
                        raise GatewayLifecycleError("replacement component exited during readiness")
                    update_state(self.directory, status="ready", pids=self._process_ids())
                    return
                except GatewayAuthenticationError as exc:
                    last_error = exc
                    break
                except GatewayLifecycleError as exc:
                    last_error = exc
            error = GatewayLifecycleError(f"gateway reconnection failed: {last_error}")
            self._fail_recovery(error)
            raise error from last_error

    def _resource_ceiling_exceeded(self) -> bool:
        if self.config.platform != "linux":
            return False
        rss, descriptors, processes, threads = _process_tree_usage(
            [process.pid for process in self.processes.values()]
        )
        log_bytes = 0
        if self.directory is not None:
            with suppress(OSError):
                log_bytes = sum(
                    entry.stat(follow_symlinks=False).st_size
                    for entry in os.scandir(self.directory)
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".log")
                )
        return (
            rss > MAX_DATA_PLANE_RSS
            or descriptors > self.config.quotas.open_files
            or processes > MAX_DATA_PLANE_PROCESSES
            or threads > MAX_DATA_PLANE_THREADS
            or log_bytes > MAX_LOG_TOTAL_BYTES
        )

    def _drain_worker_stats(self) -> None:
        if self._worker_stdout is None or self.directory is None:
            return
        fd = self._worker_stdout.fileno()
        readable, _, _ = select.select([fd], [], [], 0)
        if readable:
            chunk = os.read(fd, 4096)
            if chunk:
                self._worker_buffer.extend(chunk)
        if len(self._worker_buffer) > 8192:
            raise GatewayLifecycleError("worker returned oversized statistics")
        while b"\n" in self._worker_buffer:
            line, _, remainder = bytes(self._worker_buffer).partition(b"\n")
            self._worker_buffer.clear()
            self._worker_buffer.extend(remainder)
            try:
                payload = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GatewayLifecycleError("worker returned invalid statistics") from exc
            if not isinstance(payload, dict) or payload.get("status") != "stats":
                raise GatewayLifecycleError("worker returned unexpected status")
            names = (
                "frames_read",
                "frames_written",
                "frames_rejected",
                "tcp_active",
                "tcp_rejected",
                "destination_rejected",
                "ingress_active",
                "dns_active",
                "dns_rejected",
                "dns_mappings",
                "bytes_in",
                "bytes_out",
            )
            counters = {
                name: payload[name]
                for name in names
                if type(payload.get(name)) is int and payload[name] >= 0
            }
            if counters:
                exhausted = any(
                    counters.get(name, 0) > 0
                    for name in ("tcp_rejected", "destination_rejected", "dns_rejected")
                )
                update_state(
                    self.directory,
                    counters=counters,
                    failure=("worker", "quota_exhausted") if exhausted else None,
                )

    def _fail_quota(self) -> None:
        error = GatewayLifecycleError("gateway resource ceiling exhausted")
        self._supervisor_failure = error
        self._draining = True
        self._discard_data_plane()
        self._close_endpoints()
        if self.directory is not None:
            with suppress(Exception):
                update_state(
                    self.directory,
                    status="failed",
                    pids=self._process_ids(),
                    failure=("controller", "quota_exhausted"),
                )

    def _fail_supervision(self) -> None:
        self._supervisor_failure = GatewayLifecycleError("gateway supervision failed")
        self._draining = True
        self._discard_data_plane()
        self._close_endpoints()
        if self.directory is not None:
            with suppress(Exception):
                update_state(
                    self.directory,
                    status="failed",
                    pids=self._process_ids(),
                    failure=("controller", "worker_failed"),
                )

    def _fail_audit(self) -> None:
        self._supervisor_failure = self._audit_failure
        self._draining = True
        self._discard_data_plane()
        self._close_endpoints()
        if self.directory is not None:
            update_state(
                self.directory,
                status="failed",
                pids=self._process_ids(),
                failure=("controller", "audit_failed"),
            )

    def _supervise(self) -> None:
        while not self._supervisor_stop.wait(0.05):
            if self._closed or self._draining:
                return
            worker = self.processes.get("worker")
            mediator = self.processes.get("mediator")
            try:
                if self._audit_failure is not None:
                    with self._lock:
                        self._fail_audit()
                    return
                if (
                    self.directory is not None
                    and read_state(self.directory)["status"] == "draining"
                ):
                    return
                if (worker is not None and worker.poll() is not None) or (
                    mediator is not None and mediator.poll() is not None
                ):
                    self.recover()
                else:
                    self._drain_worker_stats()
                    if self._resource_ceiling_exceeded():
                        with self._lock:
                            self._fail_quota()
                        return
            except (GatewayLifecycleError, GatewayStateError):
                if not self._draining:
                    with self._lock:
                        self._fail_supervision()
                return

    def check(self) -> None:
        if self._audit_failure is not None:
            with self._lock:
                self._fail_audit()
            raise self._audit_failure
        if self._supervisor_failure is not None:
            raise self._supervisor_failure
        worker = self.processes.get("worker")
        if worker is not None and worker.poll() is not None:
            self.recover()
            return
        mediator = self.processes.get("mediator")
        if mediator is not None and mediator.poll() is not None:
            self.recover()
            return
        if self.directory is not None and read_state(self.directory)["status"] == "ready":
            self._drain_worker_stats()
            if self._resource_ceiling_exceeded():
                with self._lock:
                    self._fail_quota()
                raise self._supervisor_failure

    @staticmethod
    def _terminate(process: Any) -> None:
        if process.poll() is not None:
            with suppress(Exception):
                process.wait(timeout=0)
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, AttributeError):
            with suppress(Exception):
                process.terminate()
        try:
            process.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            with suppress(Exception):
                process.kill()
        with suppress(Exception):
            process.wait(timeout=1)

    def _close_endpoints(self) -> None:
        self._supervisor_stop.set()
        for listener in self.listeners:
            with suppress(Exception):
                listener.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                listener.close()
        self.listeners.clear()
        if self.qemu_listener is not None:
            with suppress(Exception):
                self.qemu_listener.close()
            self.qemu_listener = None
        if self.runtime_directory is not None:
            with suppress(FileNotFoundError):
                self.qemu_socket.unlink()

    def drain(self) -> None:
        """Stop accepting controller-owned endpoints before QEMU teardown."""
        with self._lock:
            self._draining = True
            self._close_endpoints()
            self._discard_data_plane()
            if self.directory is not None and not self._closed:
                with suppress(Exception):
                    update_state(self.directory, status="draining")

    def rollback(self, code: str = "cleanup_failed", *, preserve_state: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._draining = True
            self._supervisor_stop.set()
            if self.directory is not None and not preserve_state:
                with suppress(Exception):
                    update_state(
                        self.directory,
                        status="failed",
                        pids=self._process_ids(),
                        failure=("controller", code),
                    )
            self._close_endpoints()
            self._discard_data_plane()
            for stream in self._logs:
                with suppress(Exception):
                    stream.close()
            self._logs.clear()
            self._mediator_policy_payload = None
            self._incarnation_token = b""
            if self.directory is not None:
                if preserve_state:
                    _preserve_stopped_state(self.directory, ownership_fd=self._ownership_fd)
                else:
                    _rollback_state_directory(self.directory, ownership_fd=self._ownership_fd)
            elif self.runtime_directory is not None:
                with suppress(Exception):
                    _remove_runtime_directory(self.runtime_directory)
        if self._supervisor is not None and self._supervisor is not threading.current_thread():
            self._supervisor.join(timeout=1)

    def close(self) -> None:
        self.rollback(preserve_state=True)


from sbx import gateway_supervisor  # noqa: E402 - imported after GatewayController is defined


class PersistentGatewayController:
    """CLI-side handle for a dedicated supervisor subprocess."""

    def __init__(
        self,
        config: GatewayConfig,
        vm_id: str,
        *,
        root: Path | None = None,
        runtime_root: Path | None = None,
        generation: str | None = None,
        timeout: float = 10.0,
        runner: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.config = config
        self.vm_id = _validate_id(vm_id, "VM identity")
        self.generation = generation or uuid.uuid4().hex
        self.root = GATEWAY_STATE_DIR if root is None else root
        self.runtime_root = runtime_root
        if timeout <= 0:
            raise ValueError("gateway lifecycle timeout must be positive")
        self.timeout = timeout
        self.runner = runner
        self.directory: Path | None = None
        self.runtime_directory: Path | None = None
        self.mac = ""
        self._placeholders: dict[str, str] = {}
        self._bootstrap: socket.socket | None = None
        self._process: Any = None
        self._committed = False
        self._lock_fd: int | None = None

    @property
    def qemu_socket(self) -> Path:
        if self.runtime_directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        return self.runtime_directory / "qemu-net.sock"

    @property
    def credential_placeholders(self) -> dict[str, str]:
        return {
            credential.environment: self._placeholders[credential.id]
            for credential in self.config.credentials
            if credential.id in self._placeholders
        }

    def _acquire_lock(self) -> int:
        base = _state_root(self.root)
        vm_directory = base / self.vm_id
        _private_directory(vm_directory, create=True)
        path = vm_directory / "ownership.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise GatewayLifecycleError("gateway is already owned by a live supervisor") from exc
        try:
            generations, _current = _read_vm_state(vm_directory)
            for generation, state in generations.items():
                entry = vm_directory / generation
                if any(_pid_is_running(pid) for pid in state["pids"].values()):
                    raise GatewayLifecycleError(
                        "gateway has live stale processes; stop/start required"
                    )
                if state["status"] == "prepared" and not (entry / "audit.log").exists():
                    _remove_state_directory(entry, ownership_fd=fd)
                else:
                    _preserve_stopped_state(entry, ownership_fd=fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _payload_file(directory: Path, payload: bytes) -> BinaryIO:
        stream = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115
        stream.write(payload)
        stream.seek(0)
        return stream

    def prepare(self, *, provider_environment: Mapping[str, str] | None = None) -> None:
        if self.directory is not None:
            raise GatewayLifecycleError("gateway is already prepared")
        environment = os.environ if provider_environment is None else provider_environment
        self._lock_fd = self._acquire_lock()
        staging = GatewayController(
            self.config,
            self.vm_id,
            root=self.root,
            runtime_root=self.runtime_root,
            generation=self.generation,
            timeout=self.timeout,
        )
        inherited: list[Any] = []
        try:
            inherited_names = {
                name
                for credential in self.config.credentials
                for name in credential.inherit_environment
            }
            missing = sorted(inherited_names - environment.keys())
            if missing:
                raise GatewayLifecycleError(
                    f"credential provider requires missing host environment {missing[0]!r}"
                )
            provider_payload = _compact_json(
                {
                    "baseline": {
                        "HOME": environment.get("HOME", str(Path.home())),
                        "PATH": environment.get("PATH", os.defpath),
                        "TZ": environment.get("TZ", "UTC"),
                    },
                    "inherited": {name: environment[name] for name in inherited_names},
                }
            )
            self.runtime_directory = _create_runtime_directory(
                self.generation, root=self.runtime_root
            )
            self.directory = create_state(
                self.config,
                self.vm_id,
                self.generation,
                root=self.root,
                runtime_directory=self.runtime_directory,
                ownership_fd=self._lock_fd,
            )
            staging.directory = self.directory
            staging.runtime_directory = self.runtime_directory
            staging._reserve_qemu_listener()
            staging._reserve_listeners()
            self.mac = staging.mac
            self._placeholders = dict(staging._placeholders)
            metadata = {
                "config": gateway_supervisor.config_payload(self.config),
                "requested_config_digest": self.config.digest(self.vm_id, self.generation),
                "vm_id": self.vm_id,
                "generation": self.generation,
                "root": str(self.root),
                "directory": str(self.directory),
                "runtime_directory": str(self.runtime_directory),
                "timeout": self.timeout,
                "mac": self.mac,
                "placeholders": self._placeholders,
                "listener_rules": [asdict(rule) for rule in staging.listener_rules],
                "qemu_fd": staging.qemu_listener.fileno(),
                "listener_fds": [listener.fileno() for listener in staging.listeners],
            }
            config_file = self._payload_file(self.directory, _compact_json(metadata))
            provider_file = self._payload_file(self.directory, provider_payload)
            parent, child = socket.socketpair()
            self._bootstrap = parent
            inherited.extend((config_file, provider_file, child))
            pass_fds = (
                child.fileno(),
                config_file.fileno(),
                provider_file.fileno(),
                self._lock_fd,
                staging.qemu_listener.fileno(),
                *(listener.fileno() for listener in staging.listeners),
            )
            command = [
                sys.executable,
                "-m",
                "sbx.gateway_supervisor",
                "--bootstrap-fd",
                str(child.fileno()),
                "--config-fd",
                str(config_file.fileno()),
                "--provider-fd",
                str(provider_file.fileno()),
                "--lock-fd",
                str(self._lock_fd),
            ]
            self._process = self.runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=pass_fds,
                close_fds=True,
                start_new_session=True,
                cwd=self.directory,
                env=_sanitized_child_environment(
                    self.directory, path=os.environ.get("PATH", os.defpath)
                ),
            )
            response = gateway_supervisor.recv_frame(parent, self.timeout)
            if (
                response.get("status") != "prepared"
                or response.get("generation") != self.generation
            ):
                detail = str(response.get("error", "failed to prepare"))
                raise GatewayLifecycleError(f"gateway supervisor failed to prepare: {detail}")
            os.close(self._lock_fd)
            self._lock_fd = None
        except BaseException:
            self.rollback("startup_failed")
            raise
        finally:
            for resource in inherited:
                with suppress(Exception):
                    resource.close()
            if staging.qemu_listener is not None:
                staging.qemu_listener.close()
                staging.qemu_listener = None
            for listener in staging.listeners:
                listener.close()
            staging.listeners.clear()

    def vm_network_ready(self, qemu_pid: int, timeout: float | None = None) -> None:
        if self._bootstrap is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        try:
            gateway_supervisor.send_frame(
                self._bootstrap,
                {
                    "version": 1,
                    "generation": self.generation,
                    "operation": "commit",
                    "qemu_pid": qemu_pid,
                    "timeout": self.timeout if timeout is None else timeout,
                },
            )
            response = gateway_supervisor.recv_frame(
                self._bootstrap, self.timeout if timeout is None else timeout
            )
            if response.get("status") != "ready" or response.get("generation") != self.generation:
                detail = str(response.get("error", "failed readiness commit"))
                raise GatewayLifecycleError(f"gateway supervisor failed readiness commit: {detail}")
        except BaseException:
            self.rollback("startup_failed")
            raise
        self._committed = True
        self._bootstrap.close()
        self._bootstrap = None

    def _request(self, operation: str) -> dict[str, Any]:
        if self.directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        return control_request(self.vm_id, operation, root=self.root, timeout=self.timeout)

    def check(self) -> None:
        response = self._request("check")
        if response.get("status") == "failed":
            raise GatewayLifecycleError("gateway supervisor reports failure")

    def drain(self) -> None:
        if self._committed:
            self._request("drain")

    def rollback(self, code: str = "cleanup_failed") -> None:
        del code
        if self._committed:
            try:
                self._request("shutdown")
            except GatewayLifecycleError:
                stop(self.vm_id, root=self.root)
        else:
            if self._bootstrap is not None:
                self._bootstrap.close()
                self._bootstrap = None
            if self._process is not None:
                GatewayController._terminate(self._process)
            if self.directory is not None and self.directory.exists():
                _rollback_state_directory(self.directory, ownership_fd=self._lock_fd)
            elif self.runtime_directory is not None and self.runtime_directory.exists():
                with suppress(Exception):
                    _remove_runtime_directory(self.runtime_directory)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._committed = False

    close = rollback


def control_request(
    vm_id: str,
    operation: str,
    *,
    root: Path | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    directory = _latest_directory(vm_id, root)
    if directory is None:
        raise GatewayLifecycleError("gateway supervisor is inactive")
    state = read_state(directory, vm_id=vm_id)
    path_value = state["sockets"].get("control")
    if not isinstance(path_value, str):
        raise GatewayLifecycleError("gateway supervisor is inactive")
    path = Path(path_value)
    _reject_symlink_ancestors(path.parent)
    _private_directory(path.parent)
    try:
        info = path.lstat()
    except OSError as exc:
        raise GatewayLifecycleError("gateway supervisor is unresponsive") from exc
    if (
        not stat.S_ISSOCK(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise GatewayStateError("unsafe gateway control socket")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
        gateway_supervisor.send_frame(
            client,
            {"version": 1, "generation": state["generation"], "operation": operation},
        )
        response = gateway_supervisor.recv_frame(client, timeout)
    except OSError as exc:
        raise GatewayLifecycleError("gateway supervisor is unresponsive") from exc
    finally:
        client.close()
    if (
        response.get("version") != 1
        or response.get("generation") != state["generation"]
        or "error" in response
    ):
        raise GatewayLifecycleError(str(response.get("error", "gateway control identity mismatch")))
    return response


def drain(vm_id: str, *, root: Path | None = None) -> None:
    directory = _latest_directory(vm_id, root)
    if directory is None:
        return
    control_request(vm_id, "drain", root=root)


def _rollback_state_directory(directory: Path, *, ownership_fd: int | None = None) -> None:
    audit = directory / "audit.log"
    if audit.exists() or audit.is_symlink():
        info = audit.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise GatewayStateError("unsafe gateway audit file")
        if info.st_size > 0:
            _preserve_stopped_state(directory, ownership_fd=ownership_fd)
            return
    _remove_state_directory(directory, ownership_fd=ownership_fd)


def _preserve_stopped_state(directory: Path, *, ownership_fd: int | None = None) -> None:
    """Remove runtime secrets/endpoints, retaining generation audit evidence."""
    with _state_lock(directory.parent, ownership_fd):
        _private_directory(directory)
        state = read_state(directory)
        runtime = state["runtime_directory"]
        if isinstance(runtime, str):
            path = Path(runtime)
            if path.exists() or path.is_symlink():
                _remove_runtime_directory(path)
        for entry in directory.iterdir():
            if entry.name in {"web-auth.json", "web.json"} or (
                entry.name.startswith("mitmproxy-") and not entry.name.endswith(".log")
            ):
                entry.unlink()
        update_state(
            directory,
            status="stopped",
            pids={},
            sockets={} if runtime is not None else None,
            listeners=[],
        )


def _remove_state_directory(directory: Path, *, ownership_fd: int | None = None) -> None:
    vm_directory = directory.parent
    with _state_lock(vm_directory, ownership_fd):
        _private_directory(directory)
        state = read_state(directory)
        _generations, current = _read_vm_state(vm_directory)
        if current == directory.name:
            # The selected directory remains intact if clearing/syncing its reference fails.
            _write_current(vm_directory, None)
        runtime_value = state["runtime_directory"]
        if isinstance(runtime_value, str):
            runtime_directory = Path(runtime_value)
            if runtime_directory.exists() or runtime_directory.is_symlink():
                _remove_runtime_directory(runtime_directory)
        shutil.rmtree(directory)


def _latest_directory(vm_id: str, root: Path | None = None) -> Path | None:
    state, error = _latest_state(vm_id, root)
    if error is not None:
        raise GatewayStateError(f"unsafe gateway state: {error}")
    if state is None:
        return None
    base = GATEWAY_STATE_DIR if root is None else root
    return base / vm_id / state["generation"]


def _pid_is_running(pid: int) -> bool:
    if not pid_is_alive(pid):
        return False
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (IndexError, OSError):
        return True
    return not fields or fields[0] != "Z"


def _process_matches(pid: int, component: str, directory: Path) -> bool:
    if not _pid_is_running(pid):
        return True
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    marker = b"sbx-gateway" if component == "worker" else b"sbx_gateway_mediator.py"
    return marker in command and os.fsencode(directory) in command


def _fallback_lock(directory: Path, *, timeout: float = 0) -> int:
    path = directory.parent / "ownership.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # Shutdown acknowledgement can precede process exit and lock release.
            if time.monotonic() >= deadline:
                os.close(fd)
                raise GatewayStateError("gateway supervisor still owns the lifecycle lock") from exc
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        else:
            return fd


def _pidfd_open(pid: int) -> int:
    if opener := getattr(os, "pidfd_open", None):
        return opener(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    fd = int(libc.syscall(434, pid, 0))
    if fd < 0:
        error = ctypes.get_errno()
        if error == 3:
            raise ProcessLookupError(pid)
        if error in {22, 38}:
            raise GatewayStateError("safe gateway cleanup requires Linux pidfd support")
        raise OSError(error, os.strerror(error))
    return fd


def _pidfd_send_signal(fd: int, sig: signal.Signals) -> None:
    if sender := getattr(signal, "pidfd_send_signal", None):
        sender(fd, sig)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(424, fd, int(sig), 0, 0) < 0:
        error = ctypes.get_errno()
        if error in {22, 38}:
            raise GatewayStateError("safe gateway cleanup requires Linux pidfd signaling")
        if error != 3:
            raise OSError(error, os.strerror(error))


def _pidfd_for_owned_process(pid: int, component: str, directory: Path) -> int:
    if sys.platform != "linux":
        raise GatewayStateError(
            "refusing unauthenticated macOS orphan cleanup; manual cleanup is required"
        )
    try:
        fd = _pidfd_open(pid)
    except ProcessLookupError:
        return -1
    if component == "supervisor":
        try:
            command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
            cwd = (Path("/proc") / str(pid) / "cwd").resolve()
        except OSError:
            os.close(fd)
            raise GatewayStateError("refusing unverified gateway supervisor process") from None
        matches = b"sbx.gateway_supervisor" in command and cwd == directory.resolve()
    else:
        matches = _process_matches(pid, component, directory)
    if not matches:
        os.close(fd)
        raise GatewayStateError(f"refusing unverified gateway {component} process")
    return fd


def stop(vm_id: str, *, root: Path | None = None) -> None:
    """Shutdown a drained supervisor, using pidfds only after its lock is released."""
    directory = _latest_directory(vm_id, root)
    if directory is None or read_state(directory, vm_id=vm_id)["status"] == "stopped":
        vm_directory = (GATEWAY_STATE_DIR if root is None else root) / vm_id
        if vm_directory.exists():
            with _state_lock(vm_directory, timeout=3):
                states, current = _read_vm_state(vm_directory)
                if current is not None and states[current]["status"] != "stopped":
                    raise GatewayStateError("gateway lifecycle changed; retry stop")
        return
    try:
        control_request(vm_id, "shutdown", root=root)
        return
    except GatewayLifecycleError:
        pass
    lock_fd = _fallback_lock(directory)
    pidfds: list[tuple[str, int]] = []
    try:
        state = read_state(directory, vm_id=vm_id)
        for component in ("supervisor", "worker", "mediator"):
            pid = state["pids"].get(component)
            if pid and _pid_is_running(pid):
                fd = _pidfd_for_owned_process(pid, component, directory)
                if fd >= 0:
                    pidfds.append((component, fd))
        for _, fd in pidfds:
            _pidfd_send_signal(fd, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            _pid_is_running(state["pids"].get(name, 0))
            for name in ("supervisor", "worker", "mediator")
        ):
            time.sleep(0.05)
        for name, fd in pidfds:
            if _pid_is_running(state["pids"].get(name, 0)):
                _pidfd_send_signal(fd, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and any(
            _pid_is_running(state["pids"].get(name, 0))
            for name in ("supervisor", "worker", "mediator")
        ):
            time.sleep(0.05)
        if any(
            _pid_is_running(state["pids"].get(name, 0))
            for name in ("supervisor", "worker", "mediator")
        ):
            raise GatewayStateError("gateway process did not stop")
        _preserve_stopped_state(directory, ownership_fd=lock_fd)
    finally:
        for _, fd in pidfds:
            os.close(fd)
        os.close(lock_fd)


def remove(vm_id: str, *, root: Path | None = None) -> None:
    """Explicit VM removal deletes retained generations under the ownership lock."""
    stop(vm_id, root=root)
    vm_directory = (GATEWAY_STATE_DIR if root is None else root) / vm_id
    if not vm_directory.exists():
        return
    lock_fd = _fallback_lock(vm_directory / "state", timeout=3)
    try:
        generations, _current = _read_vm_state(vm_directory)
        for state in generations.values():
            if any(_pid_is_running(pid) for pid in state["pids"].values()):
                raise GatewayStateError("refusing to remove live gateway generation")
        for generation in generations:
            _remove_state_directory(vm_directory / generation, ownership_fd=lock_fd)
        pointer = vm_directory / "current.json"
        if pointer.exists():
            pointer.unlink()
            directory_fd = os.open(vm_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        os.close(lock_fd)


def active(vm_id: str, *, root: Path | None = None) -> bool:
    try:
        return control_request(vm_id, "status", root=root).get("status") in {
            "ready",
            "recovering",
        }
    except (GatewayLifecycleError, GatewayStateError):
        return False


def configuration_drift(vm_id: str, configured: GatewayConfig, *, root: Path | None = None) -> bool:
    response = control_request(vm_id, "status", root=root)
    return response["config_digest"] != configured.digest(vm_id, response["generation"])


def _latest_state(vm_id: str, root: Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    _validate_id(vm_id, "VM identity")
    base = GATEWAY_STATE_DIR if root is None else root
    vm_directory = base / vm_id
    if not base.exists() and not base.is_symlink():
        return None, None
    try:
        _private_directory(base)
        if not vm_directory.exists() and not vm_directory.is_symlink():
            return None, None
        _private_directory(vm_directory)
        generations, current = _read_vm_state(vm_directory)
        return (None if current is None else generations[current]), None
    except GatewayStateError as exc:
        return None, str(exc)


def _web_diagnostic_lines(vm_id: str, state: Mapping[str, Any], *, root: Path | None) -> list[str]:
    directory = _state_root(root) / vm_id / state["generation"]
    try:
        web = _read_json(directory / "web.json")
        port = web.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            return []
    except GatewayStateError:
        return []
    return [
        f"Gateway web UI: http://127.0.0.1:{port}/ (read-only; active only while ready)",
        f"Gateway web authentication file: {directory / 'web-auth.json'}",
    ]


def diagnostic_lines(
    vm_id: str,
    *,
    configured: GatewayConfig | None = None,
    root: Path | None = None,
    include_disabled: bool = False,
) -> list[str]:
    state, error = _latest_state(vm_id, root)
    if state is None and error is None and configured is None:
        return ["Gateway: disabled"] if include_disabled else []
    if error is not None:
        return ["Gateway: error", "Gateway state: unsafe or corrupt (details redacted)"]
    if state is None:
        platform_note = (
            " (unverified on macOS)" if configured and configured.platform == "darwin" else ""
        )
        return [f"Gateway: configured{platform_note}", "Gateway state: inactive"]

    pids = state["pids"]
    try:
        live = control_request(vm_id, "diagnostics", root=root)
    except GatewayLifecycleError:
        live = None
    qemu_stale = "qemu" in pids and not pid_is_alive(pids["qemu"])
    if live is not None:
        status = "stale QEMU" if qemu_stale else str(live["status"])
        state["counters"] = live["counters"]
        state["last_failure"] = live["last_failure"]
    elif any(_pid_is_running(pids.get(name, 0)) for name in ("worker", "mediator")):
        status = "dead supervisor with orphaned children"
    elif state["status"] in _ACTIVE_STATES:
        status = "stale"
    else:
        status = state["status"]
    platform_note = " (unverified on macOS)" if state["platform"] == "darwin" else ""
    process_detail = (
        ", ".join(
            f"{name}=active" if pid_is_alive(pid) else f"{name}=stale"
            for name, pid in sorted(pids.items())
        )
        or "-"
    )
    sockets = ", ".join(f"{name}=recorded" for name in sorted(state["sockets"])) or "-"
    listeners = (
        ", ".join(
            f"{item['listen_host']}:{item['host_port']} -> "
            f"guest {item['guest_address']}:{item['guest_port']}"
            for item in state["listeners"]
        )
        or "-"
    )
    counters = (
        ", ".join(f"{name}={value}" for name, value in sorted(state["counters"].items())) or "-"
    )
    failure = state["last_failure"]
    failure_detail = "-" if failure is None else f"{failure['component']}/{failure['code']}"
    return [
        f"Gateway: enabled{platform_note}",
        f"Gateway state: {status}",
        f"Gateway generation: {state['generation']}",
        f"Gateway config digest: {state['config_digest']}",
        *(
            ["Gateway configuration: drift (stop/start required)"]
            if configured is not None
            and configured.digest(vm_id, state["generation"]) != state["requested_config_digest"]
            else ["Gateway configuration: current"]
        ),
        f"Gateway processes: {process_detail}",
        f"Gateway sockets: {sockets}",
        f"Gateway ingress: {listeners}",
        f"Gateway counters: {counters}",
        f"Gateway last failure: {failure_detail}",
        f"Gateway audit log: {_state_root(root) / vm_id / state['generation'] / 'audit.log'}",
        *_web_diagnostic_lines(vm_id, state, root=root),
    ]
