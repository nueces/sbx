from __future__ import annotations

import hashlib
import ipaddress
import json
import os
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
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sbx.constants import SBX_STATE_DIR
from sbx.runtime import ConfigError, pid_is_alive

GATEWAY_STATE_DIR = SBX_STATE_DIR / "gateway"
GATEWAY_SOURCE_DIR = Path(__file__).resolve().parents[2] / "gateway"
GATEWAY_WORKER = GATEWAY_SOURCE_DIR / "sbx-gateway"
GATEWAY_MEDIATOR_PYTHON = GATEWAY_SOURCE_DIR / "mediator" / ".venv" / "bin" / "python"
GATEWAY_MEDIATOR_LAUNCHER = GATEWAY_SOURCE_DIR / "mediator" / "launch.py"
GUEST_ADDRESS = "192.0.2.2"
APPROVED_HOOKS = frozenset({"package-audit"})
_ALLOWED_NETWORK_KEYS = frozenset(
    {
        "default",
        "http",
        "tcp",
        "tls_passthrough",
        "secret",
        "ingress",
        "quotas",
        "reconnection",
    }
)
_ALLOWED_FAILURES = frozenset(
    {
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


@dataclass(frozen=True)
class SecretBinding:
    name: str
    provider: str
    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    header: str
    methods: tuple[str, ...] = ()
    path_prefix: str | None = None


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
    secrets: tuple[SecretBinding, ...]
    ingress: tuple[IngressRule, ...]
    quotas: Quotas
    reconnection: Reconnection
    platform: str

    def canonical(self, vm_id: str, generation: str) -> dict[str, Any]:
        _validate_id(vm_id, "VM identity")
        _validate_id(generation, "gateway generation")
        return {
            "version": 1,
            "vm_id": vm_id,
            "generation": generation,
            "default": "deny",
            "platform": self.platform,
            "rules": [asdict(rule) for rule in self.rules],
            "secrets": [asdict(binding) for binding in self.secrets],
            "ingress": [asdict(rule) for rule in self.ingress],
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
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be a non-empty array of strings")
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


def _methods(value: object, label: str) -> tuple[str, ...]:
    methods = _strings(value, label) if value is not None else ()
    normalized = tuple(sorted({method.upper() for method in methods}))
    if any(_TOKEN_RE.fullmatch(method) is None for method in normalized):
        raise ConfigError(f"{label} contains an invalid HTTP method")
    return normalized


def _paths(value: object, label: str) -> tuple[str, ...]:
    paths = _strings(value, label) if value is not None else ()
    normalized = []
    for path in paths:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path.split("/"):
            raise ConfigError(f"{label} contains an unsafe path")
        normalized.append(posixpath.normpath(path))
    return tuple(sorted(set(normalized)))


def _hooks(value: object, label: str) -> tuple[str, ...]:
    hooks = _strings(value, label) if value is not None else ()
    if len(set(hooks)) != len(hooks):
        raise ConfigError(f"{label} contains a duplicate hook")
    unknown = sorted(set(hooks) - APPROVED_HOOKS)
    if unknown:
        raise ConfigError(f"{label} names unapproved hook(s): {', '.join(unknown)}")
    return hooks


def _rule(item: Mapping[str, Any], protocol: str, index: int) -> PolicyRule:
    label = f"[[network.{protocol if protocol != 'tls' else 'tls_passthrough'}]]"
    allowed = {"id", "host", "ports", "allow_ranges"}
    if protocol == "http":
        allowed |= {"methods", "paths", "hooks"}
    _reject_unknown(item, allowed, label)
    rule_id = item.get("id", f"{protocol}-{index}")
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise ConfigError(f"{label}.id is invalid")
    host = _normalize_host(item.get("host"), f"{label}.host", exact=protocol == "tls")
    ports = _ports(item.get("ports"), f"{label}.ports")
    methods = _methods(item.get("methods"), f"{label}.methods") if protocol == "http" else ()
    paths = _paths(item.get("paths"), f"{label}.paths") if protocol == "http" else ()
    hooks = _hooks(item.get("hooks"), f"{label}.hooks") if protocol == "http" else ()
    ranges = (
        _strings(item.get("allow_ranges"), f"{label}.allow_ranges")
        if item.get("allow_ranges") is not None
        else ()
    )
    normalized_ranges: list[str] = []
    for value in ranges:
        try:
            normalized_ranges.append(str(ipaddress.ip_network(value, strict=False)))
        except ValueError as exc:
            raise ConfigError(f"{label}.allow_ranges contains an invalid network") from exc
    if len(set(normalized_ranges)) != len(normalized_ranges):
        raise ConfigError(f"{label}.allow_ranges contains a duplicate network")
    return PolicyRule(
        rule_id,
        protocol,
        host,
        ports,
        methods,
        paths,
        tuple(sorted(normalized_ranges)),
        hooks,
    )


def _provider(value: object, label: str) -> str:
    if not isinstance(value, str) or ":" not in value or "\x00" in value:
        raise ConfigError(f"{label} must be a supported provider reference")
    kind, reference = value.split(":", 1)
    if kind == "env" and _ENV_RE.fullmatch(reference):
        return value
    if kind == "file" and Path(reference).is_absolute() and ".." not in Path(reference).parts:
        return value
    if kind == "aws-codeartifact" and _RULE_ID_RE.fullmatch(reference):
        return value
    raise ConfigError(f"{label} must use env:NAME, file:/absolute/path, or aws-codeartifact:NAME")


def _secret(item: Mapping[str, Any], index: int) -> SecretBinding:
    label = "[[network.secret]]"
    _reject_unknown(
        item,
        {"name", "provider", "hosts", "ports", "header", "methods", "path_prefix"},
        label,
    )
    name = item.get("name")
    if not isinstance(name, str) or _ENV_RE.fullmatch(name) is None:
        raise ConfigError(f"{label}.name is invalid")
    hosts = tuple(
        sorted(
            {
                _normalize_host(host, f"{label}.hosts", exact=True)
                for host in _strings(item.get("hosts"), f"{label}.hosts", required=True)
            }
        )
    )
    ports = _ports(item.get("ports"), f"{label}.ports")
    header = item.get("header")
    if (
        not isinstance(header, str)
        or header.count("{placeholder}") != 1
        or "\n" in header
        or "\r" in header
        or ":" not in header
    ):
        raise ConfigError(f"{label}.header must contain one structural {{placeholder}}")
    header_name, _ = header.split(":", 1)
    if _TOKEN_RE.fullmatch(header_name.strip()) is None:
        raise ConfigError(f"{label}.header has an invalid field name")
    path_prefix = item.get("path_prefix")
    if path_prefix is not None:
        if (
            not isinstance(path_prefix, str)
            or not path_prefix.startswith("/")
            or ".." in path_prefix.split("/")
            or "?" in path_prefix
            or "#" in path_prefix
        ):
            raise ConfigError(f"{label}.path_prefix is unsafe")
        path_prefix = posixpath.normpath(path_prefix)
    return SecretBinding(
        name,
        _provider(item.get("provider"), f"{label}.provider"),
        hosts,
        ports,
        header,
        _methods(item.get("methods"), f"{label}.methods"),
        path_prefix,
    )


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

    rules = tuple(
        _rule(item, protocol, index)
        for protocol, key in (("http", "http"), ("tcp", "tcp"), ("tls", "tls_passthrough"))
        for index, item in enumerate(_tables(network.get(key), f"[[network.{key}]]"), 1)
    )
    ids = [rule.id for rule in rules]
    if len(set(ids)) != len(ids):
        raise ConfigError("network policy rule IDs must be unique")
    destinations: dict[tuple[str, str, int], str] = {}
    for rule in rules:
        transport = "tcp" if rule.protocol in {"http", "tls"} else rule.protocol
        for port in rule.ports:
            key = (transport, rule.host, port)
            if key in destinations:
                raise ConfigError(f"network rules {destinations[key]!r} and {rule.id!r} conflict")
            destinations[key] = rule.id

    secrets = tuple(
        _secret(item, index)
        for index, item in enumerate(_tables(network.get("secret"), "[[network.secret]]"), 1)
    )
    secret_names = [binding.name for binding in secrets]
    if len(set(secret_names)) != len(secret_names):
        raise ConfigError("network secret binding names must be unique")
    http_rules = [rule for rule in rules if rule.protocol == "http"]
    for binding in secrets:
        for host in binding.hosts:
            for port in binding.ports:
                if not any(rule.host == host and port in rule.ports for rule in http_rules):
                    raise ConfigError(
                        f"network secret {binding.name!r} is not scoped to an exact HTTP policy"
                    )

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

    return GatewayConfig(
        tuple(sorted(rules, key=lambda rule: rule.id)),
        tuple(sorted(secrets, key=lambda binding: binding.name)),
        tuple(ingress),
        _quotas(network.get("quotas")),
        _reconnection(network.get("reconnection")),
        selected_platform,
    )


parse_gateway_config = parse_config


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


def _state_root(root: Path | None = None) -> Path:
    selected = GATEWAY_STATE_DIR if root is None else root
    parent = selected.parent
    if parent.exists() and parent.is_symlink():
        raise GatewayStateError(f"unsafe gateway state parent: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    _private_directory(selected, create=True)
    return selected


def _atomic_json(directory: Path, name: str, data: Mapping[str, Any]) -> None:
    _private_directory(directory)
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
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


def _new_state(config: GatewayConfig, vm_id: str, generation: str) -> dict[str, Any]:
    return {
        "version": 1,
        "vm_id": vm_id,
        "generation": generation,
        "config_digest": config.digest(vm_id, generation),
        "platform": config.platform,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "prepared",
        "pids": {},
        "sockets": {},
        "listeners": [],
        "counters": {},
        "last_failure": None,
    }


def create_state(
    config: GatewayConfig,
    vm_id: str,
    generation: str,
    *,
    root: Path | None = None,
) -> Path:
    _validate_id(vm_id, "VM identity")
    _validate_id(generation, "gateway generation")
    base = _state_root(root)
    vm_directory = base / vm_id
    _private_directory(vm_directory, create=True)
    directory = vm_directory / generation
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise GatewayStateError(f"gateway incarnation state already exists: {directory}") from exc
    _private_directory(directory)
    _atomic_json(directory, "policy.json", config.worker_config(vm_id, generation))
    _atomic_json(directory, "state.json", _new_state(config, vm_id, generation))
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
    except (OSError, json.JSONDecodeError) as exc:
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
        "config_digest",
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
    digest = data.get("config_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
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
        or set(pids) - {"worker", "mediator", "qemu"}
        or any(type(pid) is not int or pid <= 0 for pid in pids.values())
    ):
        raise GatewayStateError("corrupt gateway process metadata")
    sockets = data.get("sockets")
    if (
        not isinstance(sockets, dict)
        or set(sockets) - {"qemu", "control"}
        or any(not isinstance(value, str) for value in sockets.values())
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
        if set(pids) - {"worker", "mediator", "qemu"} or any(
            type(pid) is not int or pid <= 0 for pid in pids.values()
        ):
            raise GatewayStateError("invalid gateway process metadata")
        data["pids"] = dict(pids)
    if sockets is not None:
        checked: dict[str, str] = {}
        if set(sockets) - {"qemu", "control"}:
            raise GatewayStateError("invalid gateway socket metadata")
        for name, raw_path in sockets.items():
            path = Path(raw_path)
            if path.parent != directory or path.name not in {"qemu-net.sock", "control.sock"}:
                raise GatewayStateError("gateway socket path crosses its incarnation directory")
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


class GatewayController:
    """Own one gateway incarnation from reserved listeners through teardown."""

    def __init__(
        self,
        config: GatewayConfig,
        vm_id: str,
        *,
        root: Path | None = None,
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
        self.runner = runner
        self.socket_factory = socket_factory
        if timeout <= 0:
            raise ValueError("gateway lifecycle timeout must be positive")
        self.timeout = timeout
        self.directory: Path | None = None
        self.listeners: list[socket.socket] = []
        self.listener_rules: tuple[IngressRule, ...] = ()
        self.processes: dict[str, Any] = {}
        self._worker_stdout: BinaryIO | None = None
        self._worker_buffer = bytearray()
        self._logs: list[BinaryIO] = []
        self._closed = False
        self.mediator_address: str | None = None
        self.worker_incarnation = uuid.uuid4().hex
        self._placeholders = {
            binding.name: f"SBX_SECRET_{binding.name}_{secrets.token_urlsafe(24)}"
            for binding in config.secrets
        }
        random = bytearray(secrets.token_bytes(6))
        random[0] = (random[0] | 2) & 0xFE
        self.mac = ":".join(f"{value:02x}" for value in random)

    @property
    def qemu_socket(self) -> Path:
        if self.directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        return self.directory / "qemu-net.sock"

    @property
    def secret_placeholders(self) -> dict[str, str]:
        return dict(self._placeholders)

    def _open_log(self, name: str) -> BinaryIO:
        assert self.directory is not None
        fd = os.open(
            self.directory / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        stream = os.fdopen(fd, "wb", buffering=0)
        self._logs.append(stream)
        return stream

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
        for binding in self.config.secrets:
            kind, reference = binding.provider.split(":", 1)
            if kind == "env":
                value = os.environ.get(reference)
            elif kind == "file":
                try:
                    fd = os.open(
                        reference,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        info = os.fstat(fd)
                        raw = os.read(fd, 65537)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    raise GatewayLifecycleError(
                        "gateway credential provider is unavailable"
                    ) from exc
                try:
                    value = (
                        raw.decode(errors="strict").strip() if stat.S_ISREG(info.st_mode) else None
                    )
                except UnicodeError as exc:
                    raise GatewayLifecycleError(
                        "gateway credential provider is unavailable"
                    ) from exc
            else:
                value = None
            if (
                not value
                or len(value.encode()) > 65536
                or any(character in value for character in "\r\n\x00")
            ):
                raise GatewayLifecycleError("gateway credential provider is unavailable")
            bindings.append(
                {
                    "name": binding.name,
                    "hosts": list(binding.hosts),
                    "ports": list(binding.ports),
                    "methods": list(binding.methods),
                    "path_prefix": binding.path_prefix,
                    "header": binding.header,
                    "placeholder": self._placeholders[binding.name],
                    "value": value,
                }
            )
        payload = _compact_json({"version": 1, "rules": rules, "secrets": bindings})
        if len(payload) > 65536:
            raise GatewayLifecycleError("gateway mediator policy is too large")
        return payload

    def _mediator_command(self, key_fd: int, ready_fd: int, policy_fd: int) -> list[str]:
        assert self.directory is not None
        python = Path(os.environ.get("SBX_GATEWAY_MEDIATOR_PYTHON", GATEWAY_MEDIATOR_PYTHON))
        launcher = Path(os.environ.get("SBX_GATEWAY_MEDIATOR_LAUNCHER", GATEWAY_MEDIATOR_LAUNCHER))
        if not python.is_file() or not launcher.is_file():
            raise GatewayLifecycleError("pinned gateway mediator runtime is missing")
        return [
            str(python),
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
            "--confdir",
            str(self.directory),
        ]

    def _worker_command(self, key_fd: int | None) -> list[str]:
        assert self.directory is not None
        worker = Path(os.environ.get("SBX_GATEWAY_BIN", GATEWAY_WORKER))
        if not worker.is_file():
            raise GatewayLifecycleError("exact gateway worker artifact is missing")
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
        ]
        if key_fd is not None and self.mediator_address is not None:
            command.extend(
                (
                    "--capability-key-fd",
                    str(key_fd),
                    "--worker-incarnation",
                    self.worker_incarnation,
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

    def _read_worker_status(self, expected: str) -> dict[str, Any]:
        process = self.processes["worker"]
        stdout = self._worker_stdout
        if stdout is None:
            raise GatewayLifecycleError("worker readiness channel is unavailable")
        deadline = time.monotonic() + self.timeout
        while True:
            payload = self._read_json_fd(
                stdout.fileno(), process, "worker", self._worker_buffer, deadline
            )
            status = payload.get("status")
            if status == expected:
                return payload
            if status != "stats":
                raise GatewayLifecycleError("worker returned an unexpected readiness status")

    def prepare(self) -> None:
        """Reserve resources and authenticate mediator/worker readiness before QEMU."""
        if self.directory is not None:
            raise GatewayLifecycleError("gateway is already prepared")
        mediator_policy = self._mediator_policy()
        try:
            self.directory = create_state(self.config, self.vm_id, self.generation, root=self.root)
            update_state(self.directory, status="starting")
            self._reserve_listeners()
            update_state(self.directory, listeners=self.listener_rules)

            key = secrets.token_bytes(32)
            mediator_key = self._pipe_with(key)
            mediator_policy_fd, mediator_policy_write = os.pipe()
            ready_read, ready_write = os.pipe()
            try:
                try:
                    mediator = self.runner(
                        self._mediator_command(mediator_key, ready_write, mediator_policy_fd),
                        stdin=subprocess.DEVNULL,
                        stdout=self._open_log("mediator.log"),
                        stderr=subprocess.STDOUT,
                        pass_fds=(mediator_key, ready_write, mediator_policy_fd),
                        start_new_session=True,
                    )
                    self.processes["mediator"] = mediator
                finally:
                    os.close(mediator_key)
                    os.close(mediator_policy_fd)
                    os.close(ready_write)
                view = memoryview(mediator_policy)
                while view:
                    view = view[os.write(mediator_policy_write, view) :]
                os.close(mediator_policy_write)
                mediator_policy_write = -1
                ready = self._read_json_fd(ready_read, mediator, "mediator")
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
            ):
                raise GatewayLifecycleError("mediator readiness identity mismatch")
            self.mediator_address = f"127.0.0.1:{ready['port']}"

            has_http = any(rule.protocol == "http" for rule in self.config.rules)
            worker_key = self._pipe_with(key) if has_http else None
            try:
                ingress_fds = tuple(listener.fileno() for listener in self.listeners)
                worker = self.runner(
                    self._worker_command(worker_key),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=self._open_log("worker.log"),
                    pass_fds=ingress_fds
                    if worker_key is None
                    else (worker_key, *ingress_fds),
                    start_new_session=True,
                )
                self.processes["worker"] = worker
                self._worker_stdout = worker.stdout
            finally:
                if worker_key is not None:
                    os.close(worker_key)
            ready = self._read_worker_status("ready")
            policy = _read_json(self.directory / "policy.json")
            if (
                ready.get("version") != 1
                or ready.get("vm_id") != self.vm_id
                or ready.get("generation") != self.generation
                or ready.get("digest") != policy.get("digest")
                or ready.get("mediator") is not has_http
                or ready.get("ingress") != len(self.listener_rules)
            ):
                raise GatewayLifecycleError("worker readiness identity mismatch")
            if not self.qemu_socket.exists() or not stat.S_ISSOCK(self.qemu_socket.lstat().st_mode):
                raise GatewayLifecycleError("worker did not create its private QEMU socket")
            update_state(
                self.directory,
                pids={name: process.pid for name, process in self.processes.items()},
                sockets={"qemu": self.qemu_socket},
            )
        except BaseException:
            self.rollback("startup_failed")
            raise

    def vm_network_ready(self, qemu_pid: int) -> None:
        """Complete readiness only after QEMU exchanges a valid framed packet."""
        if self.directory is None:
            raise GatewayLifecycleError("gateway has not been prepared")
        try:
            self._read_worker_status("network-ready")
            if any(process.poll() is not None for process in self.processes.values()):
                raise GatewayLifecycleError("gateway component exited during startup")
            pids = {name: process.pid for name, process in self.processes.items()}
            pids["qemu"] = qemu_pid
            update_state(self.directory, status="ready", pids=pids)
        except BaseException:
            self.rollback("startup_failed")
            raise

    def check(self) -> None:
        failed = next(
            (name for name, process in self.processes.items() if process.poll() is not None), None
        )
        if failed is None:
            return
        if self.directory is not None:
            update_state(self.directory, status="failed", failure=(failed, f"{failed}_failed"))
        raise GatewayLifecycleError(f"gateway {failed} exited")

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

    def drain(self) -> None:
        """Stop accepting ingress before QEMU teardown."""
        for listener in self.listeners:
            with suppress(Exception):
                listener.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                listener.close()
        self.listeners.clear()
        if self.directory is not None and not self._closed:
            with suppress(Exception):
                update_state(self.directory, status="draining")

    def rollback(self, code: str = "cleanup_failed") -> None:
        if self._closed:
            return
        self._closed = True
        if self.directory is not None:
            with suppress(Exception):
                update_state(
                    self.directory,
                    status="failed",
                    pids={name: process.pid for name, process in self.processes.items()},
                    failure=("controller", code),
                )
        self.drain()
        for process in reversed(tuple(self.processes.values())):
            self._terminate(process)
        self.processes.clear()
        if self._worker_stdout is not None:
            with suppress(Exception):
                self._worker_stdout.close()
            self._worker_stdout = None
        for stream in self._logs:
            with suppress(Exception):
                stream.close()
        self._logs.clear()
        if self.directory is not None:
            with suppress(Exception):
                _remove_state_directory(self.directory)

    close = rollback


def _remove_state_directory(directory: Path) -> None:
    _private_directory(directory)
    vm_directory = directory.parent
    shutil.rmtree(directory)
    with suppress(OSError):
        vm_directory.rmdir()


def _latest_directory(vm_id: str, root: Path | None = None) -> Path | None:
    state, error = _latest_state(vm_id, root)
    if error is not None:
        raise GatewayStateError("unsafe gateway state")
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


def stop(vm_id: str, *, root: Path | None = None) -> None:
    """Stop a persisted gateway without trusting arbitrary PIDs from stale state."""
    directory = _latest_directory(vm_id, root)
    if directory is None:
        return
    state = read_state(directory, vm_id=vm_id)
    for component in ("worker", "mediator"):
        pid = state["pids"].get(component)
        if pid is None or not _pid_is_running(pid):
            continue
        if not _process_matches(pid, component, directory):
            raise GatewayStateError(f"refusing unverified gateway {component} process")
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(
        _pid_is_running(state["pids"].get(name, 0)) for name in ("worker", "mediator")
    ):
        time.sleep(0.05)
    remaining = []
    for component in ("worker", "mediator"):
        pid = state["pids"].get(component)
        if pid and _pid_is_running(pid):
            with suppress(ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            remaining.append((component, pid))
    deadline = time.monotonic() + 1
    while remaining and time.monotonic() < deadline:
        remaining = [(name, pid) for name, pid in remaining if _pid_is_running(pid)]
        if remaining:
            time.sleep(0.05)
    if remaining:
        raise GatewayStateError(f"gateway {remaining[0][0]} did not stop")
    _remove_state_directory(directory)


def active(vm_id: str, *, root: Path | None = None) -> bool:
    directory = _latest_directory(vm_id, root)
    if directory is None:
        return False
    state = read_state(directory, vm_id=vm_id)
    return state["status"] == "ready" and all(
        _pid_is_running(state["pids"].get(name, 0)) for name in ("qemu", "worker", "mediator")
    )


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
        candidates = []
        for entry in vm_directory.iterdir():
            if entry.is_symlink() or not entry.is_dir() or _ID_RE.fullmatch(entry.name) is None:
                raise GatewayStateError("unsafe gateway incarnation entry")
            state = read_state(entry, vm_id=vm_id)
            candidates.append(state)
        if not candidates:
            return None, None
        return max(candidates, key=lambda item: str(item["created_at"])), None
    except GatewayStateError as exc:
        return None, str(exc)


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
    stale = state["status"] in _ACTIVE_STATES and (
        not pids or any(not pid_is_alive(pid) for pid in pids.values())
    )
    status = "stale" if stale else state["status"]
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
        f"Gateway processes: {process_detail}",
        f"Gateway sockets: {sockets}",
        f"Gateway ingress: {listeners}",
        f"Gateway counters: {counters}",
        f"Gateway last failure: {failure_detail}",
    ]
