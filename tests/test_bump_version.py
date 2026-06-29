import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "bump_version", Path(__file__).parents[1] / ".github" / "scripts" / "bump_version.py"
)
assert spec and spec.loader
bump_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump_version)


def test_bump_version_updates_project_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src/sbx").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        'name = "sbx"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "src/sbx/__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        'name = "other"\nversion = "9.9.9"\n\nname = "sbx"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)

    bump_version.bump("1.2.3")

    assert 'version = "1.2.3"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "1.2.3"' in (tmp_path / "src/sbx/__init__.py").read_text(
        encoding="utf-8"
    )
    lock = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "other"\nversion = "9.9.9"' in lock
    assert 'name = "sbx"\nversion = "1.2.3"' in lock


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3-rc1"])
def test_bump_version_rejects_non_x_y_z(version: str) -> None:
    with pytest.raises(SystemExit):
        bump_version.bump(version)
