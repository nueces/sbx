"""Read-only-shared workload for the disposable direct-IP QEMU integration guest."""

import base64
import http.client
import json
import socket
import ssl
import struct
import subprocess
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
public, private = config["public"], config["private"]
cached = socket.gethostbyname("cached.example")
assert cached.startswith(("198.18.", "198.19."))
# Any accidental normal DNS lookup now fails instead of quietly helping the IP path.
Path("/etc/resolv.conf").write_text("nameserver 203.0.113.53\noptions timeout:1 attempts:1\n")
context = ssl.create_default_context(cafile=str(Path(sys.argv[1]).with_name("gateway-ca.pem")))


def request(port, path, *, tls=False, headers=None, method="GET", expected=200):
    connection = (
        http.client.HTTPSConnection(public, port, context=context, timeout=8)
        if tls
        else http.client.HTTPConnection(public, port, timeout=8)
    )
    try:
        connection.connect()
        if tls:
            assert ("IP Address", public) in connection.sock.getpeercert()["subjectAltName"]
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        assert response.status == expected, (path, response.status)
        if expected == 200:
            assert body == b"direct-ip-ok" * 4096
    finally:
        connection.close()


def refused(host, port):
    try:
        connection = socket.create_connection((host, port), timeout=1)
    except OSError:
        return
    connection.close()
    raise AssertionError(("unexpected TCP access", host, port))


def check():
    for host in (public, private):
        with socket.create_connection((host, config["echo_port"]), timeout=5) as connection:
            connection.sendall(b"ip-echo")
            assert connection.recv(64) == b"ip-echo"
    request(config["http_port"], "/ip/plain")
    request(config["tls_port"], "/ip/tls", tls=True)
    request(config["bad_tls_port"], "/ip/wrong-certificate", tls=True, expected=502)
    request(config["http_port"], "/ip/forged-host", headers={"Host": "auth.example"}, expected=403)
    request(
        config["http_port"],
        "/ip/credential",
        headers={"Authorization": "Bearer " + "p" * 43},
        expected=403,
    )
    request(config["http_port"], "/ip/method", method="POST", expected=403)
    request(config["http_port"], "/outside", expected=403)
    refused(public, config["denied_port"])
    refused(config["other_public"], config["http_port"])
    refused(config["other_private"], config["echo_port"])
    # Explicit query checks stable DNS state without relying on the disabled system resolver.
    labels = b"\x06cached\x07example\x00"
    query = struct.pack("!HHHHHH", 17, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dns:
        dns.settimeout(3)
        dns.sendto(query, ("192.0.2.1", 53))
        reply = dns.recv(1232)
    assert reply[6:8] == b"\x00\x01" and socket.inet_ntoa(reply[-4:]) == cached


check()
print("DIRECT_IP_INITIAL_OK", flush=True)
assert input().strip() == "continue"
check()
state = {"cached_dns": cached, "resolv": Path("/etc/resolv.conf").read_text()}
for name, argv in {
    "addresses": ["ip", "-j", "address"],
    "routes": ["ip", "-j", "route"],
    "kernel": ["uname", "-a"],
}.items():
    state[name] = subprocess.check_output(argv, text=True)
for path in Path("/var/lib/dhcp").glob("*lease*"):
    if path.is_file():
        state[str(path)] = path.read_text()
subprocess.run(["sync"], check=True)
print("DIRECT_IP_GUEST_STATE " + base64.b64encode(json.dumps(state).encode()).decode(), flush=True)
print("DIRECT_IP_RECOVERY_OK", flush=True)
