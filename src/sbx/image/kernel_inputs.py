from __future__ import annotations

import hashlib
import urllib.request
from collections.abc import Callable
from pathlib import Path

SMOLVM_COMMIT = "20e1fdf72c2139622eb32ab21f288c7290bba7bf"
MOBY_COMMIT = "b780867932842071ca38968da81ec52d8b70f0bc"

# output name: (repository, commit, upstream path, expected SHA-256)
KERNEL_INPUTS = {
    "build.sh": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/build.sh",
        "798ee1af08740bcd0348570151ae29b2ccbae9674443ad7c564de6f281bc8f75",
    ),
    "config.fragment": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/config.fragment",
        "9df5020319525f1df6ada9ddc29eb334ed625edc1c08f347877eafe4e0d58215",
    ),
    "config.amd64.fragment": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/config.amd64.fragment",
        "46649f51667d1ede8ae436c9cb8ce5c44cfa45c814847760445faa1aa596a3d7",
    ),
    "config.arm64.fragment": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/config.arm64.fragment",
        "2f9b9db74a4581d77c8a794a826a6dc688358cf2e8d119e2dc8de8fad89a6b3e",
    ),
    "linux.version": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/linux.version",
        "f692c6ce637cb4bdf81a87c7818080995b9c888f842ae952aadc6cae632d24c1",
    ),
    "linux.sha256": (
        "CelestoAI/SmolVM",
        SMOLVM_COMMIT,
        "kernel/microvm/linux.sha256",
        "30781e950c485491db11e248b785fa5e98d91f536889361b72f795d4d5c7d41f",
    ),
    "check-config.sh": (
        "moby/moby",
        MOBY_COMMIT,
        "contrib/check-config.sh",
        "fda4343e9b50c47896653ca774ccbe9614bfcdb60f080d2b6277baf27efc0a71",
    ),
}


def raw_url(repository: str, revision: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repository}/{revision}/{path}"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed URLs.
        return response.read()


def download_verified(
    source: tuple[str, str, str, str],
    destination: Path,
    *,
    fetcher: Callable[[str], bytes] = fetch,
) -> None:
    repository, commit, path, expected = source
    url = raw_url(repository, commit, path)
    data = fetcher(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {url}\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n"
            "update the pinned commit and checksum together after reviewing the file"
        )
    destination.write_bytes(data)
