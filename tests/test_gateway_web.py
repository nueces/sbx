from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from sbx import gateway, gateway_supervisor
from sbx.runtime import ConfigError


def test_web_boolean_digest_and_supervisor_transport() -> None:
    config = gateway.parse_config({"network": {}})
    assert config is not None and config.mitmweb is False
    web = gateway.parse_config({"network": {"mitmweb": True}})
    assert web == replace(config, mitmweb=True)
    assert web.digest("vm", "generation") != config.digest("vm", "generation")
    assert gateway_supervisor.config_from_payload(gateway_supervisor.config_payload(web)) == web
    for invalid in ("true", 1, 0, None, []):
        with pytest.raises(ConfigError, match="must be a boolean"):
            gateway.parse_config({"network": {"mitmweb": invalid}})


def test_web_diagnostics_never_read_authentication(tmp_path: Path) -> None:
    config = gateway.parse_config({"network": {"mitmweb": True}})
    assert config is not None
    directory = gateway.create_state(config, "vm", "generation", root=tmp_path)
    gateway._atomic_json(directory, "web.json", {"port": 12345})
    gateway._atomic_json(directory, "web-auth.json", {"url": "private-ui-token"})
    state = gateway.read_state(directory)
    rendered = "\n".join(gateway._web_diagnostic_lines("vm", state, root=tmp_path))
    assert "127.0.0.1:12345" in rendered and "web-auth.json" in rendered
    assert "private-ui-token" not in rendered
    gateway._atomic_json(directory, "web.json", {"port": "invalid"})
    assert gateway._web_diagnostic_lines("vm", state, root=tmp_path) == []
    (directory / "web.json").unlink()
    assert gateway._web_diagnostic_lines("vm", state, root=tmp_path) == []


def test_web_launcher_preserves_generation_and_audit_transport(tmp_path: Path) -> None:
    config = gateway.parse_config({"network": {"mitmweb": True}})
    assert config is not None
    controller = gateway.GatewayController(config, "vm", root=tmp_path)
    controller.directory = tmp_path
    controller._audit_write_fd = 123
    controller._audit_ack_read_fd = 124
    argv = controller._mediator_command(3, 4, 5, 6)
    assert "--mitmweb" in argv
    assert argv[argv.index("--audit-fd") + 1] == "123"
    assert argv[argv.index("--audit-ack-fd") + 1] == "124"
    assert argv[argv.index("--generation") + 1] == controller.generation
    controller.config = replace(config, mitmweb=False)
    assert "--mitmweb" not in controller._mediator_command(3, 4, 5, 6)
    read_fd, write_fd = os.pipe()
    ack_read, ack_write = os.pipe()
    try:
        controller._audit_write_fd = write_fd
        controller._audit_ack_read_fd = ack_read
        controller._discard_data_plane()
        assert controller._audit_write_fd is None
        assert controller._audit_ack_read_fd is None
        with pytest.raises(OSError):
            os.fstat(ack_read)
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)
        os.close(ack_write)


def test_mediator_runtime_copies_match() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("launch.py", "hardening.py", "sbx_gateway_mediator.py", "sbx_gateway_web.py"):
        assert (root / "gateway/mediator" / name).read_bytes() == (
            root / "src/sbx/gateway_runtime/mediator" / name
        ).read_bytes()


def test_audit_event_is_not_truncated_at_log_limit(tmp_path: Path) -> None:
    config = gateway.parse_config({"network": {}})
    assert config is not None
    controller = gateway.GatewayController(config, "vm", root=tmp_path)
    controller.directory = gateway.create_state(config, "vm", controller.generation, root=tmp_path)
    stream = controller._open_log("audit.log")
    try:
        controller._log_bytes = gateway.MAX_LOG_BYTES - 2
        with pytest.raises(gateway.GatewayLifecycleError, match="audit storage limit"):
            controller._emit_log(stream, b'{"version":1}', required=True)
        assert (controller.directory / "audit.log").read_bytes() == b""
        controller._log_bytes = 0
        controller._placeholders = {"token": '"'}
        controller._emit_log(stream, json.dumps({"path": '"'}).encode(), required=True)
        assert json.loads((controller.directory / "audit.log").read_text()) == {
            "path": "[redacted]"
        }
    finally:
        stream.close()
