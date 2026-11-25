"""Read-only-shared workload for the disposable default-allow QEMU integration guest."""

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
context = ssl.create_default_context(cafile=str(Path(sys.argv[1]).with_name("gateway-ca.pem")))
placeholder = config["placeholder"]


def query(name):
    """Raw A query to the gateway resolver: (rcode, answers)."""
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\x00"
    packet = struct.pack("!HHHHHH", 29, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dns:
        dns.settimeout(8)
        dns.sendto(packet, ("192.0.2.1", 53))
        reply = dns.recv(1232)
    count = struct.unpack("!H", reply[6:8])[0]
    answers = [
        socket.inet_ntoa(reply[len(reply) - 16 * i - 4 : len(reply) - 16 * i]) for i in range(count)
    ]
    return reply[3] & 15, sorted(answers)


def echo(host, port):
    with socket.create_connection((host, port), timeout=8) as connection:
        connection.sendall(b"default-echo")
        assert connection.recv(64) == b"default-echo", (host, port)


def refused(host, port):
    try:
        connection = socket.create_connection((host, port), timeout=3)
    except OSError:
        return
    connection.close()
    raise AssertionError(("unexpected TCP access", host, port))


def https(host, path, *, method="GET", expected=200):
    connection = http.client.HTTPSConnection(host, config["tls_port"], context=context, timeout=10)
    try:
        connection.request(method, path, headers={"Authorization": "Bearer " + placeholder})
        response = connection.getresponse()
        response.read()
        assert response.status == expected, (host, path, method, response.status)
    finally:
        connection.close()


def raw_http(host, path):
    connection = http.client.HTTPConnection(host, config["http_port"], timeout=10)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + placeholder})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200 and body == b"default-allow-ok", (host, path, response.status)
    finally:
        connection.close()


def check(cached):
    resolver = [
        line.split()[1]
        for line in Path("/etc/resolv.conf").read_text().splitlines()
        if line.startswith("nameserver")
    ]
    assert resolver == ["192.0.2.1"], resolver

    # DNS: policy names are synthetic and stable; unmatched names get filtered real answers.
    api_rcode, api = query("api.example")
    assert api_rcode == 0 and len(api) == 1 and api[0].startswith(("198.18.", "198.19.")), api
    git_rcode, git = query("git.example")
    assert git_rcode == 0 and git == [cached], (git, cached)
    assert query("open.example") == (0, [public])
    assert query("lan.example") == (0, [private])
    assert query("missing.example")[0] == 3, "missing name must be NXDOMAIN"
    assert query("meta.example") == (0, []), "protected-only answers must be NODATA"

    # Raw default route to public/private literals and real-answer names.
    for host in (public, private, "open.example", "lan.example"):
        echo(host, config["echo_port"])
    # Same policy host on an unmatched port: default route, token-free.
    echo("api.example", config["echo_port"])
    # Unmatched literal HTTP carries the guest placeholder unchanged (no injection).
    raw_http(public, "/raw/literal")

    # Explicit policy is enforced; denials never fall through to the default route.
    https("api.example", "/v1/allowed")
    https("api.example", "/outside", expected=403)
    https("api.example", "/v1/post", method="POST", expected=403)

    # Protected destinations stay blocked, and dead ports do not look open.
    for host, port in (
        ("169.254.169.254", config["echo_port"]),
        ("100.100.100.200", config["echo_port"]),
        ("198.18.200.200", config["echo_port"]),
        ("192.0.2.1", 22),
        (public, 1),  # allowed address, nothing listening: refused, not accepted then closed
    ):
        refused(host, port)
    return api[0]


_, cached_answers = query("git.example")
cached = cached_answers[0]
api_before = check(cached)
print("DEFAULT_ALLOW_INITIAL_OK", flush=True)
assert input().strip() == "continue"
api_after = check(cached)
assert api_after == api_before, "policy synthetic identity changed across replacement"
state = {
    "cached_dns": cached,
    "api_dns": api_before,
    "resolv": Path("/etc/resolv.conf").read_text(),
}
for name, argv in {
    "addresses": ["ip", "-j", "address"],
    "routes": ["ip", "-j", "route"],
    "kernel": ["uname", "-a"],
}.items():
    state[name] = subprocess.check_output(argv, text=True)
subprocess.run(["sync"], check=True)
print(
    "DEFAULT_ALLOW_GUEST_STATE " + base64.b64encode(json.dumps(state).encode()).decode(), flush=True
)
print("DEFAULT_ALLOW_RECOVERY_OK", flush=True)
