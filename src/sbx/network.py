import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sbx.constants import SBX_STATE_DIR, TUNNELS_FILE
from sbx.runtime import (
    ConfigError,
    debug,
    debug_command,
    pid_is_alive,
    read_json_object,
    run,
    run_smolvm_capture,
    ssh_command,
    suppress_process_errors,
    vm_name_from_arg_or_config,
    write_json_object,
)


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


def _load_tunnels() -> dict[str, Any]:
    return read_json_object(TUNNELS_FILE)


def _save_tunnels(data: Mapping[str, Any]) -> None:
    write_json_object(TUNNELS_FILE, data, state_dir=SBX_STATE_DIR)


def _tracked_auth_tunnel(vm_id: str) -> dict[str, Any] | None:
    tunnel = _load_tunnels().get(vm_id, {}).get("auth_port")
    if not isinstance(tunnel, dict):
        return None
    pid = tunnel.get("pid")
    if not isinstance(pid, int) or not pid_is_alive(pid):
        return None
    return tunnel


def _tracked_auth_tunnel_for_host_port(host_port: int) -> tuple[str, dict[str, Any]] | None:
    for vm_id, vm_data in _load_tunnels().items():
        if not isinstance(vm_data, dict):
            continue
        tunnel = vm_data.get("auth_port")
        if not isinstance(tunnel, dict) or tunnel.get("host_port") != host_port:
            continue
        pid = tunnel.get("pid")
        if isinstance(pid, int) and pid_is_alive(pid):
            return str(vm_id), tunnel
    return None


def _record_auth_tunnel(vm_id: str, *, pid: int, host_port: int, guest_port: int) -> None:
    data = _load_tunnels()
    data.setdefault(vm_id, {})["auth_port"] = {
        "pid": pid,
        "host_port": host_port,
        "guest_port": guest_port,
    }
    _save_tunnels(data)


def _remove_auth_tunnel_record(vm_id: str) -> None:
    data = _load_tunnels()
    vm_data = data.get(vm_id)
    if isinstance(vm_data, dict):
        vm_data.pop("auth_port", None)
        if not vm_data:
            data.pop(vm_id, None)
    _save_tunnels(data)


def _localhost_port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _close_tracked_auth_tunnel(vm_id: str) -> bool:
    tracked = _tracked_auth_tunnel(vm_id)
    if tracked is None:
        _remove_auth_tunnel_record(vm_id)
        return False

    pid = int(tracked["pid"])
    with suppress_process_errors():
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and pid_is_alive(pid):
        time.sleep(0.1)
    if pid_is_alive(pid):
        with suppress_process_errors():
            os.killpg(pid, signal.SIGKILL)
    _remove_auth_tunnel_record(vm_id)
    return True


