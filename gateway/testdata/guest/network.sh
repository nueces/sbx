#!/bin/sh
set -eu

iface=${1:-eth0}
ip link set "$iface" up
dhclient -1 "$iface"

python3 - <<'PY' >/dev/ttyS0 2>&1 &
import socket

host = "localhost.localdomain"
ip = socket.gethostbyname(host)
assert ip.startswith("198.18.") or ip.startswith("198.19."), ip
with socket.create_connection((host, 18080), timeout=5) as conn:
    conn.sendall(b"gateway-test")
    assert conn.recv(64) == b"gateway-test"

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

print("GATEWAY_TEST_PASS", flush=True)
PY
