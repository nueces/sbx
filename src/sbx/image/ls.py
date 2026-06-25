from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _image_cache_dir() -> Path:
    return Path.home() / ".smolvm" / "images"


def _manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((path / "smolvm-image.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("kernel"), str) or not isinstance(data.get("rootfs"), str):
        return None
    return data


def _features(value: object) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _image_row(path: Path) -> dict[str, Any] | None:
    manifest = _manifest(path)
    if manifest is None:
        return None
    sbx = manifest.get("sbx", {})
    if not isinstance(sbx, dict):
        sbx = {}
    return {
        "name": str(manifest.get("name") or path.name),
        "path": str(path),
        "agent": str(sbx.get("agent") or "-"),
        "features": _features(sbx.get("features")),
        "kernel": manifest["kernel"],
        "rootfs": manifest["rootfs"],
    }


def _images(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    root = cache_dir or _image_cache_dir()
    if not root.is_dir():
        return []
    rows = [_image_row(path) for path in sorted(root.iterdir()) if path.is_dir()]
    return [row for row in rows if row is not None]


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = ("NAME", "AGENT", "FEATURES", "KERNEL", "PATH")
    table = [
        (
            row["name"],
            row["agent"],
            ",".join(row["features"]) or "-",
            row["kernel"],
            row["path"],
        )
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for row in table:
        widths = [max(width, len(str(cell))) for width, cell in zip(widths, row, strict=True)]
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    for row in table:
        print(fmt.format(*row))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def main_from_args(args: argparse.Namespace) -> int:
    rows = _images()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print_table(rows)
    return 0
