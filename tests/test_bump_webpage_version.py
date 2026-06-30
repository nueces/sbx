import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "bump_webpage_version",
    Path(__file__).parents[1] / ".github" / "scripts" / "bump_webpage_version.py",
)
assert spec and spec.loader
bump_webpage_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump_webpage_version)


def test_bump_webpage_version_updates_marked_versions(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text(
        "git+https://github.com/nueces/sbx.git@"
        "<!-- sbx-release-version -->v0.2.0<!-- /sbx-release-version -->\n"
        "<strong>sbx</strong> "
        "<!-- sbx-release-version -->v0.2.0<!-- /sbx-release-version --><br />\n",
        encoding="utf-8",
    )

    bump_webpage_version.bump("0.3.0", page)

    text = page.read_text(encoding="utf-8")
    assert text.count("v0.3.0") == 2
    assert "v0.2.0" not in text


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3-rc1"])
def test_bump_webpage_version_rejects_non_x_y_z(version: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        bump_webpage_version.bump(version, tmp_path / "index.html")


def test_bump_webpage_version_requires_two_markers(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text(
        "<!-- sbx-release-version -->v0.2.0<!-- /sbx-release-version -->\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="expected two"):
        bump_webpage_version.bump("0.3.0", page)
