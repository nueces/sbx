import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audit_dependencies", Path(__file__).parents[1] / ".github/scripts/audit_dependencies.py"
)
assert spec and spec.loader
audit_dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_dependencies)

ADVISORIES = {
    "GHSA-5cjw-f85j-cv6r",
    "GHSA-r4j5-9xpp-3985",
    "GHSA-pcg2-g6cj-q92c",
    "GHSA-pw9p-v7r6-gx4p",
}


@pytest.mark.parametrize(
    ("version", "tornado", "excepted"),
    [
        ("0.2.6.dev0", "6.5.10", True),
        ("0.2.6.dev12", "6.5.10", True),
        ("0.2.6", "6.5.10", False),
        ("1.0.0", "6.5.10", False),
        ("0.2.6rc1", "6.5.10", False),
        ("0.2.6.dev0+local", "6.5.10", False),
        ("0.2.6.dev0", "6.6.1", False),
        ("0.2.6.dev0", "6.5.9", False),
    ],
)
@pytest.mark.parametrize("result", [0, 1, 2])
def test_audit_exceptions_are_narrow_and_failures_propagate(
    tmp_path, monkeypatch, capsys, version, tornado, excepted, result
):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (tmp_path / "uv.lock").write_text(f'[[package]]\nname = "tornado"\nversion = "{tornado}"\n')
    monkeypatch.setattr(audit_dependencies, "ROOT", tmp_path)
    calls = []

    def run(command, *, cwd, check):
        assert cwd == tmp_path and check is False
        calls.append(command)
        return subprocess.CompletedProcess(command, result)

    monkeypatch.setattr(audit_dependencies.subprocess, "run", run)
    assert audit_dependencies.main() == result
    assert len(calls) == 1
    command = calls[0]
    assert command[:3] == ["uv", "audit", "--locked"]
    warning = capsys.readouterr().err
    if excepted:
        assert command[3::2] == ["--ignore"] * 4
        assert set(command[4::2]) == ADVISORIES
        assert "WARNING" in warning and "accepted risk" in warning
        assert all(advisory in warning for advisory in ADVISORIES)
    else:
        assert command == ["uv", "audit", "--locked"]
        assert warning == ""


@pytest.mark.parametrize("versions", [[], ["6.5.10", "6.6.1"]])
def test_missing_or_mixed_tornado_versions_receive_no_exception(tmp_path, monkeypatch, versions):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.6.dev0"\n')
    packages = [{"name": "other", "version": "1.0"}]
    packages += [{"name": "tornado", "version": version} for version in versions]
    lock = "".join(
        f'[[package]]\nname = "{item["name"]}"\nversion = "{item["version"]}"\n'
        for item in packages
    )
    (tmp_path / "uv.lock").write_text(lock)
    monkeypatch.setattr(audit_dependencies, "ROOT", tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(audit_dependencies.subprocess, "run", run)
    assert audit_dependencies.main() == 1
    assert calls == [["uv", "audit", "--locked"]]


def test_invalid_metadata_cannot_skip_the_audit_successfully(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("not valid TOML")
    monkeypatch.setattr(audit_dependencies, "ROOT", tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid metadata must fail before invoking uv")

    monkeypatch.setattr(audit_dependencies.subprocess, "run", unexpected)
    with pytest.raises(ValueError):
        audit_dependencies.main()


def test_ci_runs_the_version_gated_audit():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    assert "python .github/scripts/audit_dependencies.py" in workflow
    assert "continue-on-error" not in workflow
