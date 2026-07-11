#!/usr/bin/env python3

import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:\.dev\d+)?\Z")
ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"expected one version match in {path.relative_to(ROOT)}")
    path.write_text(new, encoding="utf-8")


def bump(version: str) -> None:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit("version must match x.y.z or x.y.z.devN")

    replace_once(ROOT / "pyproject.toml", r'^version = "[^"]+"$', f'version = "{version}"')
    replace_once(
        ROOT / "src/sbx/__init__.py",
        r'^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
    )
    replace_once(
        ROOT / "uv.lock",
        r'(?m)(^name = "sbx"\nversion = ")[^"]+("$)',
        rf"\g<1>{version}\2",
    )
    if ".dev" not in version:
        replace_once(
            ROOT / "README.md",
            r"git\+https://github\.com/nueces/sbx\.git@v[^\s]+",
            f"git+https://github.com/nueces/sbx.git@v{version}",
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: bump_version.py x.y.z[.devN]")
    bump(sys.argv[1])
