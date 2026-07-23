import hashlib
import importlib.util
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_image_build_inputs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_image_build_inputs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup(module, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    repository = "example/project"
    commit = "a" * 40
    path = "build.sh"
    monkeypatch.setattr(
        module,
        "KERNEL_INPUTS",
        {"build.sh": (repository, commit, path, hashlib.sha256(b"same").hexdigest())},
    )
    monkeypatch.setattr(module, "BRANCHES", {repository: "main"})
    monkeypatch.setattr(module, "LEGAL_FILES", {repository: ("LICENSE", "NOTICE")})
    return repository, commit, path


def test_update_checker_reports_current(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    module = _load_script()
    repository, commit, path = _setup(module, monkeypatch)

    def fake_fetch(url: str) -> bytes:
        if url.endswith("/NOTICE"):
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        if url.endswith("/LICENSE"):
            return b"license"
        assert url.endswith(f"/{path}")
        return b"same"

    assert module.check_updates(fake_fetch) == 0
    assert capsys.readouterr().out == "Image build inputs are current.\n"


def test_update_checker_reports_source_change(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    module = _load_script()
    repository, commit, path = _setup(module, monkeypatch)

    def fake_fetch(url: str) -> bytes:
        if "/commits?" in url:
            return b'[{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]'
        if url.endswith("/NOTICE"):
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        if url.endswith("/LICENSE"):
            return b"license"
        if f"/{commit}/" in url:
            return b"same"
        return b"new"

    assert module.check_updates(fake_fetch) == 1
    output = capsys.readouterr().out
    assert "build.sh: update available" in output
    assert "commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in output
    assert f"sha256: {hashlib.sha256(b'new').hexdigest()}" in output


def test_update_checker_reports_license_change(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    module = _load_script()
    repository, commit, path = _setup(module, monkeypatch)

    def fake_fetch(url: str) -> bytes:
        if url.endswith("/NOTICE"):
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        if url.endswith("/LICENSE"):
            return b"old" if f"/{commit}/" in url else b"new"
        assert url.endswith(f"/{path}")
        return b"same"

    assert module.check_updates(fake_fetch) == 1
    assert f"{repository}/LICENSE: licensing review required" in capsys.readouterr().out
