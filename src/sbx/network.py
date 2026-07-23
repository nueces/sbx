import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from sbx import auth_ports, gateway
from sbx.constants import SBX_STATE_DIR, TUNNELS_FILE
from sbx.gateway import parse_port_forward
from sbx.runtime import (
    ConfigError,
    debug,
    debug_command,
    pid_is_alive,
    read_json_object,
    run,
    run_smolvm_capture,
    ssh_command,
    vm_name_from_arg_or_config,
    write_json_object,
)

port_forwards_from_specs = gateway.port_forwards_from_specs


@contextmanager
def tunnel_lock():
    SBX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with TUNNELS_FILE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load_tunnels() -> dict[str, Any]:
    data = read_json_object(TUNNELS_FILE)
    for vm_id, vm in list(data.items()):
        if not isinstance(vm, dict):
            data.pop(vm_id)
            continue
        ports = vm.get("auth_ports")
        if not isinstance(ports, dict):
            ports = {}
        legacy = vm.pop("auth_port", None)
        if isinstance(legacy, dict) and type(legacy.get("host_port")) is int:
            ports.setdefault(str(legacy["host_port"]), legacy)
        vm["auth_ports"] = ports
    return data


def _save_tunnels(data: Mapping[str, Any]) -> None:
    write_json_object(TUNNELS_FILE, data, state_dir=SBX_STATE_DIR)


def _live_auth_tunnel(tunnel: object) -> bool:
    return (
        isinstance(tunnel, dict)
        and type(tunnel.get("pid")) is int
        and tunnel["pid"] > 0
        and type(tunnel.get("host_port")) is int
        and 1 <= tunnel["host_port"] <= 65535
        and type(tunnel.get("guest_port")) is int
        and 1 <= tunnel["guest_port"] <= 65535
        and pid_is_alive(tunnel["pid"])
    )


def _tracked_auth_tunnels(vm_id: str) -> list[dict[str, Any]]:
    return [
        tunnel
        for tunnel in _load_tunnels().get(vm_id, {}).get("auth_ports", {}).values()
        if _live_auth_tunnel(tunnel)
    ]


def _tracked_auth_tunnel(vm_id: str, host_port: int | None = None) -> dict[str, Any] | None:
    return next(
        (
            t
            for t in _tracked_auth_tunnels(vm_id)
            if host_port is None or t["host_port"] == host_port
        ),
        None,
    )


def _tracked_auth_tunnel_for_host_port(host_port: int) -> tuple[str, dict[str, Any]] | None:
    for vm_id in _load_tunnels():
        tunnel = _tracked_auth_tunnel(vm_id, host_port)
        if tunnel is not None:
            return vm_id, tunnel
    return None


def _record_auth_tunnel(vm_id: str, *, pid: int, host_port: int, guest_port: int) -> None:
    data = _load_tunnels()
    data.setdefault(vm_id, {}).setdefault("auth_ports", {})[str(host_port)] = {
        "pid": pid,
        "host_port": host_port,
        "guest_port": guest_port,
    }
    _save_tunnels(data)


def _remove_auth_tunnel_record(vm_id: str, host_port: int | None = None) -> None:
    data = _load_tunnels()
    vm_data = data.get(vm_id)
    if isinstance(vm_data, dict):
        ports = vm_data.get("auth_ports", {})
        if host_port is None:
            ports.clear()
        else:
            ports.pop(str(host_port), None)
        if not ports:
            vm_data.pop("auth_ports", None)
        if not vm_data:
            data.pop(vm_id, None)
    _save_tunnels(data)


def _localhost_port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _close_tracked_auth_tunnel(vm_id: str, host_port: int | None = None) -> bool:
    tracked = _tracked_auth_tunnel(vm_id, host_port)
    if tracked is None:
        _remove_auth_tunnel_record(vm_id, host_port)
        return False

    pid = int(tracked["pid"])
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and pid_is_alive(pid):
        time.sleep(0.1)
    if pid_is_alive(pid):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    _remove_auth_tunnel_record(vm_id, tracked["host_port"])
    return True


def close_session_auth_ports(vm_id: str, created: Sequence[Mapping[str, Any]]) -> None:
    with tunnel_lock():
        _close_session_auth_ports(vm_id, created)


def _close_session_auth_ports(vm_id: str, created: Sequence[Mapping[str, Any]]) -> None:
    for tunnel in created:
        current = _load_tunnels().get(vm_id, {}).get("auth_ports", {}).get(str(tunnel["host_port"]))
        if current == tunnel:
            _close_tracked_auth_tunnel(vm_id, tunnel["host_port"])


def close_auth_ports(vm_id: str) -> bool:
    with tunnel_lock():
        tunnels = _tracked_auth_tunnels(vm_id)
        for tunnel in tunnels:
            _close_tracked_auth_tunnel(vm_id, tunnel["host_port"])
        _remove_auth_tunnel_record(vm_id)
        return bool(tunnels)


