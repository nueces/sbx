#!/usr/bin/env python3

import hashlib
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sbx.image.kernel_inputs import KERNEL_INPUTS, fetch, raw_url  # noqa: E402

BRANCHES = {"CelestoAI/SmolVM": "main", "moby/moby": "master"}
LEGAL_FILES = {
    "CelestoAI/SmolVM": ("LICENSE", "NOTICE"),
    "moby/moby": ("LICENSE", "NOTICE"),
}


def _optional_fetch(url: str, fetcher=fetch) -> bytes | None:
    try:
        return fetcher(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _latest_commit(repository: str, path: str, fetcher=fetch) -> str:
    query = urllib.parse.urlencode({"path": path, "per_page": 1})
    data = json.loads(fetcher(f"https://api.github.com/repos/{repository}/commits?{query}"))
    return data[0]["sha"]


def check_updates(fetcher=fetch) -> int:
    changed = False
    for name, (repository, _commit, path, expected) in KERNEL_INPUTS.items():
        latest = fetcher(raw_url(repository, BRANCHES[repository], path))
        digest = hashlib.sha256(latest).hexdigest()
        if digest != expected:
            changed = True
            latest_commit = _latest_commit(repository, path, fetcher)
            print(f"{name}: update available")
            print(f"  commit: {latest_commit}")
            print(f"  sha256: {digest}")

    for repository, paths in LEGAL_FILES.items():
        pinned_commit = next(
            source[1] for source in KERNEL_INPUTS.values() if source[0] == repository
        )
        for path in paths:
            pinned = _optional_fetch(raw_url(repository, pinned_commit, path), fetcher)
            latest = _optional_fetch(raw_url(repository, BRANCHES[repository], path), fetcher)
            if pinned != latest:
                changed = True
                print(f"{repository}/{path}: licensing review required")

    if not changed:
        print("Image build inputs are current.")
    return int(changed)


if __name__ == "__main__":
    raise SystemExit(check_updates())
