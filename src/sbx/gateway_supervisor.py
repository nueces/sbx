"""Persistent, unprivileged per-VM gateway supervisor."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sbx import gateway
from sbx.gateway_credentials import communicate_bounded

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
PROVIDER_TIMEOUT_SECONDS = 10
PROVIDER_OUTPUT_BYTES = 64 * 1024
PROVIDER_CONCURRENCY = 8
BROKER_CONCURRENCY = 128
BROKER_RECENT_REQUESTS = 4096
EARLY_REFRESH_SECONDS = 60


def send_frame(sock: socket.socket, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME_BYTES:
        raise gateway.GatewayLifecycleError("gateway control frame is too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_frame(sock: socket.socket, timeout: float | None) -> dict[str, Any]:
    sock.settimeout(timeout)
    header = _recv_exact(sock, 4)
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise gateway.GatewayLifecycleError("invalid gateway control frame length")
    try:
        value = json.loads(_recv_exact(sock, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise gateway.GatewayLifecycleError("invalid gateway control frame") from exc
    if not isinstance(value, dict):
        raise gateway.GatewayLifecycleError("invalid gateway control frame")
    return value


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise gateway.GatewayLifecycleError("gateway control channel closed")
        data.extend(chunk)
    return bytes(data)


def config_from_payload(value: dict[str, Any]) -> gateway.GatewayConfig:
    credentials = []
    for item in value["credentials"]:
        item = dict(item)
        item["bindings"] = tuple(
            gateway.CredentialBinding(**binding) for binding in item["bindings"]
        )
        item["options"] = dict(item["options"])
        item["inherit_environment"] = tuple(item["inherit_environment"])
        credentials.append(gateway.Credential(**item))
    return gateway.GatewayConfig(
        tuple(gateway.PolicyRule(**item) for item in value["rules"]),
        tuple(credentials),
        tuple(gateway.IngressRule(**item) for item in value["ingress"]),
        gateway.Quotas(**value["quotas"]),
        gateway.Reconnection(**value["reconnection"]),
        value["platform"],
        value.get("mitmweb", False),
    )


def config_payload(config: gateway.GatewayConfig) -> dict[str, Any]:
    return asdict(config)


class CredentialBroker:
    """Supervisor-owned provider execution, cache, and mediator broker."""

    def __init__(
        self,
        credentials: tuple[gateway.Credential, ...],
        source_environment: dict[str, Any],
        directory: Path,
        *,
        clock: Any = time.time,
    ) -> None:
        self.credentials = {credential.id: credential for credential in credentials}
        self.source_environment = source_environment
        self.directory = directory
        self.clock = clock
        self.cache: dict[str, tuple[str, float | None]] = {}
        self.cache_lock = threading.Lock()
        self.locks = {name: threading.Lock() for name in self.credentials}
        self.slots = threading.BoundedSemaphore(PROVIDER_CONCURRENCY)
        self.pending = threading.BoundedSemaphore(BROKER_CONCURRENCY)

    @property
    def values(self) -> set[str]:
        with self.cache_lock:
            return {value for value, _expires in self.cache.values()}

    def describe(self) -> tuple[gateway.Credential, ...]:
        described = []
        for credential in self.credentials.values():
            if not credential.bindings:
                described.append(credential)
                continue
            result = self._invoke(credential, "describe")
            if set(result) != {"header"}:
                raise gateway.GatewayLifecycleError("credential description is invalid")
            header = gateway.validate_header_descriptor(result["header"], allow_none=True)
            described.append(replace(credential, header=header))
        self.credentials = {credential.id: credential for credential in described}
        return tuple(described)

    def _environment(self, credential: gateway.Credential, operation: str) -> dict[str, str]:
        baseline = self.source_environment.get("baseline")
        inherited = self.source_environment.get("inherited")
        if not isinstance(baseline, dict) or not isinstance(inherited, dict):
            raise gateway.GatewayLifecycleError("invalid inherited credential environment")
        environment = {name: str(baseline[name]) for name in ("HOME", "PATH", "TZ")}
        if operation == "fetch":
            for name in credential.inherit_environment:
                value = inherited.get(name)
                if not isinstance(value, str):
                    raise gateway.GatewayLifecycleError(
                        f"credential {credential.id!r} requires missing host environment {name!r}"
                    )
                environment[name] = value
        return environment

    @staticmethod
    def _acquire(resource: Any, cancelled: threading.Event | None) -> None:
        deadline = time.monotonic() + PROVIDER_TIMEOUT_SECONDS
        while True:
            if cancelled is not None and cancelled.is_set():
                raise gateway.GatewayLifecycleError("credential lookup cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise gateway.GatewayLifecycleError("credential provider capacity exhausted")
            if resource.acquire(timeout=min(0.1, remaining)):
                if cancelled is not None and cancelled.is_set():
                    resource.release()
                    raise gateway.GatewayLifecycleError("credential lookup cancelled")
                return

    def _invoke(
        self,
        credential: gateway.Credential,
        operation: str,
        *,
        cancelled: threading.Event | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "credential_id": credential.id,
            "options": dict(credential.options),
        }
        if credential.method == "command":
            payload.update(credential.options)
        lifetime_read, lifetime_write = os.pipe()
        command = [
            sys.executable,
            "-m",
            "sbx.gateway_provider_helper",
            "--method",
            credential.method,
            "--operation",
            operation,
            "--lifetime-fd",
            str(lifetime_read),
        ]
        acquired = False
        process: subprocess.Popen[bytes] | None = None
        try:
            self._acquire(self.slots, cancelled)
            acquired = True
            environment = self._environment(credential, operation)
            if cancelled is not None and cancelled.is_set():
                raise gateway.GatewayLifecycleError("credential lookup cancelled")
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.directory,
                env=environment,
                pass_fds=(lifetime_read,),
                close_fds=True,
                start_new_session=True,
            )
            os.close(lifetime_read)
            lifetime_read = -1
            try:
                completed = communicate_bounded(
                    process,
                    input=json.dumps(payload, separators=(",", ":")).encode(),
                    timeout=PROVIDER_TIMEOUT_SECONDS,
                    maximum=PROVIDER_OUTPUT_BYTES,
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                # Helper I/O/limits are a boundary; never expose provider output in errors.
                raise gateway.GatewayLifecycleError("credential provider execution failed") from exc
            if completed.returncode:
                raise gateway.GatewayLifecycleError("credential provider failed")
            try:
                result = json.loads(completed.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise gateway.GatewayLifecycleError(
                    "credential provider returned invalid output"
                ) from exc
            if not isinstance(result, dict):
                raise gateway.GatewayLifecycleError("credential provider returned invalid output")
            return result
        finally:
            if process is not None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            if lifetime_read >= 0:
                os.close(lifetime_read)
            os.close(lifetime_write)
            if acquired:
                self.slots.release()

    def fetch(self, credential_id: str, *, cancelled: threading.Event | None = None) -> str:
        credential = self.credentials.get(credential_id)
        if credential is None or not credential.bindings:
            raise gateway.GatewayAuthenticationError("unknown broker credential")
        self._acquire(self.locks[credential_id], cancelled)
        try:
            now = float(self.clock())
            with self.cache_lock:
                cached = self.cache.get(credential_id)
            if cached is not None and (
                cached[1] is None or cached[1] - now > EARLY_REFRESH_SECONDS
            ):
                return cached[0]
            try:
                result = self._invoke(credential, "fetch", cancelled=cancelled)
                if set(result) != {"value", "expires_at"}:
                    raise gateway.GatewayLifecycleError("credential result is invalid")
                value, expires = result["value"], result["expires_at"]
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value.encode()) > PROVIDER_OUTPUT_BYTES
                    or any(character in value for character in "\r\n\0")
                    or (
                        expires is not None
                        and (type(expires) not in {int, float} or not math.isfinite(float(expires)))
                    )
                ):
                    raise gateway.GatewayLifecycleError("credential result is invalid")
                with self.cache_lock:
                    if expires is not None and float(expires) <= float(self.clock()):
                        raise gateway.GatewayLifecycleError("credential result has expired")
                    self.cache[credential_id] = (
                        value,
                        None if expires is None else float(expires),
                    )
                return value
            except Exception as exc:
                # Provider refresh is a boundary: only an actually unexpired cache is safe.
                if cached is not None and (cached[1] is None or cached[1] > float(self.clock())):
                    return cached[0]
                raise gateway.GatewayLifecycleError("credential provider failed") from exc
        finally:
            self.locks[credential_id].release()

    def serve(self, sock: socket.socket, incarnation: str) -> None:
        send_lock = threading.Lock()
        cancelled = threading.Event()
        seen: set[str] = set()
        seen_order: deque[str] = deque()

        def close_channel() -> None:
            cancelled.set()
            # Teardown may race with the peer or another failed writer.
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

        def deliver(response: dict[str, Any]) -> None:
            with send_lock:
                if cancelled.is_set():
                    return
                try:
                    send_frame(sock, response)
                except (OSError, gateway.GatewayLifecycleError):
                    # A broken response channel cannot be reused safely.
                    close_channel()

        def answer(request: dict[str, Any]) -> None:
            response: dict[str, Any] = {
                "version": 1,
                "request_id": request["request_id"],
                "incarnation": incarnation,
            }
            try:
                try:
                    response["value"] = self.fetch(request["credential"], cancelled=cancelled)
                except Exception:
                    # Provider/cancellation failures become a secret-free RPC error only here.
                    response["error"] = "credential unavailable"
                deliver(response)
            finally:
                self.pending.release()

        try:
            while True:
                request = recv_frame(sock, None)
                request_id = request.get("request_id")
                if (
                    set(request) != {"version", "request_id", "incarnation", "credential"}
                    or request.get("version") != 1
                    or request.get("incarnation") != incarnation
                    or not isinstance(request_id, str)
                    or not request_id
                    or request_id in seen
                    or request.get("credential") not in self.credentials
                ):
                    raise gateway.GatewayAuthenticationError("invalid broker request")
                seen.add(request_id)
                seen_order.append(request_id)
                if len(seen_order) > BROKER_RECENT_REQUESTS:
                    seen.discard(seen_order.popleft())
                if not self.pending.acquire(blocking=False):
                    deliver(
                        {
                            "version": 1,
                            "request_id": request_id,
                            "incarnation": incarnation,
                            "error": "credential unavailable",
                        }
                    )
                    continue
                try:
                    threading.Thread(target=answer, args=(request,), daemon=True).start()
                except Exception:
                    self.pending.release()
                    raise
        except Exception:
            # Invalid framing/JSON, peer closure or dispatch failure is fatal to this channel.
            return
        finally:
            close_channel()


def _read_fd(fd: int, maximum: int) -> bytes:
    data = bytearray()
    while chunk := os.read(fd, min(65536, maximum + 1 - len(data))):
        data.extend(chunk)
        if len(data) > maximum:
            raise gateway.GatewayLifecycleError("inherited gateway payload is too large")
    return bytes(data)


def _peer_uid(client: socket.socket) -> int:
    if sys.platform == "linux":
        return struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    getter = getattr(client, "getpeereid", None)
    if getter is None:
        raise gateway.GatewayAuthenticationError("peer credentials are unavailable")
    return int(getter()[0])


def _serve(controller: gateway.GatewayController, server: socket.socket) -> None:
    while True:
        client, _ = server.accept()
        with client:
            shutdown_complete = False
            try:
                if _peer_uid(client) != os.getuid():
                    raise gateway.GatewayAuthenticationError("gateway control peer UID mismatch")
                request = recv_frame(client, controller.timeout)
                if (
                    request.get("version") != PROTOCOL_VERSION
                    or request.get("generation") != controller.generation
                ):
                    raise gateway.GatewayAuthenticationError("gateway control identity mismatch")
                operation = request.get("operation")
                if operation in {"status", "diagnostics", "check"}:
                    pass
                elif operation == "drain":
                    controller.drain()
                elif operation == "shutdown":
                    controller.close()
                    shutdown_complete = True
                    send_frame(
                        client,
                        {"version": 1, "generation": controller.generation, "status": "stopped"},
                    )
                    return
                else:
                    raise gateway.GatewayLifecycleError("unknown gateway control operation")
                state = gateway.read_state(controller.directory)  # type: ignore[arg-type]
                send_frame(
                    client,
                    {
                        "version": 1,
                        "generation": controller.generation,
                        "status": state["status"],
                        "config_digest": getattr(
                            controller, "_requested_config_digest", state["config_digest"]
                        ),
                        "counters": state["counters"],
                        "last_failure": state["last_failure"],
                    },
                )
            except Exception as exc:  # control errors are bounded and secret-free
                if shutdown_complete:
                    # A failed final reply must not leave a stopped supervisor owning the lock.
                    raise
                with suppress(Exception):
                    send_frame(
                        client,
                        {
                            "version": 1,
                            "generation": controller.generation,
                            "error": str(exc)[:300],
                        },
                    )


def run(bootstrap_fd: int, config_fd: int, provider_fd: int, lock_fd: int) -> int:
    bootstrap = socket.socket(fileno=bootstrap_fd)
    controller: gateway.GatewayController | None = None
    committed = False
    try:
        metadata = json.loads(_read_fd(config_fd, gateway.MAX_CONFIG_BYTES))
        os.close(config_fd)
        if not isinstance(metadata, dict):
            raise gateway.GatewayLifecycleError("invalid inherited gateway configuration")
        config = config_from_payload(metadata["config"])
        directory = Path(metadata["directory"])
        os.chdir(directory)
        source_environment = json.loads(_read_fd(provider_fd, gateway.MAX_CONFIG_BYTES))
        os.close(provider_fd)
        if not isinstance(source_environment, dict):
            raise gateway.GatewayLifecycleError("invalid inherited credential environment")
        broker = CredentialBroker(config.credentials, source_environment, directory)
        config = gateway.finalize_credentials(config, broker.describe())
        controller = gateway.GatewayController(
            config,
            metadata["vm_id"],
            root=Path(metadata["root"]),
            generation=metadata["generation"],
            timeout=float(metadata["timeout"]),
        )
        controller.directory = directory
        controller._ownership_fd = lock_fd
        controller.runtime_directory = Path(metadata["runtime_directory"])
        state = gateway.read_state(directory, vm_id=controller.vm_id)
        if state["runtime_directory"] != str(controller.runtime_directory):
            raise gateway.GatewayAuthenticationError("gateway runtime identity mismatch")
        gateway._reject_symlink_ancestors(controller.runtime_directory)
        gateway._private_directory(controller.runtime_directory)
        controller.mac = metadata["mac"]
        controller._placeholders = dict(metadata["placeholders"])
        controller._credential_broker = broker
        controller._owner_pid = os.getpid()
        controller._requested_config_digest = metadata["requested_config_digest"]
        controller.listener_rules = tuple(
            gateway.IngressRule(**item) for item in metadata["listener_rules"]
        )
        controller.qemu_listener = socket.socket(fileno=int(metadata["qemu_fd"]))
        controller.listeners = [socket.socket(fileno=int(fd)) for fd in metadata["listener_fds"]]
        gateway.write_effective_policy(directory, config, controller.vm_id, controller.generation)
        controller._immutable_policy = (directory / "policy.json").read_bytes()
        controller._mediator_policy_payload = controller._mediator_policy()
        gateway.update_state(directory, status="starting")
        controller._start_data_plane(time.monotonic() + controller.timeout)
        gateway.update_state(
            directory,
            pids=controller._process_ids(),
            listeners=controller.listener_rules,
        )
        send_frame(
            bootstrap,
            {"version": 1, "generation": controller.generation, "status": "prepared"},
        )
        request = recv_frame(bootstrap, controller.timeout)
        if (
            request.get("operation") != "commit"
            or request.get("generation") != controller.generation
        ):
            raise gateway.GatewayAuthenticationError("invalid gateway bootstrap commit")
        controller.vm_network_ready(int(request["qemu_pid"]), float(request["timeout"]))
        control_path = controller.runtime_directory / "control.sock"
        with suppress(FileNotFoundError):
            control_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(control_path))
        os.chmod(control_path, 0o600)
        server.listen(16)
        gateway.update_state(
            directory,
            pids=controller._process_ids() | {"supervisor": os.getpid()},
            sockets={"qemu": controller.qemu_socket, "control": control_path},
        )
        committed = True
        with suppress(OSError, gateway.GatewayLifecycleError):
            send_frame(
                bootstrap,
                {"version": 1, "generation": controller.generation, "status": "ready"},
            )
        bootstrap.close()
        _serve(controller, server)
        server.close()
        return 0
    except BaseException as exc:
        with suppress(Exception):
            send_frame(
                bootstrap,
                {
                    "version": 1,
                    "generation": controller.generation if controller else "unknown",
                    "error": str(exc)[:300],
                },
            )
        if controller is not None:
            if committed:
                controller.close()
            else:
                controller.rollback("startup_failed")
        return 1
    finally:
        with suppress(Exception):
            bootstrap.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-fd", type=int, required=True)
    parser.add_argument("--config-fd", type=int, required=True)
    parser.add_argument("--provider-fd", type=int, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.bootstrap_fd, args.config_fd, args.provider_fd, args.lock_fd))


if __name__ == "__main__":
    main()
