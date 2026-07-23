#!/usr/bin/env python3
"""Build a local Debian rootfs/kernel image for SmolVM.

This is a convenience wrapper around SmolVM's ImageBuilder. It can use Docker
to build a Debian userspace from a Containerfile, packs it into rootfs.ext4,
downloads/resolves a SmolVM-compatible kernel, and prints the resulting paths.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from smolvm.images.builder import ImageBuilder
from smolvm.images.published import BASE_KERNELS

RESOURCE_PACKAGE = "sbx.image.resources"
DEFAULT_BASE_CONTAINERFILE = Path("Containers") / "Debian" / "Base.Containerfile"
DEFAULT_AGENT_CONTAINERFILE = Path("Containers") / "Agents" / "Pi.Containerfile"
DEFAULT_DOCKER_CONTAINERFILE = Path("Containers") / "Debian" / "fragments" / "Docker.Containerfile"
DEFAULT_DOCKER_KERNEL_FRAGMENT = Path("kernel") / "docker.config.fragment"
DEFAULT_KERNEL_BUILDER_DOCKERFILE = Path("Containers") / "Build" / "Kernel.Containerfile"
SMOLVM_KERNEL_REF = "20e1fdf72c2139622eb32ab21f288c7290bba7bf"
SMOLVM_RAW_BASE = f"https://raw.githubusercontent.com/CelestoAI/SmolVM/{SMOLVM_KERNEL_REF}"
SMOLVM_KERNEL_FILES = (
    "build.sh",
    "config.fragment",
    "config.amd64.fragment",
    "config.arm64.fragment",
    "linux.version",
    "linux.sha256",
)
DOCKER_KERNEL_NAME = "vmlinux-docker.bin"


@contextmanager
def _packaged_resources() -> Iterator[Path]:
    with as_file(files(RESOURCE_PACKAGE)) as root:
        yield root


def _default_public_key() -> Path | None:
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        candidate = ssh_dir / name
        if candidate.is_file():
            return candidate
    return None


def _compose_containerfiles(
    base_containerfile: Path,
    agent_containerfile: Path,
    output: Path,
    docker_containerfile: Path | None = None,
) -> None:
    base_containerfile = base_containerfile.expanduser().resolve()
    agent_containerfile = agent_containerfile.expanduser().resolve()
    if not base_containerfile.is_file():
        raise FileNotFoundError(f"Base Containerfile not found: {base_containerfile}")
    if not agent_containerfile.is_file():
        raise FileNotFoundError(f"Agent Containerfile not found: {agent_containerfile}")

    content = base_containerfile.read_text(encoding="utf-8").rstrip()
    if docker_containerfile is not None:
        docker_containerfile = docker_containerfile.expanduser().resolve()
        if not docker_containerfile.is_file():
            raise FileNotFoundError(f"Docker Containerfile not found: {docker_containerfile}")
        content += "\n\n# ---- Docker layer ----\n"
        content += docker_containerfile.read_text(encoding="utf-8").lstrip().rstrip()
    content += "\n\n# ---- Agent/tooling layer ----\n"
    content += agent_containerfile.read_text(encoding="utf-8").lstrip()
    output.write_text(content, encoding="utf-8")


class SbxDockerImageBuilder(ImageBuilder):
    def _default_init_script(self) -> str:
        # ponytail: protected SmolVM hook; replace with public boot hooks when upstream exists.
        return self._base_init_script(
            custom_commands="""
# sbx: start rootless Docker at boot for Docker-capable images.
if [ -x /usr/local/bin/sbx-start-rootless-docker ]; then
    /usr/local/bin/sbx-start-rootless-docker >/var/log/sbx-rootless-docker.log 2>&1 &
