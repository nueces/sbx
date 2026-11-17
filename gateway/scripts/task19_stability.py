#!/usr/bin/env python3
"""Ten-minute mixed acceptance through a real VM and an installed gateway.

The caller creates an isolated VM/public synthetic origin and retains its audits.
This runner never substitutes mediator fixtures for VM traffic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from sbx import gateway, guest_setup

# Scripts also run from outside the checkout against wheel-installed sbx.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gateway.scripts.resource_usage import process_usage  # noqa: E402

DURATION_SECONDS = 600
SAMPLE_SECONDS = 2
RSS_CEILING = 512 * 1024 * 1024
FD_CEILING = 4096
ALLOWANCES = {"rss_bytes": 4 * 1024 * 1024, "fds": 2, "processes": 0, "threads": 0}

# Only opaque guest credentials enter these commands. Origin values stay on the host.
GUEST_SETUP = r"""
import pathlib, subprocess
root=pathlib.Path('/tmp/sbx-task19')
root.mkdir(exist_ok=True)
(root/'ingress.txt').write_text('task19-ingress-ok')
process=subprocess.Popen(['python3','-m','http.server','3000','--bind','0.0.0.0','--directory',str(root)],
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
(root/'ingress.pid').write_text(str(process.pid))
subprocess.run(['uv','venv','--clear','--python','/usr/bin/python3',str(root/'venv')],check=True,timeout=30)
"""

GUEST_WORKLOAD = r'''
import base64, hashlib, http.client, json, os, pathlib, shutil
import socket, ssl, struct, subprocess, time
from urllib.parse import urlsplit
assert os.getuid()==1000
parsed=urlsplit(URL)
auth='Bearer '+os.environ['GWC_TOKEN']
context=ssl.create_default_context()
def request(path):
    connection=http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,
                                           context=context,timeout=25)
    connection.request('GET',path,headers={'Authorization':auth})
    response=connection.getresponse()
    assert response.status==200,(path,response.status)
    data=response.read()
    connection.close()
    return data
assert request('/cmd/ok')==b'gateway-ok'
assert (hashlib.sha256(request('/cmd/bytes/1048576')).hexdigest()
        ==hashlib.sha256(b'x'*1048576).hexdigest())
connection=http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,
                                       context=context,timeout=25)
connection.request('GET','/cmd/sse',headers={'Authorization':auth})
response=connection.getresponse()
assert response.status==200
arrivals=[]
while line:=response.readline():
    if line.startswith(b'data: '):
        arrivals.append((line.strip(),time.monotonic()))
connection.close()
assert [x[0] for x in arrivals]==[b'data: 0',b'data: 1',b'data: 2'],arrivals
assert arrivals[-1][1]-arrivals[0][1]>=1.0, 'SSE was buffered instead of incrementally delivered'
raw=socket.create_connection((parsed.hostname,parsed.port or 443),timeout=25)
with context.wrap_socket(raw,server_hostname=parsed.hostname) as sock:
    key=base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f'GET /cmd/ws HTTP/1.1\r\nHost: {parsed.netloc}\r\nAuthorization: {auth}\r\n'
                  f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                  f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
    data=b''
    while b'\r\n\r\n' not in data: data+=sock.recv(1)
    assert b' 101 ' in data.split(b'\r\n',1)[0]
    payload=b'task19-echo'; mask=os.urandom(4)
    sock.sendall(bytes([0x81,0x80|len(payload)])+mask
                 +bytes(b^mask[i%4] for i,b in enumerate(payload)))
    def exact(size):
        data=b''
        while len(data)<size:
            chunk=sock.recv(size-len(data));assert chunk
            data+=chunk
        return data
    assert exact(2)==bytes([0x81,len(payload)]) and exact(len(payload))==payload
node=r"""
const h2=require('http2'); const assert=require('assert');
const client=h2.connect(process.env.TASK19_ORIGIN);
const timeout=setTimeout(()=>{client.destroy();process.exit(3)},25000);
client.on('error',error=>{console.error(error.message);process.exit(2)});
const request=client.request({':path':'/cmd/ok','authorization':'Bearer '+process.env.GWC_TOKEN});
let data=''; request.on('response',headers=>assert.equal(headers[':status'],200));
request.on('data',chunk=>data+=chunk);
request.on('end',()=>{assert.equal(data,'gateway-ok');clearTimeout(timeout);client.close()});
request.end();
"""
subprocess.run(['node','-e',node],env={**os.environ,'TASK19_ORIGIN':URL},check=True,timeout=30)
basic=base64.b64encode(('aws:'+os.environ['GWC_AWS']).encode()).decode()
root=pathlib.Path('/tmp/sbx-task19')
clone=root/'clone'
shutil.rmtree(clone,ignore_errors=True)
subprocess.run(['git','-c','http.extraHeader=Authorization: Basic '+basic,
                'clone','--quiet',URL+'/aws/git/fixture.git',str(clone)],check=True,timeout=30)
head=subprocess.check_output(['git','-C',str(clone),'rev-parse','HEAD'],text=True).strip()
assert head==COMMIT and (clone/'evidence.txt').read_text()=='populated task19 fixture\n'
index='https://aws:'+os.environ['GWC_AWS']+'@'+parsed.netloc+'/aws/simple/'
subprocess.run(['uv','--native-tls','--no-cache','pip','install','--python',str(root/'venv/bin/python'),
                '--no-deps','--reinstall','--index-url',index,'gwv-package'],check=True,timeout=30)
subprocess.run([str(root/'venv/bin/python'),'-c',
                'import gwv_package; assert gwv_package.__version__=="0.1"'],check=True,timeout=10)
print(json.dumps({'status':'mixed-pass','sse_span_seconds':arrivals[-1][1]-arrivals[0][1],
                  'commit':head,'workloads':['https','transfer','sse','websocket','http2','git','package']}))
'''


def guest_command(code: str) -> str:
    # The SSH adapter normalizes literal newlines, including inside shell quotes.
    command = "python3 -c " + shlex.quote(f"exec({code!r})")
    return "sudo -iu agent bash -lc " + shlex.quote(command)


def ready_state(vm_id: str, root: Path) -> dict:
    status = gateway.control_request(vm_id, "status", root=root)
    if status["status"] != "ready":
        raise RuntimeError("gateway not ready: " + status["status"])
    stored = gateway.read_state(root / vm_id / status["generation"], vm_id=vm_id)
    if (
        stored["status"] != "ready"
        or stored["generation"] != status["generation"]
        or stored["requested_config_digest"] != status["config_digest"]
    ):
        raise RuntimeError("gateway state changed during measurement")
    return stored


def growth_exceeded(samples: list[dict], field: str, allowance: int) -> bool:
    groups: dict[tuple[str, tuple[int, ...]], list[int]] = {}
    for row in samples:
        key = (row["generation"], tuple(row["pids"]))
        groups.setdefault(key, []).append(int(row["usage"][field]))
    return not groups or any(
        len(values) < 12 or values[-1] - values[len(values) // 4] > allowance
        for values in groups.values()
    )


def check_quiescent_growth(baseline: dict, final: dict) -> None:
    if (baseline["generation"], baseline["pids"]) != (final["generation"], final["pids"]):
        raise RuntimeError("quiescent checkpoints crossed gateway pairs")
    for field, allowance in ALLOWANCES.items():
        if final["usage"][field] - baseline["usage"][field] > allowance:
            raise RuntimeError("quiescent growth: " + field)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--gateway-root", required=True, type=Path)
    parser.add_argument("--ingress-port", required=True, type=int)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password:
        parser.error("origin must be the synthetic HTTPS fixture without userinfo")
    assert os.getuid() != 0 and not Path("/dev/kvm").exists()
    os.umask(0o077)
    args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((args.artifacts / name).exists() for name in ("samples.jsonl", "result.json")):
        parser.error("use a fresh artifacts directory; historical evidence is never overwritten")
    stop = threading.Event()
    lock = threading.Lock()
    samples: list[dict] = []
    failures: list[str] = []
    result: dict = {
        "vm": args.vm,
        "origin": args.origin,
        "git_commit": args.git_commit,
        "gateway_module": gateway.__file__,
        "guest_module": guest_setup.__file__,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "required_seconds": DURATION_SECONDS,
        "mixed_iterations": 0,
    }

    def state():
        return ready_state(args.vm, args.gateway_root)

    def measure():
        current = state()
        roots = [current["pids"][name] for name in ("worker", "mediator")]
        rss, fds, processes, threads = process_usage(roots)
        return {
            "generation": current["generation"],
            "pids": roots,
            "usage": dict(rss_bytes=rss, fds=fds, processes=processes, threads=threads),
        }

    def monitor(started):
        try:
            while not stop.is_set():
                with lock:
                    row = measure()
                    row["elapsed_seconds"] = time.monotonic() - started
                    if row["usage"]["rss_bytes"] > RSS_CEILING or row["usage"]["fds"] > FD_CEILING:
                        raise RuntimeError("resource ceiling exceeded")
                    if samples and row["elapsed_seconds"] - samples[-1]["elapsed_seconds"] > 10:
                        raise RuntimeError("resource sampling gap exceeded ten seconds")
                    samples.append(row)
                    with (args.artifacts / "samples.jsonl").open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                stop.wait(SAMPLE_SECONDS)
        except Exception as exc:
            failures.append(str(exc))
            stop.set()

    def guest(vm, code, timeout=120):
        response = vm.run(guest_command(code), timeout=timeout)
        with (args.artifacts / "guest.log").open("a") as stream:
            stream.write(response.stdout + response.stderr)
        if response.exit_code:
            raise RuntimeError(
                f"guest workload failed: {response.exit_code}; see private guest.log"
            )
        return response.stdout

    def interrupted(signum, _frame):
        raise RuntimeError(f"stability interrupted by {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    thread = None
    with guest_setup.open_guest(args.vm) as vm:
        vm.wait_for_ready(timeout=30)
        boot = vm.run("cat /proc/sys/kernel/random/boot_id", timeout=15)
        assert (
            boot.exit_code == 0
            and boot.stdout.strip() != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        result["guest_boot_id"] = boot.stdout.strip()
        try:
            guest(vm, GUEST_SETUP, 60)
            started = time.monotonic()
            result["initial_idle"] = measure()
            thread = threading.Thread(target=monitor, args=(started,), daemon=True)
            thread.start()
            while time.monotonic() - started < DURATION_SECONDS and not failures:
                guest(
                    vm,
                    "URL="
                    + repr(args.origin)
                    + "\nCOMMIT="
                    + repr(args.git_commit)
                    + "\n"
                    + GUEST_WORKLOAD,
                )
                with urlopen(
                    f"http://127.0.0.1:{args.ingress_port}/ingress.txt", timeout=15
                ) as response:
                    assert response.read() == b"task19-ingress-ok"
                result["mixed_iterations"] += 1
                elapsed = time.monotonic() - started
                if (
                    "replacement" in result
                    and "recovered_idle" not in result
                    and result["mixed_iterations"] >= result["replacement"]["mixed_at"] + 3
                ):
                    time.sleep(SAMPLE_SECONDS)
                    with lock:
                        result["recovered_idle"] = measure()
                if elapsed >= 150 and "warm_idle" not in result:
                    time.sleep(SAMPLE_SECONDS)
                    with lock:
                        result["warm_idle"] = measure()
                if elapsed >= 300 and "replacement" not in result:
                    with lock:
                        before = measure()
                        previous = state()["pids"]["worker"]
                        descriptor = os.pidfd_open(previous)
                        try:
                            assert state()["pids"]["worker"] == previous
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        finally:
                            os.close(descriptor)
                        deadline = time.monotonic() + 25
                        while time.monotonic() < deadline:
                            try:
                                replacement = state()
                                if replacement["pids"].get("worker") not in (None, previous):
                                    break
                            except (OSError, RuntimeError):
                                pass
                            time.sleep(0.1)
                        else:
                            raise RuntimeError("worker replacement failed")
                        result["replacement"] = {
                            "old": previous,
                            "new": replacement["pids"]["worker"],
                            "mixed_at": result["mixed_iterations"],
                            "before_idle": before,
                            "ready_idle": measure(),
                        }
                    vm.wait_for_ready(timeout=25)
            result["elapsed_seconds"] = time.monotonic() - started
            if (
                failures
                or result["elapsed_seconds"] < DURATION_SECONDS
                or result["mixed_iterations"] < 3
            ):
                raise RuntimeError(f"incomplete stability workload: {failures}")
            final_boot = vm.run("cat /proc/sys/kernel/random/boot_id", timeout=15)
            if final_boot.exit_code or final_boot.stdout.strip() != result["guest_boot_id"]:
                raise RuntimeError("guest identity changed during worker replacement")
            time.sleep(SAMPLE_SECONDS)
            with lock:
                final = measure()
            result["final_idle"] = final
            for field, allowance in ALLOWANCES.items():
                if growth_exceeded(samples, field, allowance):
                    raise RuntimeError("post-warmup growth: " + field)
            check_quiescent_growth(result["warm_idle"], result["replacement"]["before_idle"])
            check_quiescent_growth(result["recovered_idle"], final)
            assert result.get("replacement") and len(samples) >= 60
            result["status"] = "PASS"
        except BaseException as exc:
            result["status"] = "FAIL"
            result["failure"] = str(exc)
            raise
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=7)
            try:
                guest(
                    vm,
                    "import os, pathlib, signal\n"
                    "p=pathlib.Path('/tmp/sbx-task19/ingress.pid')\n"
                    "if p.exists(): os.kill(int(p.read_text()),signal.SIGTERM)",
                    20,
                )
            except Exception as exc:
                result["cleanup_failure"] = str(exc)
                result["status"] = "FAIL"
            if failures or (thread is not None and thread.is_alive()):
                result["status"] = "FAIL"
            with (args.artifacts / "result.json").open("w") as stream:
                json.dump(
                    result | {"samples": len(samples), "monitor_failures": failures},
                    stream,
                    indent=2,
                )
            if result["status"] != "PASS" and sys.exc_info()[0] is None:
                raise RuntimeError("stability cleanup or monitoring failed; see result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
