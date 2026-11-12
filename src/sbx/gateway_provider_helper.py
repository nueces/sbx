"""Killable subprocess boundary for trusted host credential methods."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import sys
import threading
from typing import Any

from sbx.gateway_credentials import run_bounded


def _monitor(fd: int) -> None:
    with os.fdopen(fd, "rb") as lifetime:
        lifetime.read()
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _plugin(name: str, operation: str, payload: dict[str, Any]) -> object:
    matches = tuple(
        importlib.metadata.entry_points(group="sbx.network.auth.methods").select(name=name)
    )
    if len(matches) != 1:
        raise RuntimeError("credential plugin is missing or ambiguous")
    loaded = matches[0].load()
    provider = loaded if hasattr(loaded, operation) else loaded()
    callback = getattr(provider, operation)
    return callback(payload["credential_id"], payload["options"])


def _command(operation: str, payload: dict[str, Any]) -> object:
    if operation == "describe":
        return {"header": None}
    result = run_bounded(payload["argv"])
    if result.returncode:
        raise RuntimeError("credential command failed")
    if payload["format"] == "text":
        raw = result.stdout
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or any(character in raw for character in (b"\r", b"\n", b"\0")):
            raise ValueError("invalid credential command output")
        return {"value": raw.decode(), "expires_at": None}
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("invalid credential command output")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True)
    parser.add_argument("--operation", choices=("describe", "fetch"), required=True)
    parser.add_argument("--lifetime-fd", type=int, required=True)
    args = parser.parse_args()
    threading.Thread(target=_monitor, args=(args.lifetime_fd,), daemon=True).start()
    os.environ.pop("LC_CTYPE", None)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError
        if args.method == "command":
            result = _command(args.operation, payload)
        elif args.method.startswith("plugin:"):
            result = _plugin(args.method[7:], args.operation, payload)
        else:
            raise ValueError
        json.dump(result, sys.stdout, separators=(",", ":"))
    except BaseException:
        print("credential provider failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