fi
"""
        )


def _download(url: str, output: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned HTTPS URL.
        output.write_bytes(response.read())


def _build_kernel_builder_image(dockerfile: Path, context_dir: Path) -> str:
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Kernel builder Dockerfile not found: {dockerfile}")
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    tag = f"sbx-kernel-builder:{digest}"
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "-t",
            tag,
            str(context_dir),
        ],
        check=True,
    )
    return tag


def _docker_run_kernel_builder(
    tag: str, work_dir: Path, command: list[str], *, check: bool
) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work_dir}:/work",
            "-w",
            "/work",
            tag,
            *command,
        ],
        check=check,
    )


def _build_docker_kernel(*, image_dir: Path, arch: str, resources_dir: Path | None = None) -> Path:
    if resources_dir is None:
        with _packaged_resources() as packaged:
            return _build_docker_kernel(image_dir=image_dir, arch=arch, resources_dir=packaged)

    docker_fragment = resources_dir / DEFAULT_DOCKER_KERNEL_FRAGMENT
    builder_dockerfile = resources_dir / DEFAULT_KERNEL_BUILDER_DOCKERFILE
    if not docker_fragment.is_file():
        raise FileNotFoundError(f"Docker kernel fragment not found: {docker_fragment}")

    builder_tag = _build_kernel_builder_image(builder_dockerfile, resources_dir)
    with tempfile.TemporaryDirectory(prefix="sbx-docker-kernel-") as tmp:
        work_dir = Path(tmp)
        for name in SMOLVM_KERNEL_FILES:
            _download(f"{SMOLVM_RAW_BASE}/kernel/microvm/{name}", work_dir / name)
        config = work_dir / "config.fragment"
        config.write_text(
            config.read_text(encoding="utf-8").rstrip()
            + "\n\n# ---- sbx Docker additions ----\n"
            + docker_fragment.read_text(encoding="utf-8").lstrip(),
            encoding="utf-8",
        )
        (work_dir / "build.sh").chmod(0o755)
        out_dir = work_dir / "out"
        check_config = work_dir / "check-config.sh"
        _download(
            "https://raw.githubusercontent.com/moby/moby/master/contrib/check-config.sh",
            check_config,
        )
        try:
            _docker_run_kernel_builder(
                builder_tag,
                work_dir,
                ["env", "OUT_DIR=/work/out", "bash", "/work/build.sh"],
                check=True,
            )
            _docker_run_kernel_builder(
                builder_tag,
                work_dir,
                [
                    "env",
                    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "sh",
                    "/work/check-config.sh",
                    f"/work/out/vmlinux-{arch}.config",
                ],
                check=True,
            )
        finally:
            _docker_run_kernel_builder(
                builder_tag,
                work_dir,
                ["chown", "-R", f"{os.getuid()}:{os.getgid()}", "/work"],
                check=False,
            )
        kernel_path = image_dir / DOCKER_KERNEL_NAME
        shutil.copy2(out_dir / f"vmlinux-{arch}.image", kernel_path)
        return kernel_path


def _build_containerfile_base_image(
    base_image: str, containerfile: Path, *, context_dir: Path | None = None
) -> str:
    """Build a Containerfile and return the local image tag to use as the SmolVM base."""
    containerfile = containerfile.expanduser().resolve()
    if not containerfile.is_file():
        raise FileNotFoundError(f"Containerfile not found: {containerfile}")
    context_dir = (context_dir or containerfile.parent).expanduser().resolve()

    digest_input = base_image.encode() + b"\0" + containerfile.read_bytes()
    digest = hashlib.sha256(digest_input).hexdigest()[:12]
    user_tag = f"sbx-debian-base-user:{digest}"
    root_tag = f"sbx-debian-base:{digest}"
    subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "-f",
            str(containerfile),
            "-t",
            user_tag,
            str(context_dir),
        ],
        check=True,
    )

    # SmolVM's Debian builder derives another image from this base and runs
    # apt/ssh setup commands. A user-provided Containerfile may end with
    # `USER agent`; reset the default user to root in a tiny wrapper image so
    # those builder steps still run with the expected privileges.
    with tempfile.TemporaryDirectory(prefix="sbx-debian-root-base-") as tmp:
        context = Path(tmp)
        (context / "Dockerfile").write_text(f"FROM {user_tag}\nUSER root\n", encoding="utf-8")
        subprocess.run(["docker", "build", "-t", root_tag, str(context)], check=True)

    return root_tag


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        default="debian-sbx",
        help="Image cache name under ~/.smolvm/images/ (default: debian-sbx).",
    )
    parser.add_argument(
        "--base-image",
        default="debian:stable-slim",
        help="Docker base image to build from (default: debian:stable-slim).",
    )
    parser.add_argument(
        "--containerfile",
        "--dockerfile",
        dest="containerfile",
        type=Path,
        default=None,
        help=(
            "Optional fully composed Containerfile to build first and use as the Debian base. "
            "Its parent directory is used as the build context, and BASE_IMAGE "
            "is passed as a build arg. (--dockerfile is accepted as a compatibility alias.)"
        ),
    )
    parser.add_argument(
        "--base-containerfile",
        type=Path,
        default=None,
        help="Base OS Containerfile (default: packaged Debian base).",
    )
    parser.add_argument(
        "--agent-containerfile",
        type=Path,
        default=None,
        help="Agent/tooling Containerfile (default: packaged Pi agent).",
    )
    parser.add_argument(
        "--with-docker",
        action="store_true",
        help="Include Docker packages and build a Docker-capable local kernel.",
    )
    parser.add_argument(
        "--rootfs-size-mb",
        type=int,
        default=20480,
        help="Size of the ext4 root filesystem in MiB (default: 20480).",
    )
    parser.add_argument(
        "--ssh-public-key",
        type=Path,
        default=_default_public_key(),
        help="SSH public key to authorize for root login (default: first key in ~/.ssh).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Image cache directory (default: ~/.smolvm/images).",
    )
    parser.add_argument(
        "--kernel-url",
        default=None,
        help="Optional SmolVM-compatible kernel URL override.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text summary.",
    )


def main_from_args(args: argparse.Namespace) -> int:
    if args.with_docker and args.containerfile is not None:
        print(
            "build-debian-image: --with-docker cannot be combined with --containerfile; "
            "add Docker to the custom Containerfile instead",
            file=sys.stderr,
        )
        return 2

    if args.ssh_public_key is None:
        print(
            "build-debian-image: no SSH public key found; pass --ssh-public-key PATH",
            file=sys.stderr,
        )
        return 2

    BuilderClass = SbxDockerImageBuilder if args.with_docker else ImageBuilder
    builder = BuilderClass(cache_dir=args.cache_dir.expanduser() if args.cache_dir else None)
    base_image = args.base_image
    arch = "amd64" if builder._host_arch_key() == "x86_64" else "arm64"
    kernel_url = args.kernel_url
    if kernel_url is None:
        kernel_url = BASE_KERNELS[arch].image_url

    selected_base_containerfile: Path | None = None
    selected_agent_containerfile: Path | None = None
    selected_docker_containerfile: Path | None = None

    try:
        with _packaged_resources() as resources_dir:
            if args.containerfile is not None:
                base_image = _build_containerfile_base_image(args.base_image, args.containerfile)
            else:
                with tempfile.TemporaryDirectory(prefix="sbx-combined-containerfile-") as tmp:
                    combined_containerfile = Path(tmp) / "Containerfile"
                    selected_base_containerfile = args.base_containerfile or (
                        resources_dir / DEFAULT_BASE_CONTAINERFILE
                    )
                    selected_agent_containerfile = args.agent_containerfile or (
                        resources_dir / DEFAULT_AGENT_CONTAINERFILE
                    )
                    selected_docker_containerfile = (
                        resources_dir / DEFAULT_DOCKER_CONTAINERFILE if args.with_docker else None
                    )
                    _compose_containerfiles(
                        selected_base_containerfile,
                        selected_agent_containerfile,
                        combined_containerfile,
                        selected_docker_containerfile,
                    )
                    base_image = _build_containerfile_base_image(
                        args.base_image, combined_containerfile, context_dir=resources_dir
                    )

            kernel_path, rootfs_path = builder.build_debian_ssh_key(
                ssh_public_key=args.ssh_public_key.expanduser(),
                name=args.name,
                rootfs_size_mb=args.rootfs_size_mb,
                base_image=base_image,
                kernel_url=kernel_url,
            )
            if args.with_docker:
                kernel_path = _build_docker_kernel(
                    image_dir=rootfs_path.parent, arch=arch, resources_dir=resources_dir
                )
    except Exception as exc:  # noqa: BLE001 - keep the CLI error friendly.
        print(f"build-debian-image: failed to build image: {exc}", file=sys.stderr)
        return 1

    boot_args = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
    manifest_path = rootfs_path.parent / "smolvm-image.json"
    manifest = {
        "name": args.name,
        "kernel": kernel_path.name,
        "rootfs": rootfs_path.name,
        "boot_args": boot_args,
        "sbx": {
            "agent": "pi",
            "features": ["docker"] if args.with_docker else [],
            "launch_command": "pi",
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    uses_custom = args.containerfile is not None
    payload = {
        "name": args.name,
        "base_image": base_image,
        "source_base_image": args.base_image,
        "containerfile": str(args.containerfile.expanduser()) if uses_custom else None,
        "base_containerfile": None if uses_custom else str(selected_base_containerfile),
        "agent_containerfile": None if uses_custom else str(selected_agent_containerfile),
        "with_docker": args.with_docker,
        "docker_containerfile": str(selected_docker_containerfile) if args.with_docker else None,
        "kernel_url": kernel_url,
        "kernel_path": str(kernel_path),
        "kernel_source": (
            "Docker-capable local kernel" if args.with_docker else "SmolVM published QEMU kernel"
        ),
        "rootfs_path": str(rootfs_path),
        "manifest_path": str(manifest_path),
        "boot_args": boot_args,
        "rootfs_size_mb": args.rootfs_size_mb,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Built Debian SmolVM image:")
    print(f"  name:        {payload['name']}")
    print(f"  base image:  {payload['base_image']}")
    print(f"  kernel:      {payload['kernel_path']} [source: {payload['kernel_source']}]")
    print(f"  rootfs:      {payload['rootfs_path']}")
    print(f"  manifest:    {payload['manifest_path']}")
    print(f"  boot args:   {payload['boot_args']}")
    print()
    print("sbx config:")
    print("  [sbx]")
    print(f"  image = {str(manifest_path.parent)!r}")
    print("  run_user = 'agent'")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local Debian SSH-ready image for SmolVM.")
    add_arguments(parser)
    return main_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
