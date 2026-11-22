"""Built-in host credential methods."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


def run_bounded(
    argv: Sequence[str], *, timeout: float = 8, maximum: int = 64 * 1024
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return communicate_bounded(process, timeout=timeout, maximum=maximum)


def communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    input: bytes = b"",
    timeout: float = 8,
    maximum: int = 64 * 1024,
) -> subprocess.CompletedProcess[bytes]:
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    remaining_input = memoryview(input)
    try:
        for stream in streams:
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        if process.stdin is not None:
            if remaining_input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(remaining):
                if key.events == selectors.EVENT_WRITE:
                    written = os.write(key.fd, remaining_input[:8192])
                    remaining_input = remaining_input[written:]
                    if not remaining_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                available = maximum - sum(map(len, streams.values()))
                chunk = os.read(key.fd, min(8192, available + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(chunk)
                if sum(map(len, streams.values())) > maximum:
                    raise ValueError("provider output is too large")
        return subprocess.CompletedProcess(
            process.args,
            process.wait(timeout=max(0, deadline - time.monotonic())),
            bytes(streams[process.stdout]),
            bytes(streams[process.stderr]),
        )
    except BaseException:
        # Failed or cancelled communication must not leave the child running.
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        if process.stdin is not None:
            process.stdin.close()
        for stream in streams:
            stream.close()


class AwsCodeArtifact:
    def _options(self, options: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
        if set(options) - {"domain", "domain_owner", "region", "profile"} or not {
            "domain",
            "domain_owner",
            "region",
        } <= set(options):
            raise ValueError("invalid aws-codeartifact options")
        if not all(
            isinstance(options[name], str) and options[name]
            for name in ("domain", "domain_owner", "region")
        ):
            raise ValueError("invalid aws-codeartifact options")
        profile = options.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile):
            raise ValueError("invalid aws-codeartifact profile")
        return options["domain"], options["domain_owner"], options["region"], profile

    def describe(self, credential_id: str, options: Mapping[str, Any]) -> dict[str, Any]:
        del credential_id
        self._options(options)
        return {
            "header": {
                "name": "Authorization",
                "format": "basic-password",
                "username": "aws",
            }
        }

    def fetch(self, credential_id: str, options: Mapping[str, Any]) -> dict[str, Any]:
        del credential_id
        domain, owner, region, profile = self._options(options)
        argv = ["aws"]
        if profile:
            argv.extend(("--profile", profile))
        argv.extend(
            (
                "codeartifact",
                "get-authorization-token",
                "--domain",
                domain,
                "--domain-owner",
                owner,
                "--region",
                region,
                "--output",
                "json",
            )
        )
        result = run_bounded(argv)
        if result.returncode:
            raise RuntimeError("aws CodeArtifact token request failed")
        payload = json.loads(result.stdout)
        return {
            "value": payload["authorizationToken"],
            "expires_at": _expiration(payload["expiration"]),
        }


def _expiration(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise ValueError("invalid expiration")
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def aws_codeartifact() -> AwsCodeArtifact:
    return AwsCodeArtifact()
