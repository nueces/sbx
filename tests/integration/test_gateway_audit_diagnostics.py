"""Real supervisor-protocol process; no QEMU or production fault endpoint."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sbx import gateway, gateway_supervisor

PROGRAM = r"""
import os, pathlib, socket, sys
from sbx import gateway, gateway_supervisor
root=pathlib.Path(sys.argv[1])
config=gateway.parse_config({'network': {}})
controller=gateway.GatewayController(config,'audit-test',root=root/'state',generation='generation')
controller.directory=gateway.create_state(config,'audit-test','generation',root=root/'state')
controller._audit_stream=controller._open_log('audit.log')
read,write=os.pipe()
controller._route_audit(os.fdopen(read,'rb',buffering=0),controller._audit_stream)
os.write(write,b'not-json-secret-payload\n');os.close(write)
for thread in controller._log_threads:
 thread.join(3);assert not thread.is_alive()
assert controller._audit_failure_detail is not None
server=socket.socket(socket.AF_UNIX);server.bind(str(root/'control'));server.listen(2)
def unavailable(*args,**kwargs):raise gateway.GatewayStateError('filesystem unavailable')
gateway.read_state=unavailable
(root/'ready').write_text('ready')
gateway_supervisor._serve(controller,server)
"""


def test_supervisor_protocol_retains_failure_without_stderr_or_state_reads():
    assert os.getuid() != 0
    with tempfile.TemporaryDirectory(prefix="gw-audit-", dir="/tmp") as raw:
        root = Path(raw)
        process = subprocess.Popen(
            [sys.executable, "-c", PROGRAM, raw],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not (root / "ready").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            for operation in ("status", "diagnostics"):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(root / "control"))
                    gateway_supervisor.send_frame(
                        client,
                        {"version": 1, "generation": "generation", "operation": operation},
                    )
                    response = gateway_supervisor.recv_frame(client, 3)
                assert response["status"] == "failed"
                assert response["last_failure"]["code"] == "audit_failed"
                if operation == "diagnostics":
                    detail = response["audit_failure"]
                    assert detail["operation"] == "decode" and detail["reason"] == "framing"
                    assert "secret-payload" not in json.dumps(response)
            saved = gateway.read_audit_failure(root / "state/audit-test/generation")
            assert saved == detail
            assert (root / "state/audit-test/generation/state.json").is_file()
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