def expose_auth_port(vm_id: str, host_port: int, guest_port: int, *, replace: bool = False) -> int:
    debug(f"expose auth port: vm={vm_id}, host_port={host_port}, guest_port={guest_port}")
    tracked = _tracked_auth_tunnel(vm_id)
    if (
        tracked
        and tracked.get("host_port") == host_port
        and tracked.get("guest_port") == guest_port
    ):
        debug(f"auth host port {host_port} already tracked with pid {tracked['pid']}")
        return 0
    if _localhost_port_is_listening(host_port):
        owner = _tracked_auth_tunnel_for_host_port(host_port)
        if owner is not None:
            owner_vm, _owner_tunnel = owner
            if replace:
                print(
                    f"sbx: replacing auth tunnel on localhost:{host_port}: "
                    f"VM '{owner_vm}' -> VM '{vm_id}'.",
                    file=sys.stderr,
                )
                _close_tracked_auth_tunnel(owner_vm)
            else:
                print(
                    f"sbx: warning: localhost:{host_port} is already used by the auth "
                    f"tunnel for VM '{owner_vm}'. `/login` in VM '{vm_id}' may not work; "
                    f"run `sbx network auth-port {vm_id} --replace` to switch it.",
                    file=sys.stderr,
                )
                return 0
        else:
            print(
                f"sbx: warning: localhost:{host_port} is already in use and is not tracked "
                "by sbx. `/login` may not work until that port is free.",
                file=sys.stderr,
            )
            return 0

    cmd = ssh_command(vm_id)
    cmd[-1:-1] = [
        "-N",
        "-L",
        f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
    ]
    debug_command(cmd, None)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("sbx: command not found: ssh", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"sbx: failed to start auth port tunnel: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
            print(f"sbx: auth port tunnel exited before becoming ready: {stderr}", file=sys.stderr)
            return proc.returncode or 1
        if _localhost_port_is_listening(host_port):
            _record_auth_tunnel(vm_id, pid=proc.pid, host_port=host_port, guest_port=guest_port)
            debug(f"auth port tunnel ready with pid {proc.pid}")
            return 0
        time.sleep(0.1)

    with suppress_process_errors():
        os.killpg(proc.pid, signal.SIGTERM)
    print(f"sbx: auth port tunnel did not become ready on localhost:{host_port}", file=sys.stderr)
    return 1


def _foreground_port_forward(vm_id: str, forward: tuple[str, int, int]) -> int:
    host, host_port, guest_port = forward
    cmd = ssh_command(vm_id)
    cmd[-1:-1] = [
        "-N",
        "-L",
        f"{host}:{host_port}:127.0.0.1:{guest_port}",
        "-o",
        "ExitOnForwardFailure=yes",
    ]
    print(f"Forwarding {host}:{host_port} -> guest 127.0.0.1:{guest_port}")
    print("Press Ctrl-C to stop.")
    return run(cmd)


def cmd_forward(args: argparse.Namespace) -> int:
    if len(args.forward_args) == 1:
        spec = args.forward_args[0]
        name = vm_name_from_arg_or_config(
            args, getattr(args, "config_data", None), "network forward"
        )
    elif len(args.forward_args) == 2:
        name, spec = args.forward_args
    else:
        print("sbx: network forward expects [NAME] SPEC", file=sys.stderr)
        return 2
    if name is None:
        return 2
    try:
        return _foreground_port_forward(str(name), parse_port_forward(spec))
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2


def cmd_auth_port(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(args, getattr(args, "config_data", None), "network auth-port")
    if name is None:
        return 2
    return expose_auth_port(
        name, args.host_port, args.guest_port, replace=bool(getattr(args, "replace", False))
    )


def cmd_close_auth_port(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network close-auth-port"
    )
    if name is None:
        return 2
    if not _close_tracked_auth_tunnel(name):
        print(f"No tracked auth port tunnel for '{name}'.")
        return 0
    print(f"Closed auth port tunnel for '{name}'.")
    return 0


def _port_forward_detail(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    host_address = value.get("host_address", "127.0.0.1")
    host_port = value.get("host_port")
    guest_port = value.get("guest_port")
    if (
        not isinstance(host_address, str)
        or not isinstance(host_port, int)
        or not isinstance(guest_port, int)
    ):
        return None
    return f"{host_address}:{host_port} -> guest 127.0.0.1:{guest_port}"


def _vm_port_forward_details(vm: Mapping[str, Any]) -> list[str]:
    raw = vm.get("port_forwards")
    if raw is None and isinstance(vm.get("config"), Mapping):
        raw = vm["config"].get("port_forwards")
    if not isinstance(raw, list):
        return []
    return [detail for item in raw if (detail := _port_forward_detail(item)) is not None]


def cmd_status(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(args, getattr(args, "config_data", None), "network status")
    if name is None:
        return 2
    completed = run_smolvm_capture(["sandbox", "info", name, "--json"])
    if completed is None:
        return 127
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        return completed.returncode

    payload = json.loads(completed.stdout)
    vm = payload["data"]["vm"]
    tracked = _tracked_auth_tunnel(name)
    auth_status = "inactive"
    auth_detail = "-"
    if tracked is not None:
        auth_status = "active"
        auth_detail = (
            f"pid {tracked['pid']}, localhost:{tracked['host_port']} -> "
            f"guest:{tracked['guest_port']}"
        )
    elif _localhost_port_is_listening(args.host_port):
        auth_status = "busy/untracked"
        auth_detail = f"localhost:{args.host_port} is listening but is not tracked by sbx"

    print(f"Sandbox: {vm['name']}")
    print(f"Status: {vm['status']}")
    print(f"Backend: {vm['backend']}")
    print(f"Guest IP: {vm['ip_address']}")
    print(f"SSH Port: {vm['ssh_port']}")
    port_forwards = _vm_port_forward_details(vm)
    print("Port forwards: " + (", ".join(port_forwards) if port_forwards else "-"))
    print(f"Auth callback: {auth_status}")
    print(f"Auth detail: {auth_detail}")
    return 0
