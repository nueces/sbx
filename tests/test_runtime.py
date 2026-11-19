import subprocess

import pytest

from sbx import runtime
from sbx.runtime import ConfigError, env_boolean, smolvm_env


def test_env_boolean_accepts_common_truthy_values() -> None:
    for value in ("1", "true", "TRUE", " yes ", "on"):
        assert env_boolean(value) is True


def test_env_boolean_rejects_falsey_values() -> None:
    for value in (None, "", "0", "false", "no", "off"):
        assert env_boolean(value) is False


def test_smolvm_env_preserves_custom_env_and_disables_notice_by_default() -> None:
    env = smolvm_env({"HOME": "/tmp/home"})

    assert env["HOME"] == "/tmp/home"
    assert env["SMOLVM_DISABLE_VERSION_CHECK"] == "1"


def test_smolvm_env_opt_in_leaves_version_check_enabled() -> None:
    env = smolvm_env({"SBX_SMOLVM_VERSION_NOTICES": "yes"})

    assert "SMOLVM_DISABLE_VERSION_CHECK" not in env


def test_process_timeouts_are_classified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("stuck", kwargs["timeout"])

    monkeypatch.setattr(runtime.subprocess, "run", timed_out)

    assert runtime.run(["stuck"], timeout=0.01) == 124
    assert "timed out after 0.01 seconds" in capsys.readouterr().err
    with pytest.raises(ConfigError, match="timed out after 0.01 seconds"):
        runtime.run_capture(["stuck"], timeout=0.01)
