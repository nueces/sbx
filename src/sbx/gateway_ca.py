from __future__ import annotations

import base64
import hashlib
import os
import shlex
import ssl
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sbx import runtime

GUEST_CA_PATH = "/usr/local/share/ca-certificates/sbx-gateway-ca.crt"


class GatewayCAError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayCA:
    public_certificate: Path
    fingerprint: str


def _owner_file(path: Path) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise GatewayCAError(f"unsafe gateway CA file: {path}")
    return path.read_bytes()


def certificate_fingerprint(certificate: bytes) -> str:
    if b"PRIVATE KEY" in certificate or certificate.count(b"BEGIN CERTIFICATE") != 1:
        raise GatewayCAError("gateway CA export is not a single public certificate")
    try:
        der = ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GatewayCAError("gateway CA certificate is invalid") from exc
    return hashlib.sha256(der).hexdigest()


def finalize_ca_state(confdir: Path) -> GatewayCA:
    info = confdir.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise GatewayCAError("gateway CA state directory must be owner-only")
    source = _owner_file(confdir / "mitmproxy-ca-cert.pem")
    _owner_file(confdir / "mitmproxy-ca.pem")
    fingerprint = certificate_fingerprint(source)
    public = confdir / "ca-cert.pem"
    try:
        fd = os.open(
            public,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        if _owner_file(public) != source:
            raise GatewayCAError("gateway CA public certificate changed") from None
    else:
        with os.fdopen(fd, "wb") as destination:
            destination.write(source)
    if certificate_fingerprint(_owner_file(public)) != fingerprint:
        raise GatewayCAError("gateway CA fingerprint mismatch")
    return GatewayCA(public, fingerprint)


def _guest_install_script(certificate: bytes, fingerprint: str) -> str:
    encoded = base64.b64encode(certificate).decode("ascii")
    target = shlex.quote(GUEST_CA_PATH)
    return f"""
set -euo pipefail
for command in base64 openssl sha256sum update-ca-certificates; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "unsupported Linux guest CA store" >&2
    exit 1
  fi
done
target={target}
expected={shlex.quote(fingerprint)}
fingerprint() {{ openssl x509 -in "$1" -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f1; }}
if [ ! -f "$target" ] || [ "$(fingerprint "$target")" != "$expected" ]; then
  install -d -m 0755 "$(dirname "$target")"
  temporary=$(mktemp "${{target}}.XXXXXXXX")
  trap 'rm -f "$temporary"' EXIT
  printf %s {shlex.quote(encoded)} | base64 -d > "$temporary"
  if [ "$(fingerprint "$temporary")" != "$expected" ]; then
    echo "gateway CA fingerprint mismatch" >&2
    exit 1
  fi
  chmod 0644 "$temporary"
  mv -f "$temporary" "$target"
  trap - EXIT
  update-ca-certificates >/dev/null
fi
actual=$(fingerprint "$target")
[ "$actual" = "$expected" ] || {{ echo "installed gateway CA fingerprint mismatch" >&2; exit 1; }}
printf '%s\n' "$actual"
"""


def install_guest_ca(
    vm_id: str,
    ca: GatewayCA,
    *,
    run_capture: Callable[[Sequence[str]], subprocess.CompletedProcess[str] | None] | None = None,
    ssh: Callable[[str], list[str]] | None = None,
    timeout: float = 60,
) -> str:
    ssh = ssh or runtime.ssh_command
    certificate = _owner_file(ca.public_certificate)
    fingerprint = certificate_fingerprint(certificate)
    if fingerprint != ca.fingerprint:
        raise GatewayCAError("gateway CA changed before guest installation")
    script = shlex.quote(_guest_install_script(certificate, fingerprint))
    command = [*ssh(vm_id), "bash -lc " + script]
    completed = (
        runtime.run_capture(command, timeout=timeout)
        if run_capture is None
        else run_capture(command)
    )
    if completed is None:
        raise GatewayCAError("failed to install gateway CA: SSH command not found")
    installed = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    if completed.returncode != 0 or installed != fingerprint:
        detail = completed.stderr.strip() or "guest fingerprint verification failed"
        raise GatewayCAError(f"failed to install gateway CA: {detail[:500]}")
    return installed
