#!/usr/bin/env python3
"""Exec the exact pinned per-VM mitmdump mediator."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import stat
import sys
from pathlib import Path

MITMPROXY_VERSION = "12.2.3"
PYTHON_VERSION = (3, 12, 13)


def _fd(value: str) -> int:
    fd = int(value)
    if fd < 3:
        raise argparse.ArgumentTypeError("descriptor must not be stdio")
    os.fstat(fd)
    return fd


def command(confdir: Path, upstream_ca: Path | None = None) -> list[str]:
    mitmdump = Path(sys.executable).with_name("mitmdump")
    if not mitmdump.is_file():
        raise RuntimeError("pinned mitmdump executable is missing")
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
        "onboarding=false",
        "validate_inbound_headers=true",
        f"confdir={confdir}",
    ]
    if upstream_ca is not None:
        settings.append(f"ssl_verify_upstream_trusted_ca={upstream_ca}")
    argv = [str(mitmdump), "-s", str(Path(__file__).with_name("sbx_gateway_mediator.py"))]
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
    parser.add_argument("--package-auth-fd", type=_fd)
    parser.add_argument("--confdir", required=True, type=Path)
    parser.add_argument("--upstream-ca", type=Path)
    args = parser.parse_args()
    if sys.version_info[:3] != PYTHON_VERSION:
        raise RuntimeError("mediator requires CPython 3.12.13")
    if importlib.metadata.version("mitmproxy") != MITMPROXY_VERSION:
        raise RuntimeError("mediator requires mitmproxy 12.2.3")
    confdir = _private_directory(args.confdir)
    upstream_ca = args.upstream_ca.resolve(strict=True) if args.upstream_ca else None
    os.umask(0o077)
    environment = os.environ.copy()
    environment.update(
        SBX_GATEWAY_KEY_FD=str(args.key_fd),
        SBX_GATEWAY_READY_FD=str(args.ready_fd),
        SBX_GATEWAY_VM_ID=args.vm_id,
        SBX_GATEWAY_INCARNATION=args.incarnation,
    )
    if args.package_auth_fd is not None:
        environment["SBX_GATEWAY_PACKAGE_AUTH_FD"] = str(args.package_auth_fd)
    argv = command(confdir, upstream_ca)
    os.execve(argv[0], argv, environment)


if __name__ == "__main__":
    main()