def expose_auth_ports(
    vm_id: str,
    ports: Sequence[tuple[int, int]],
    *,
    replace: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    with tunnel_lock():
        try:
            for host, guest in ports:
                before = _tracked_auth_tunnel(vm_id, host)
                rc = _expose_auth_port(vm_id, host, guest, replace=replace)
                after = _tracked_auth_tunnel(vm_id, host)
                if after is not None and after != before:
                    created.append(after)
                if rc != 0:
                    _close_session_auth_ports(vm_id, created)
                    return rc, []
        except BaseException:
            _close_session_auth_ports(vm_id, created)
            raise
    return 0, created


def expose_auth_port(vm_id: str, host_port: int, guest_port: int, *, replace: bool = False) -> int:
    return expose_auth_ports(vm_id, [(host_port, guest_port)], replace=replace)[0]


def _expose_auth_port(vm_id: str, host_port: int, guest_port: int, *, replace: bool = False) -> int:
    debug(f"expose auth port: vm={vm_id}, host_port={host_port}, guest_port={guest_port}")
    tracked = _tracked_auth_tunnel(vm_id, host_port)
    if (
        tracked
        and tracked.get("host_port") == host_port
        and tracked.get("guest_port") == guest_port
    ):
        debug(f"auth host port {host_port} already tracked with pid {tracked['pid']}")
        return 0
    owner = _tracked_auth_tunnel_for_host_port(host_port)
    if owner is not None or _localhost_port_is_listening(host_port):
        if owner is not None:
            owner_vm, _owner_tunnel = owner
            if replace:
                print(
                    f"sbx: replacing auth tunnel on localhost:{host_port}: "
                    f"VM '{owner_vm}' -> VM '{vm_id}'.",
                    file=sys.stderr,
                )
                _close_tracked_auth_tunnel(owner_vm, host_port)
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

    ready = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
                print(
                    f"sbx: auth port tunnel exited before becoming ready: {stderr}", file=sys.stderr
                )
                return proc.returncode or 1
            if _localhost_port_is_listening(host_port):
                _record_auth_tunnel(vm_id, pid=proc.pid, host_port=host_port, guest_port=guest_port)
                ready = True
                debug(f"auth port tunnel ready with pid {proc.pid}")
                return 0
            time.sleep(0.1)
        print(
            f"sbx: auth port tunnel did not become ready on localhost:{host_port}", file=sys.stderr
        )
        return 1
    finally:
        if not ready:
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGTERM)
        if proc.stderr is not None:
            proc.stderr.close()


def _foreground_port_forward(vm_id: str, forwards: Sequence[tuple[str, int, int]]) -> int:
    cmd = ssh_command(vm_id)
    forward_args = ["-N"]
    for host, host_port, guest_port in forwards:
        forward_args.extend(["-L", f"{host}:{host_port}:127.0.0.1:{guest_port}"])
    forward_args.extend(["-o", "ExitOnForwardFailure=yes"])
    cmd[-1:-1] = forward_args
    for host, host_port, guest_port in forwards:
        print(f"Forwarding {host}:{host_port} -> guest 127.0.0.1:{guest_port}")
    print("Press Ctrl-C to stop.")
    return run(cmd)


def cmd_forward(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(args, getattr(args, "config_data", None), "network forward")
    if name is None:
        return 2
    try:
        return _foreground_port_forward(
            str(name), [parse_port_forward(spec) for spec in args.specs]
        )
    except ConfigError as exc:
        print(f"sbx: {exc}", file=sys.stderr)
        return 2


def cmd_auth_port(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(args, getattr(args, "config_data", None), "network auth-port")
    if name is None:
        return 2
    config = getattr(args, "config_data", {})
    agent = config.get("sbx", {}).get("agent", "pi")
    _enabled, ports = auth_ports.resolve(args, config, agent)
    return expose_auth_ports(name, ports, replace=bool(getattr(args, "replace", False)))[0]


def cmd_close_auth_port(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network close-auth-port"
    )
    if name is None:
        return 2
    if not close_auth_ports(name):
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
    tunnels = _tracked_auth_tunnels(name)
    auth_status = "inactive"
    auth_detail = "-"
    if tunnels:
        auth_status = "active"
        auth_detail = "; ".join(
            f"pid {tracked['pid']}, localhost:{tracked['host_port']} -> "
            f"guest:{tracked['guest_port']}"
            for tracked in tunnels
        )
    elif _localhost_port_is_listening(args.host_port):
        auth_status = "busy/untracked"
        auth_detail = f"localhost:{args.host_port} is listening but is not tracked by sbx"

    port_forwards = _vm_port_forward_details(vm)
    result = {
        "name": vm["name"],
        "status": vm["status"],
        "backend": vm["backend"],
        "guest_ip": vm["ip_address"],
        "ssh_port": vm["ssh_port"],
        "port_forwards": port_forwards,
        "auth_callback": {
            "status": auth_status,
            "tunnels": tunnels,
            "detail": None if auth_detail == "-" else auth_detail,
        },
    }
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Sandbox: {result['name']}")
        print(f"Status: {result['status']}")
        print(f"Backend: {result['backend']}")
        print(f"Guest IP: {result['guest_ip']}")
        print(f"SSH Port: {result['ssh_port']}")
        print("Port forwards: " + (", ".join(port_forwards) if port_forwards else "-"))
        print(f"Auth callback: {auth_status}")
        print(f"Auth detail: {auth_detail}")
        for line in gateway.diagnostic_lines(
            name, configured=getattr(args, "gateway_config", None)
        ):
            print(line)
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    name = vm_name_from_arg_or_config(
        args, getattr(args, "config_data", None), "network diagnostics"
    )
    if name is None:
        return 2
    for line in gateway.diagnostic_lines(
        name,
        configured=getattr(args, "gateway_config", None),
        include_disabled=True,
    ):
        print(line)
    return 0
