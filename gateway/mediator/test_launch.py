from pathlib import Path

import launch
import pytest


def test_command_uses_first_mitmdump_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/opt/mitm/bin/mitmdump")

    assert launch.command(Path("/state"))[0] == "/opt/mitm/bin/mitmdump"


def test_command_resolves_selected_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "tool" / "mitmdump"
    target.parent.mkdir()
    target.touch()
    link = tmp_path / "bin" / "mitmdump"
    link.parent.mkdir()
    link.symlink_to(target)
    monkeypatch.setattr(launch.shutil, "which", lambda name: str(link))

    assert launch.command(Path("/state"))[0] == str(target)


def test_command_enables_private_web_without_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/opt/mitm/bin/mitmdump")
    argv = launch.command(Path("/state"), mitmweb=True)
    assert argv[1].endswith("sbx_gateway_web.py")
    assert "web_host=127.0.0.1" in argv
    assert "web_port=0" in argv
    assert "web_open_browser=false" in argv


def test_command_requires_mitmdump_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="mitmdump is missing from PATH"):
        launch.command(Path("/state"))
