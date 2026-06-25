#!/usr/bin/env python3
"""Build a local Debian rootfs/kernel image for SmolVM.

This is a convenience wrapper around SmolVM's ImageBuilder. It can use Docker
to build a Debian userspace from a Containerfile, packs it into rootfs.ext4,
downloads/resolves a SmolVM-compatible kernel, and prints the resulting paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_CONTAINERFILE = PROJECT_ROOT / "Containers" / "Debian" / "Base.Containerfile"
DEFAULT_AGENT_CONTAINERFILE = PROJECT_ROOT / "Containers" / "Agents" / "Pi.Containerfile"
DEFAULT_DOCKER_CONTAINERFILE = (
    PROJECT_ROOT / "Containers" / "Debian" / "fragments" / "Docker.Containerfile"
)
DEFAULT_DOCKER_KERNEL_FRAGMENT = PROJECT_ROOT / "kernel" / "docker.config.fragment"
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


def _docker_builder_class(image_builder_class: type) -> type:
    class SbxDockerImageBuilder(image_builder_class):  # type: ignore[misc, valid-type]
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

    return SbxDockerImageBuilder


def _download(url: str, output: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned HTTPS URL.
        output.write_bytes(response.read())


def _build_docker_kernel(*, image_dir: Path, arch: str) -> Path:
    if not DEFAULT_DOCKER_KERNEL_FRAGMENT.is_file():
        raise FileNotFoundError(
            f"Docker kernel fragment not found: {DEFAULT_DOCKER_KERNEL_FRAGMENT}"
        )

    with tempfile.TemporaryDirectory(prefix="sbx-docker-kernel-") as tmp:
        work_dir = Path(tmp)
        for name in SMOLVM_KERNEL_FILES:
            _download(f"{SMOLVM_RAW_BASE}/kernel/microvm/{name}", work_dir / name)
        config = work_dir / "config.fragment"
        config.write_text(
            config.read_text(encoding="utf-8").rstrip()
            + "\n\n# ---- sbx Docker additions ----\n"
            + DEFAULT_DOCKER_KERNEL_FRAGMENT.read_text(encoding="utf-8").lstrip(),
            encoding="utf-8",
        )
        (work_dir / "build.sh").chmod(0o755)
        out_dir = work_dir / "out"
        subprocess.run(
            ["bash", str(work_dir / "build.sh")],
            check=True,
            env={**os.environ, "OUT_DIR": str(out_dir)},
        )
        check_config = work_dir / "check-config.sh"
        _download(
            "https://raw.githubusercontent.com/moby/moby/master/contrib/check-config.sh",
            check_config,
        )
        subprocess.run(
            ["sh", str(check_config), str(out_dir / f"vmlinux-{arch}.config")],
            check=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "") + ":/usr/sbin:/sbin"},
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


def _print_sdk_sketch(*, kernel_path: Path, rootfs_path: Path, boot_args: str) -> None:
    print("SDK usage sketch:")
    print("  from pathlib import Path")
    print("  from smolvm import SmolVM, VMConfig")
    print("  config = VMConfig(")
    print("      vm_id='debian-test',")
    print(f"      kernel_path=Path({str(kernel_path)!r}),")
    print(f"      rootfs_path=Path({str(rootfs_path)!r}),")
    print(f"      boot_args={boot_args!r},")
    print("      backend='qemu',")
    print("      ssh_capable=True,")
    print("  )")
    print("  with SmolVM(config) as vm:")
    print("      vm.start()")
    print("      vm.wait_for_ssh()")
    print("      print(vm.run('cat /etc/debian_version'))")


def _print_existing_sdk_sketch(image_dir: Path) -> int:
    image_dir = image_dir.expanduser().resolve()
    manifest_path = image_dir / "smolvm-image.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        kernel_name = manifest["kernel"]
        rootfs_name = manifest["rootfs"]
    except FileNotFoundError:
        print(f"build-debian-image: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"build-debian-image: invalid image manifest {manifest_path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(kernel_name, str) or not isinstance(rootfs_name, str):
        print(
            f"build-debian-image: invalid image manifest {manifest_path}: "
            "kernel and rootfs must be strings",
            file=sys.stderr,
        )
        return 2
    boot_args = manifest.get(
        "boot_args", "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"
    )
    if not isinstance(boot_args, str):
        print(
            f"build-debian-image: invalid image manifest {manifest_path}: "
            "boot_args must be a string",
            file=sys.stderr,
        )
        return 2
    _print_sdk_sketch(
        kernel_path=image_dir / kernel_name,
        rootfs_path=image_dir / rootfs_name,
        boot_args=boot_args,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a local Debian SSH-ready image for SmolVM.")
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
        default=DEFAULT_BASE_CONTAINERFILE,
        help=f"Base OS Containerfile (default: {DEFAULT_BASE_CONTAINERFILE}).",
    )
    parser.add_argument(
        "--agent-containerfile",
        type=Path,
        default=DEFAULT_AGENT_CONTAINERFILE,
        help=f"Agent/tooling Containerfile (default: {DEFAULT_AGENT_CONTAINERFILE}).",
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
    parser.add_argument(
        "--sdk-sketch",
        action="store_true",
        help="Include a SmolVM SDK usage sketch in the text summary.",
    )
    parser.add_argument(
        "--print-sdk-sketch",
        type=Path,
        metavar="IMAGE_DIR",
        help="Print a SmolVM SDK usage sketch for an existing local image directory and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.print_sdk_sketch is not None:
        return _print_existing_sdk_sketch(args.print_sdk_sketch)

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

    try:
        from smolvm.images.builder import ImageBuilder
        from smolvm.images.published import BASE_KERNELS
    except ImportError as exc:
        print(
            "build-debian-image: smolvm is not installed. Run this from the sbx env, "
            "for example: uv run scripts/build-debian-image.py",
            file=sys.stderr,
        )
        print(f"Original import error: {exc}", file=sys.stderr)
        return 127

    BuilderClass = _docker_builder_class(ImageBuilder) if args.with_docker else ImageBuilder
    builder = BuilderClass(cache_dir=args.cache_dir.expanduser() if args.cache_dir else None)
    base_image = args.base_image
    arch = "amd64" if builder._host_arch_key() == "x86_64" else "arm64"
    kernel_url = args.kernel_url
    if kernel_url is None:
        kernel_url = BASE_KERNELS[arch].image_url

    try:
        if args.containerfile is not None:
            base_image = _build_containerfile_base_image(args.base_image, args.containerfile)
        else:
            with tempfile.TemporaryDirectory(prefix="sbx-combined-containerfile-") as tmp:
                combined_containerfile = Path(tmp) / "Containerfile"
                _compose_containerfiles(
                    args.base_containerfile,
                    args.agent_containerfile,
                    combined_containerfile,
                    DEFAULT_DOCKER_CONTAINERFILE if args.with_docker else None,
                )
                base_image = _build_containerfile_base_image(
                    args.base_image, combined_containerfile, context_dir=PROJECT_ROOT
                )

        kernel_path, rootfs_path = builder.build_debian_ssh_key(
            ssh_public_key=args.ssh_public_key.expanduser(),
            name=args.name,
            rootfs_size_mb=args.rootfs_size_mb,
            base_image=base_image,
            kernel_url=kernel_url,
        )
        if args.with_docker:
            kernel_path = _build_docker_kernel(image_dir=rootfs_path.parent, arch=arch)
    except Exception as exc:  # noqa: BLE001 - keep this standalone script friendly.
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
        "base_containerfile": None if uses_custom else str(args.base_containerfile.expanduser()),
        "agent_containerfile": None if uses_custom else str(args.agent_containerfile.expanduser()),
        "with_docker": args.with_docker,
        "docker_containerfile": str(DEFAULT_DOCKER_CONTAINERFILE) if args.with_docker else None,
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
    if args.sdk_sketch:
        print()
        _print_sdk_sketch(kernel_path=kernel_path, rootfs_path=rootfs_path, boot_args=boot_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
