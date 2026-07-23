#!/bin/sh
set -eu

iface=${1:-eth0}
ip link set "$iface" up
dhclient -1 "$iface"

package_port=
package_placeholder=
gateway_ca_port=
for value in $(cat /proc/cmdline); do
  case "$value" in
    sbx.package_port=*) package_port=${value#*=} ;;
    sbx.gateway_ca_port=*) gateway_ca_port=${value#*=} ;;
  esac
done
if [ -n "$gateway_ca_port" ]; then
  python3 - "$gateway_ca_port" <<'PY'
import socket
import sys

with socket.create_connection(("localhost.localdomain", int(sys.argv[1])), timeout=10) as conn:
    payload = b""
    while chunk := conn.recv(65536):
        payload += chunk
placeholder, certificate = payload.split(b"\n", 1)
assert placeholder.startswith(b"SBX_SECRET_")
assert certificate.count(b"BEGIN CERTIFICATE") == 1
assert b"PRIVATE KEY" not in certificate
open("/run/sbx-package-placeholder", "wb").write(placeholder)
open("/usr/local/share/ca-certificates/sbx-gateway-ca.crt", "wb").write(certificate)
PY
  package_placeholder=$(cat /run/sbx-package-placeholder)
  rm -f /run/sbx-package-placeholder
  update-ca-certificates >/dev/null
fi
export SBX_PACKAGE_PORT=$package_port SBX_PACKAGE_PLACEHOLDER=$package_placeholder

python3 - <<'PY' >/dev/ttyS0 2>&1 &
import base64
import http.client
import os
import queue
import socket
import ssl
import threading


def ingress_server(port, expected, body, results, ready):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen()
    listener.settimeout(30)
    ready.put(port)
    try:
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(10)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            target = request.split(b" ", 2)[1].decode()
            assert target == expected, (target, expected)
            conn.sendall(
                b"HTTP/1.0 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            results.put(target)
    finally:
        listener.close()


results = queue.Queue()
ready = queue.Queue()
threading.Thread(
    target=ingress_server,
    args=(3000, "/dev", b"guest-dev-server", results, ready),
    daemon=True,
).start()
threading.Thread(
    target=ingress_server,
    args=(4180, "/oauth/callback?code=visible-to-guest", b"oauth-ok", results, ready),
    daemon=True,
).start()
assert sorted(ready.get(timeout=10) for _ in range(2)) == [3000, 4180]
print("GATEWAY_INGRESS_READY", flush=True)

host = "localhost.localdomain"
ip = socket.gethostbyname(host)
assert ip.startswith("198.18.") or ip.startswith("198.19."), ip
old_tcp = socket.create_connection((host, 18080), timeout=5)
old_tcp.sendall(b"gateway-test")
assert old_tcp.recv(64) == b"gateway-test"

for destination in ((host, 18081), ("1.1.1.1", 80)):
    try:
        socket.create_connection(destination, timeout=2)
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected TCP access: {destination}")

try:
    socket.gethostbyname("blocked.invalid")
except OSError:
    pass
else:
    raise AssertionError("unexpected DNS answer")

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.settimeout(2)
udp.sendto(b"denied", ("1.1.1.1", 443))
try:
    udp.recv(1)
except OSError:
    pass
else:
    raise AssertionError("unexpected UDP response")
finally:
    udp.close()

assert not any(name.lower() in {"http_proxy", "https_proxy"} for name in os.environ)
port = int(os.environ["SBX_PACKAGE_PORT"])
placeholder = os.environ["SBX_PACKAGE_PLACEHOLDER"]
authorization = "Basic " + base64.b64encode(f"aws:{placeholder}".encode()).decode()
context = ssl.create_default_context()
for path, expected in (
    ("/pypi/repo/simple/demo/", b'<a href="/pypi/repo/packages/demo.whl">demo</a>'),
    ("/pypi/repo/packages/demo.whl", b"fake-wheel-content"),
):
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=10)
    connection.request("GET", path, headers={"Authorization": authorization})
    response = connection.getresponse()
    body = response.read()
    assert response.status == 200, (path, response.status, body)
    assert body == expected, (path, body)
    connection.close()

fake_token = b"fake-" + b"real-token-" + b"task10"
encoded_token = base64.b64encode(b"aws:" + fake_token)
for process in os.listdir("/proc"):
    if not process.isdigit():
        continue
    for name in ("cmdline", "environ"):
        try:
            content = open(f"/proc/{process}/{name}", "rb").read()
        except OSError:
            continue
        assert fake_token not in content and encoded_token not in content, (process, name)
assert sorted(results.get(timeout=30) for _ in range(2)) == [
    "/dev",
    "/oauth/callback?code=visible-to-guest",
]

for root in ("/etc", "/home", "/root", "/run", "/tmp", "/var"):
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            try:
                if os.path.getsize(path) > 8 * 1024 * 1024:
                    continue
                content = open(path, "rb").read()
            except OSError:
                continue
            assert fake_token not in content and encoded_token not in content, path

print(
    "GATEWAY_GUEST_FAKE_TOKEN_SCAN_PASS GATEWAY_PACKAGE_AUTH_PASS "
    "GATEWAY_OLD_FLOW_READY GATEWAY_TEST_PASS",
    flush=True,
)

old_tcp.settimeout(0.2)
deadline = __import__("time").monotonic() + 30
while __import__("time").monotonic() < deadline:
    try:
        old_tcp.sendall(b"old-flow")
        assert old_tcp.recv(64) == b"old-flow"
    except OSError:
        break
else:
    raise AssertionError("old TCP flow survived worker replacement")
old_tcp.close()

recovery_results = queue.Queue()
recovery_ready = queue.Queue()
threading.Thread(
    target=ingress_server,
    args=(3000, "/recovered", b"recovered-ingress", recovery_results, recovery_ready),
    daemon=True,
).start()
assert recovery_ready.get(timeout=10) == 3000
print("GATEWAY_RECOVERY_INGRESS_READY", flush=True)

for _ in range(100):
    try:
        with socket.create_connection((host, 18080), timeout=0.2) as conn:
            conn.sendall(b"new-flow")
            if conn.recv(64) == b"new-flow":
                break
    except OSError:
        __import__("time").sleep(0.05)
else:
    raise AssertionError("new TCP flow failed after worker replacement")
assert recovery_results.get(timeout=30) == "/recovered"
print("GATEWAY_OLD_TCP_FAILED GATEWAY_RECOVERY_PASS", flush=True)
PY
