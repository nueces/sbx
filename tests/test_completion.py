import subprocess

import pytest

from sbx.completion import NETWORK_COMMANDS, SUPPORTED_SHELLS, completion_script


def test_completion_scripts_include_sbx_command() -> None:
    for shell in SUPPORTED_SHELLS:
        script = completion_script(shell)
        assert "sbx" in script
        assert "run" in script
        assert "completion" in script


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_network_ui_completion_is_static_and_includes_help(shell) -> None:
    script = completion_script(shell)
    assert "ui" in NETWORK_COMMANDS
    if shell == "bash":
        assert '"$subcmd" == "ui"' in script
    elif shell == "zsh":
        assert '$words[3] == "ui"' in script
    else:
        assert "-a ui" in script
        assert "__fish_seen_subcommand_from ui' -l help" in script


def test_bash_network_ui_completion(tmp_path) -> None:
    script = tmp_path / "completion.bash"
    script.write_text(completion_script("bash"))
    probe = f"""
source {str(script)!r}
COMP_WORDS=(sbx network u)
COMP_CWORD=2
COMP_LINE='sbx network u'
COMP_POINT=${{#COMP_LINE}}
_sbx_complete
printf '%s\\n' "${{COMPREPLY[@]}}"
COMP_WORDS=(sbx network ui --)
COMP_CWORD=3
COMP_LINE='sbx network ui --'
COMP_POINT=${{#COMP_LINE}}
_sbx_complete
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    completed = subprocess.run(["bash", "-c", probe], check=True, text=True, capture_output=True)
    assert completed.stdout.splitlines() == ["ui", "--help"]


def test_completion_rejects_unknown_shell() -> None:
    with pytest.raises(ValueError, match="unsupported shell"):
        completion_script("powershell")


def test_bash_completion_includes_agent_and_runtime_install_values() -> None:
    script = completion_script("bash")
    assert "--agent" in script
    assert "pi claude codex" in script
    assert "--runtime-install" in script
    assert "pi specify claude opencode codex" in script


def test_completion_scripts_include_all_command_option_groups() -> None:
    for shell in SUPPORTED_SHELLS:
        script = completion_script(shell)
        for value in (
            "forward",
            "auth-port",
            "close-auth-port",
            "status",
            "build",
            "force",
            "host-port",
            "guest-port",
            "replace",
            "rootfs-size-mb",
            "running",
            "json",
            "name",
        ):
            assert value in script
        assert "build-debian" not in script


def test_bash_completion_completes_redirection_targets(tmp_path) -> None:
    target = tmp_path / "sbx.sh"
    target.touch()
    script = tmp_path / "completion.bash"
    script.write_text(completion_script("bash"))
    probe = f"""
source {str(script)!r}
COMP_WORDS=(sbx completion bash)
COMP_CWORD=3
COMP_LINE={f"sbx completion bash > {str(target)[:-1]}"!r}
COMP_POINT=${{#COMP_LINE}}
_sbx_complete
printf '%s\n' "${{COMPREPLY[@]}}"
"""

    completed = subprocess.run(["bash", "-lc", probe], check=True, text=True, capture_output=True)

    assert str(target) in completed.stdout


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
    assert "complete -o default -F _sbx_complete sbx" in capsys.readouterr().out
