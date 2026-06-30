#!/usr/bin/env python3

import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"\d+\.\d+\.\d+\Z")
MARKER_RE = re.compile(
    r"(<!-- sbx-release-version -->)v\d+\.\d+\.\d+(<!-- /sbx-release-version -->)"
)


def bump(version: str, path: Path) -> None:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit("version must match x.y.z")

    text = path.read_text(encoding="utf-8")
    new, count = MARKER_RE.subn(rf"\g<1>v{version}\2", text)
    if count != 2:
        raise SystemExit("expected two sbx-release-version markers")
    path.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: bump_webpage_version.py x.y.z path/to/index.html")
    bump(sys.argv[1], Path(sys.argv[2]))
