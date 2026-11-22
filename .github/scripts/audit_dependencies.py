#!/usr/bin/env python3

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TORNADO_ADVISORIES = (
    "GHSA-5cjw-f85j-cv6r",
    "GHSA-r4j5-9xpp-3985",
    "GHSA-pcg2-g6cj-q92c",
    "GHSA-pw9p-v7r6-gx4p",
)


def main() -> int:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    command = ["uv", "audit", "--locked"]
    if re.fullmatch(r"\d+\.\d+\.\d+\.dev\d+", version):
        packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        tornado_versions = {item["version"] for item in packages if item["name"] == "tornado"}
        # shortcut: dev-only 6.5.10 risk; remove when mitmproxy accepts fixed Tornado.
        if tornado_versions == {"6.5.10"}:
            print(
                "WARNING: development-only Tornado 6.5.10 accepted risk, not a clean audit: "
                + ", ".join(TORNADO_ADVISORIES),
                file=sys.stderr,
            )
            for advisory in TORNADO_ADVISORIES:
                command.extend(["--ignore", advisory])
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
