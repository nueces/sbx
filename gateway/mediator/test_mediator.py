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


def credential(key: bytes, *, vm: str = "vm1", host: str = "example.com") -> tuple[str, str]:
    expires = int(time.time()) + 30
    nonce = base64.urlsafe_b64encode(b"123456789012").rstrip(b"=").decode()
    username = f"1|{vm}|worker1|flow1|rule1|{expires}|{nonce}|{host}|443"
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

        altered, _ = credential(key, host="other.example")
        self.assertIsNone(verifier.verify(altered, password))
        cross_vm, cross_password = credential(key, vm="vm2")
        self.assertIsNone(verifier.verify(cross_vm, cross_password))


if __name__ == "__main__":
    unittest.main()
