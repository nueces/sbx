from __future__ import annotations

import pytest

from sbx.completion import SUPPORTED_SHELLS, completion_script


def test_completion_scripts_include_sbx_command() -> None:
    for shell in SUPPORTED_SHELLS:
        script = completion_script(shell)
        assert "sbx" in script
        assert "run" in script
        assert "completion" in script


def test_completion_rejects_unknown_shell() -> None:
    with pytest.raises(ValueError, match="unsupported shell"):
        completion_script("powershell")


def test_bash_completion_includes_agent_values() -> None:
    script = completion_script("bash")
    assert "--agent" in script
    assert "pi claude codex" in script


def test_completion_command_does_not_load_project_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sbx import cli

    monkeypatch.setattr(
        cli, "load_config", lambda path: (_ for _ in ()).throw(cli.ConfigError("bad config"))
    )

    rc = cli.main(["completion", "bash"])

    assert rc == 0
    assert "complete -F _sbx_complete sbx" in capsys.readouterr().out
