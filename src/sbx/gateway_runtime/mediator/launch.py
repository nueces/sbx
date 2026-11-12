#!/usr/bin/env python3
"""Exec the installed per-VM mitmdump mediator."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

from hardening import Status, close_unexpected_fds, sanitized_environment


def _fd(value: str) -> int:
    fd = int(value)
    if fd < 3:
        raise argparse.ArgumentTypeError("descriptor must not be stdio")
    os.fstat(fd)
    return fd


def command(confdir: Path, upstream_ca: Path | None = None, *, mitmweb: bool = False) -> list[str]:
    mitmdump = shutil.which("mitmdump")
    if mitmdump is None:
        raise RuntimeError("mitmdump is missing from PATH")
    mitmdump = str(Path(mitmdump).resolve())
    settings = [
        "mode=socks5@127.0.0.1:0",
        "proxyauth=any",
        "connection_strategy=lazy",
        "stream_large_bodies=1",
        "store_streamed_bodies=false",
        "flow_detail=0",
        "termlog_verbosity=error",
        "http2=true",
        "http3=false",
        "websocket=true",
        "rawtcp=false",
        "tcp_timeout=10",
        "onboarding=false",
        "validate_inbound_headers=true",
        f"confdir={confdir}",
    ]
    if upstream_ca is not None:
        settings.append(f"ssl_verify_upstream_trusted_ca={upstream_ca}")
    executable = (
        [sys.executable, str(Path(__file__).with_name("sbx_gateway_web.py"))]
        if mitmweb
        else [mitmdump]
    )
    argv = [*executable, "-s", str(Path(__file__).with_name("sbx_gateway_mediator.py"))]
    if mitmweb:
        settings.extend(("web_host=127.0.0.1", "web_port=0", "web_open_browser=false"))
    for setting in settings:
        argv.extend(("--set", setting))
    return argv


def _private_directory(path: Path) -> Path:
    path = path.absolute()
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("mediator state directory must be owner-only")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--incarnation", required=True)
    parser.add_argument("--key-fd", required=True, type=_fd)
    parser.add_argument("--ready-fd", required=True, type=_fd)
    parser.add_argument("--policy-fd", type=_fd)
    parser.add_argument("--broker-fd", required=True, type=_fd)
    parser.add_argument("--lifetime-fd", required=True, type=_fd)
    parser.add_argument("--confdir", required=True, type=Path)
    parser.add_argument("--upstream-ca", type=Path)
    parser.add_argument("--open-files", type=int, default=4096)
    parser.add_argument("--mitmweb", action="store_true")
    parser.add_argument("--audit-fd", required=True, type=_fd)
    parser.add_argument("--audit-ack-fd", required=True, type=_fd)
    parser.add_argument("--generation", default="unknown")
    args = parser.parse_args()
    confdir = _private_directory(args.confdir)
    upstream_ca = args.upstream_ca.resolve(strict=True) if args.upstream_ca else None
    if not 32 <= args.open_files <= 4096:
        raise RuntimeError("mediator open-file limit must be between 32 and 4096")
    os.umask(0o077)
    os.chdir(confdir)
    argv = command(confdir, upstream_ca, mitmweb=args.mitmweb)
    values = {
        "SBX_GATEWAY_CONFDIR": str(confdir),
        "SBX_GATEWAY_GENERATION": args.generation,
        "SBX_GATEWAY_OPEN_FILES": str(args.open_files),
        "SBX_GATEWAY_KEY_FD": str(args.key_fd),
        "SBX_GATEWAY_READY_FD": str(args.ready_fd),
        "SBX_GATEWAY_VM_ID": args.vm_id,
        "SBX_GATEWAY_INCARNATION": args.incarnation,
    }
    preserve = {args.key_fd, args.ready_fd}
    if args.audit_fd is not None:
        values["SBX_GATEWAY_AUDIT_FD"] = str(args.audit_fd)
        preserve.add(args.audit_fd)
    values["SBX_GATEWAY_AUDIT_ACK_FD"] = str(args.audit_ack_fd)
    preserve.add(args.audit_ack_fd)
    if args.policy_fd is not None:
        values["SBX_GATEWAY_POLICY_FD"] = str(args.policy_fd)
        preserve.add(args.policy_fd)
    values["SBX_GATEWAY_BROKER_FD"] = str(args.broker_fd)
    preserve.add(args.broker_fd)
    values["SBX_GATEWAY_LIFETIME_FD"] = str(args.lifetime_fd)
    preserve.add(args.lifetime_fd)
    environment = sanitized_environment(confdir, Status(False, False, False, False), values)
    close_unexpected_fds(preserve)
    os.execve(argv[0], argv, environment)


if __name__ == "__main__":
    main()
