#!/bin/sh
set -eu

iface=${1:-eth0}
ip link set "$iface" up
dhclient -1 "$iface"

package_port=
package_placeholder=
gateway_ca_port=
capacity_id=
capacity_echo_port=
capacity_requests=1
task19_mode=
task19_port=
task19_ca_port=
for value in $(cat /proc/cmdline); do
  case "$value" in
    sbx.package_port=*) package_port=${value#*=} ;;
    sbx.gateway_ca_port=*) gateway_ca_port=${value#*=} ;;
    sbx.capacity_id=*) capacity_id=${value#*=} ;;
    sbx.capacity_echo_port=*) capacity_echo_port=${value#*=} ;;
    sbx.capacity_requests=*) capacity_requests=${value#*=} ;;
    sbx.task19_mode=*) task19_mode=${value#*=} ;;
    sbx.task19_port=*) task19_port=${value#*=} ;;
    sbx.task19_ca_port=*) task19_ca_port=${value#*=} ;;
  esac
done
if [ -n "$task19_mode" ]; then
  python3 - "$task19_mode" "$task19_port" "$task19_ca_port" <<'PY' >/dev/ttyS0 2>&1
import base64
import concurrent.futures
import http.client
import json
import socket
import subprocess
import ssl
import struct
import sys
import threading
import time

mode, port, ca_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
hosts = ("a.localhost", "b.localhost")
if mode == "slirp":
    with open("/etc/hosts", "a", encoding="ascii") as hosts_file:
        hosts_file.write("10.0.2.2 a.localhost b.localhost\n")

with socket.create_connection((hosts[0], ca_port), timeout=10) as connection:
    certificate = b""
    while chunk := connection.recv(65536):
        certificate += chunk
assert certificate.count(b"BEGIN CERTIFICATE") == 1
with open("/run/task19-ca.pem", "wb") as output:
    output.write(certificate)
context = ssl.create_default_context(cafile="/run/task19-ca.pem")


def request(host, path, method="GET", body=None, *, timeout=60):
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=timeout)
    started = time.perf_counter()
    connection.request(method, path, body=body)
    response = connection.getresponse()
    payload = response.read()
    elapsed = time.perf_counter() - started
    status = response.status
    connection.close()
    assert status == 200, (path, status, payload[:100])
    return payload, elapsed


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def dns_query(name):
    labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"
    packet = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", 1, 1)
    nameserver = next(
        line.split()[1]
        for line in open("/etc/resolv.conf", encoding="ascii")
        if line.startswith("nameserver ")
    )
    query = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    query.settimeout(5)
    try:
        query.sendto(packet, (nameserver, 53))
        reply = query.recv(1232)
    finally:
        query.close()
    assert len(reply) >= 12 and struct.unpack("!H", reply[6:8])[0] == 1


class Ingress(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.done = threading.Event()

    def run(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 3000))
        listener.listen()
        print("TASK19_INGRESS_READY", flush=True)
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(30)
            request_data = b""
            while b"\r\n\r\n" not in request_data:
                request_data += connection.recv(4096)
            size = 100 * 1024 * 1024
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(size).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            chunk = b"i" * 65536
            for _ in range(size // len(chunk)):
                connection.sendall(chunk)
        listener.close()
        self.done.set()


ingress = Ingress()
ingress.start()
assert ingress.done.wait(120), "ingress benchmark did not run"
results = {"mode": mode}
for size in (1024 * 1024, 100 * 1024 * 1024, 32 * 1024 * 1024):
    started = time.perf_counter()
    subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--cacert", "/run/task19-ca.pem",
         "--output", "/dev/null", f"https://{hosts[0]}:{port}/bytes/{size}"],
        check=True,
        timeout=180,
    )
    elapsed = time.perf_counter() - started
    results[f"throughput_{size}_mbps"] = size * 8 / elapsed / 1_000_000

connection = http.client.HTTPSConnection(hosts[0], port, context=context, timeout=30)
latencies = []
for _ in range(100):
    started = time.perf_counter()
    connection.request("GET", "/latency")
    response = connection.getresponse()
    assert response.status == 200 and response.read() == b"xx"
    latencies.append((time.perf_counter() - started) * 1000)
