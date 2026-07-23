import base64
import hashlib
import hmac
import os
import time
import unittest

read_fd, write_fd = os.pipe()
os.write(write_fd, b"x" * 32)
os.close(write_fd)
os.environ["SBX_GATEWAY_KEY_FD"] = str(read_fd)
os.environ["SBX_GATEWAY_VM_ID"] = "vm1"
os.environ["SBX_GATEWAY_INCARNATION"] = "worker1"

from sbx_gateway_mediator import CapabilityVerifier  # noqa: E402


def credential(
    key: bytes,
    *,
    vm: str = "vm1",
    incarnation: str = "worker1",
    host: str = "example.com",
    port: int = 443,
    issued: int | None = None,
    expires: int | None = None,
) -> tuple[str, str]:
    issued = int(time.time()) if issued is None else issued
    expires = issued + 30 if expires is None else expires
    nonce = base64.urlsafe_b64encode(b"123456789012").rstrip(b"=").decode()
    username = (
        f"1|{vm}|{incarnation}|flow1|rule1|{issued}|{expires}|{nonce}|{host}|{port}"
    )
    digest = hmac.new(key, username.encode(), hashlib.sha256).digest()
    password = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return username, password


class CapabilityTests(unittest.TestCase):
    def test_exactly_once_and_identity_bound(self) -> None:
        key = b"k" * 32
        verifier = CapabilityVerifier(key, "vm1", "worker1")
        username, password = credential(key)
        admission = verifier.verify(username, password)
        self.assertEqual((admission.host, admission.port), ("example.com", 443))
        self.assertIsNone(verifier.verify(username, password))

        for values in ({"vm": "vm2"}, {"incarnation": "worker0"}):
            altered, altered_password = credential(key, **values)
            self.assertIsNone(verifier.verify(altered, altered_password))
        self.assertIsNone(
            verifier.verify(username.replace("example.com", "other.example"), password)
        )
        self.assertIsNone(verifier.verify(username.removesuffix("443") + "80", password))

    def test_rejects_expired_and_altered_credentials(self) -> None:
        key = b"k" * 32
        now = int(time.time())
        verifier = CapabilityVerifier(key, "vm1", "worker1")
        expired, password = credential(key, issued=now - 31, expires=now - 1)
        self.assertIsNone(verifier.verify(expired, password, now=now))
        username, password = credential(key, issued=now)
        self.assertIsNone(verifier.verify(username, password + "x", now=now))
        self.assertIsNone(verifier.verify(username.replace("rule1", "rule2"), password, now=now))


if __name__ == "__main__":
    unittest.main()
