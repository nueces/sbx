from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sbx import gateway_ca


def ca_state(tmp_path: Path, vm: str = "vm") -> tuple[Path, bytes]:
    directory = tmp_path / vm / "mediator"
    directory.mkdir(parents=True, mode=0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sbx VM test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    for name, content in {
        "mitmproxy-ca-cert.pem": certificate,
        "mitmproxy-ca.pem": private + certificate,
    }.items():
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o600)
    return directory, private


def test_ca_state_exports_only_owner_only_public_certificate(tmp_path: Path) -> None:
    directory, private = ca_state(tmp_path)

    ca = gateway_ca.finalize_ca_state(directory)
    repeated = gateway_ca.finalize_ca_state(directory)

    assert repeated == ca
    assert ca.public_certificate.name == "ca-cert.pem"
    assert ca.public_certificate.stat().st_mode & 0o777 == 0o600
    assert b"PRIVATE KEY" not in ca.public_certificate.read_bytes()
    assert private not in ca.public_certificate.read_bytes()
    assert gateway_ca.certificate_fingerprint(ca.public_certificate.read_bytes()) == ca.fingerprint


def test_two_vm_ca_keys_and_certificates_are_distinct(tmp_path: Path) -> None:
    directory_a, private_a = ca_state(tmp_path, "vm-a")
    directory_b, private_b = ca_state(tmp_path, "vm-b")

    ca_a = gateway_ca.finalize_ca_state(directory_a)
    ca_b = gateway_ca.finalize_ca_state(directory_b)

    assert private_a != private_b
    assert ca_a.fingerprint != ca_b.fingerprint
    assert ca_a.public_certificate.parent != ca_b.public_certificate.parent
    assert private_a not in ca_b.public_certificate.read_bytes()
    assert private_b not in ca_a.public_certificate.read_bytes()


def test_ca_state_rejects_non_owner_state_and_changed_export(tmp_path: Path) -> None:
    directory, _ = ca_state(tmp_path)
    (directory / "mitmproxy-ca-cert.pem").chmod(0o644)
    with pytest.raises(gateway_ca.GatewayCAError, match="unsafe"):
        gateway_ca.finalize_ca_state(directory)

    (directory / "mitmproxy-ca-cert.pem").chmod(0o600)
    gateway_ca.finalize_ca_state(directory)
    (directory / "ca-cert.pem").write_text("changed", encoding="ascii")
    with pytest.raises(gateway_ca.GatewayCAError, match="changed"):
        gateway_ca.finalize_ca_state(directory)


def test_guest_ca_script_is_idempotent_and_verifies_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = ca_state(tmp_path)
    ca = gateway_ca.finalize_ca_state(directory)
    guest = tmp_path / "guest" / "usr" / "local" / "share" / "ca-certificates"
    target = guest / "sbx-gateway-ca.crt"
    monkeypatch.setattr(gateway_ca, "GUEST_CA_PATH", str(target))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    updates = tmp_path / "updates"
    updater = fake_bin / "update-ca-certificates"
    updater.write_text(f"#!/bin/sh\nprintf 'updated\\n' >> {updates}\n", encoding="utf-8")
    updater.chmod(0o755)
    environment = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    script = gateway_ca._guest_install_script(ca.public_certificate.read_bytes(), ca.fingerprint)

    first = subprocess.run(
        ["bash", "-c", script], check=False, text=True, capture_output=True, env=environment
    )
    second = subprocess.run(
        ["bash", "-c", script], check=False, text=True, capture_output=True, env=environment
    )

    assert (first.returncode, first.stdout.strip()) == (0, ca.fingerprint)
    assert (second.returncode, second.stdout.strip()) == (0, ca.fingerprint)
    assert updates.read_text().splitlines() == ["updated"]
    assert b"PRIVATE KEY" not in target.read_bytes()
    assert gateway_ca.certificate_fingerprint(target.read_bytes()) == ca.fingerprint


def test_guest_install_interface_exposes_no_private_key_and_denies_bad_verification(
    tmp_path: Path,
) -> None:
    directory, private = ca_state(tmp_path)
    ca = gateway_ca.finalize_ca_state(directory)
    commands: list[list[str]] = []

    def verified(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, ca.fingerprint + "\n", "")

    installed = gateway_ca.install_guest_ca(
        "vm1", ca, run_capture=verified, ssh=lambda _: ["ssh", "vm1"]
    )
    assert installed == ca.fingerprint
    rendered = " ".join(commands[0]).encode()
    assert private not in rendered
    assert str(directory / "mitmproxy-ca.pem").encode() not in rendered
    assert b"PRIVATE KEY" not in rendered

    def wrong(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "wrong\n", "")

    with pytest.raises(gateway_ca.GatewayCAError, match="fingerprint verification"):
        gateway_ca.install_guest_ca("vm1", ca, run_capture=wrong, ssh=lambda _: ["ssh", "vm1"])