connection.close()
results["latency_ms"] = {
    "p50": percentile(latencies, 0.50),
    "p95": percentile(latencies, 0.95),
    "p99": percentile(latencies, 0.99),
}

started = time.perf_counter()
for index in range(100):
    dns_query(hosts[index % 2])
results["dns_lookups_per_second"] = 100 / (time.perf_counter() - started)

barrier = threading.Barrier(128)
def concurrent_request(index):
    barrier.wait(timeout=30)
    body, _ = request(hosts[index % 2], "/concurrent", timeout=60)
    return body

with concurrent.futures.ThreadPoolExecutor(max_workers=128) as executor:
    bodies = list(executor.map(concurrent_request, range(128)))
assert bodies == [b"concurrent"] * 128
results["concurrent_requests"] = len(bodies)

upload = b"u" * (8 * 1024 * 1024)
body, upload_seconds = request(hosts[0], "/slow-read", "POST", upload, timeout=180)
assert body == str(len(upload)).encode()
body, download_seconds = request(hosts[0], "/slow-write/8388608", timeout=180)
assert len(body) == 8 * 1024 * 1024
results["backpressure"] = {
    "upload_bytes": len(upload),
    "upload_seconds": upload_seconds,
    "download_bytes": len(body),
    "download_seconds": download_seconds,
}
encoded = base64.b64encode(json.dumps(results, sort_keys=True).encode()).decode()
for _ in range(3):
    print("TASK19_RESULT " + encoded, flush=True)
PY
  exit 0
fi
if [ -n "$capacity_id" ]; then
  python3 - "$capacity_id" "$capacity_echo_port" "$capacity_requests" <<'PY' >/dev/ttyS0 2>&1
import socket
import sys

vm_id, echo_port, request_count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
host = "localhost.localdomain"
ip = socket.gethostbyname(host)
assert ip.startswith(("198.18.", "198.19.")), ip
with socket.create_connection((host, echo_port), timeout=10) as conn:
    payload = f"vm-{vm_id}".encode()
    conn.sendall(payload)
    assert conn.recv(64) == payload
try:
    socket.create_connection(("1.1.1.1", 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError("capacity guest obtained direct IP access")
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", 3000))
listener.listen()
listener.settimeout(60)
print(f"GATEWAY_CAPACITY_READY {vm_id}", flush=True)
for _ in range(request_count):
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(5)
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                break
            request += chunk
        body = f"capacity-{vm_id}".encode()
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
listener.close()
print(f"GATEWAY_CAPACITY_PASS {vm_id}", flush=True)
PY
  exit 0
fi
if [ -n "$gateway_ca_port" ]; then
  python3 - "$gateway_ca_port" <<'PY'
import socket
import sys

with socket.create_connection(("localhost.localdomain", int(sys.argv[1])), timeout=10) as conn:
    payload = b""
    while chunk := conn.recv(65536):
        payload += chunk
placeholder, certificate = payload.split(b"\n", 1)
assert len(placeholder) >= 32 and b"SBX" not in placeholder
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
import time


def ingress_server(port, expected, body, results, ready):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen()
    listener.settimeout(1)
    ready.put(port)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(10)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                parts = request.split(b" ", 2)
                target = parts[1].decode() if len(parts) == 3 else ""
                if target != expected:
                    continue
                conn.sendall(
                    b"HTTP/1.0 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                results.put(target)
                return
        raise TimeoutError(f"expected ingress request did not arrive: {expected}")
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

for destination in ((host, 18081), ("1.1.1.1", 80), ("1.1.1.1", 53)):
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

for port, payload in (
    (53, b"\0\1\1\0\0\1\0\0\0\0\0\0\7example\3com\0\0\1\0\1"),
    (443, b"denied"),
):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.settimeout(2)
    udp.sendto(payload, ("1.1.1.1", port))
    try:
        udp.recv(1)
    except OSError:
        pass
    else:
        raise AssertionError(f"unexpected UDP response from port {port}")
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
    "GATEWAY_GUEST_FAKE_TOKEN_SCAN_PASS GATEWAY_CREDENTIAL_INJECTION_PASS "
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
