#!/usr/bin/env python3
"""Build a local Debian rootfs/kernel image for SmolVM.

This builds a Docker-capable Debian userspace and kernel from pinned inputs,
packs the userspace into rootfs.ext4, and prints the resulting paths.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from smolvm.images.builder import DockerRootfsBuilder, ImageBuilder

from sbx.image import kernel_inputs

RESOURCE_PACKAGE = "sbx.image.resources"
DEFAULT_BASE_CONTAINERFILE = Path("Containers") / "Debian" / "Base.Containerfile"
DEFAULT_AGENT_CONTAINERFILE = Path("Containers") / "Agents" / "Pi.Containerfile"
DEFAULT_DOCKER_CONTAINERFILE = Path("Containers") / "Debian" / "fragments" / "Docker.Containerfile"
DEFAULT_DOCKER_KERNEL_FRAGMENT = Path("kernel") / "docker.config.fragment"
DEFAULT_KERNEL_BUILDER_DOCKERFILE = Path("Containers") / "Build" / "Kernel.Containerfile"
KERNEL_NAME = "vmlinux.bin"
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"


@contextmanager
def _packaged_resources() -> Iterator[Path]:
    with as_file(files(RESOURCE_PACKAGE)) as root:
        yield root


def _compose_containerfiles(
    base_containerfile: Path,
    docker_containerfile: Path,
    agent_containerfile: Path,
    output: Path,
) -> None:
    base_containerfile = base_containerfile.expanduser().resolve()
    agent_containerfile = agent_containerfile.expanduser().resolve()
    if not base_containerfile.is_file():
        raise FileNotFoundError(f"Base Containerfile not found: {base_containerfile}")
    if not agent_containerfile.is_file():
        raise FileNotFoundError(f"Agent Containerfile not found: {agent_containerfile}")

    docker_containerfile = docker_containerfile.expanduser().resolve()
    if not docker_containerfile.is_file():
        raise FileNotFoundError(f"Docker Containerfile not found: {docker_containerfile}")

    content = base_containerfile.read_text(encoding="utf-8").rstrip()
    content += "\n\n# ---- Docker layer ----\n"
    content += docker_containerfile.read_text(encoding="utf-8").lstrip().rstrip()
    content += "\n\n# ---- Agent/tooling layer ----\n"
    content += agent_containerfile.read_text(encoding="utf-8").lstrip()
    output.write_text(content, encoding="utf-8")


class SbxImageBuilder(ImageBuilder):
    def _default_init_script(self) -> str:
        # ponytail: protected SmolVM hook; replace with public boot hooks when upstream exists.
        return self._base_init_script(
            custom_commands="""
# sbx: start rootless Docker at boot when the image provides it.
if [ -x /usr/local/bin/sbx-start-rootless-docker ]; then
    /usr/local/bin/sbx-start-rootless-docker >/var/log/sbx-rootless-docker.log 2>&1 &
fi
"""
        )


def _ssh_rootfs_dockerfile(base_image: str) -> str:
    return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    iproute2 \\
    curl \\
    bash \\
    ca-certificates \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /etc/ssh/ssh_host_* && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY init /init
RUN chmod +x /init
"""


def _build_rootfs(
    *, builder: SbxImageBuilder, name: str, rootfs_size_mb: int, base_image: str, arch: str
) -> Path:
    image_dir = builder.cache_dir / name
    rootfs_path = image_dir / "rootfs.ext4"
    fingerprint_path = image_dir / ".fingerprint"
    dockerfile = _ssh_rootfs_dockerfile(base_image)
    init_script = builder._default_init_script()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "arch": arch,
                "base_image": base_image,
                "dockerfile": dockerfile,
                "init": init_script,
                "rootfs_size_mb": rootfs_size_mb,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if (
        rootfs_path.is_file()
        and fingerprint_path.is_file()
        and fingerprint_path.read_text(encoding="utf-8").strip() == fingerprint
    ):
        return rootfs_path

    if not builder.check_docker():
        raise builder.docker_requirement_error()

    image_dir.mkdir(parents=True, exist_ok=True)
    temporary_rootfs = image_dir / ".rootfs.tmp.ext4"
    temporary_rootfs.unlink(missing_ok=True)
    rootfs_builder = DockerRootfsBuilder(
        name=name,
        dockerfile=dockerfile,
        rootfs_size_mb=rootfs_size_mb,
        cache_dir=builder.cache_dir,
    )
    try:
        # ponytail: private rootfs-only seam; remove when SmolVM exposes a public equivalent.
        rootfs_builder._build_rootfs(
            helper=builder,
            rootfs_path=temporary_rootfs,
            docker_platform=f"linux/{arch}",
            context_files={"init": init_script.encode()},
            docker_tag=f"sbx-rootfs-{fingerprint[:16]}",
        )
        temporary_rootfs.replace(rootfs_path)
        fingerprint_path.write_text(fingerprint, encoding="utf-8")
    finally:
        temporary_rootfs.unlink(missing_ok=True)
    return rootfs_path


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
            f"{work_dir}:/work:z",
            "-w",
            "/work",
            tag,
            *command,
        ],
        check=check,
    )


def _raise_if_linux_digest_mismatch(work_dir: Path) -> None:
    version = (work_dir / "linux.version").read_text(encoding="utf-8").strip()
    checksum = (work_dir / "linux.sha256").read_text(encoding="utf-8").split()[0]
    tarball = work_dir / f"linux-{version}.tar.xz"
    if not tarball.is_file():
        return
    with tarball.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != checksum:
        raise RuntimeError(
            f"SHA-256 mismatch for https://cdn.kernel.org/pub/linux/kernel/v6.x/{tarball.name}\n"
            f"  expected: {checksum}\n"
            f"  actual:   {actual}\n"
            "update linux.version and linux.sha256 together after reviewing the new source"
        )


def _build_docker_kernel(*, image_dir: Path, arch: str, resources_dir: Path | None = None) -> Path:
    if resources_dir is None:
        with _packaged_resources() as packaged:
            return _build_docker_kernel(image_dir=image_dir, arch=arch, resources_dir=packaged)

    docker_fragment = resources_dir / DEFAULT_DOCKER_KERNEL_FRAGMENT
    builder_dockerfile = resources_dir / DEFAULT_KERNEL_BUILDER_DOCKERFILE
    for path in (docker_fragment, builder_dockerfile):
        if not path.is_file():
            raise FileNotFoundError(f"Kernel build resource not found: {path}")

    with tempfile.TemporaryDirectory(prefix="sbx-docker-kernel-") as tmp:
        work_dir = Path(tmp)
        for name, source in kernel_inputs.KERNEL_INPUTS.items():
            kernel_inputs.download_verified(source, work_dir / name)
        config = work_dir / "config.fragment"
        config.write_text(
            config.read_text(encoding="utf-8").rstrip()
            + "\n\n# ---- sbx Docker additions ----\n"
            + docker_fragment.read_text(encoding="utf-8").lstrip(),
            encoding="utf-8",
        )
        builder_tag = _build_kernel_builder_image(builder_dockerfile, resources_dir)
        out_dir = work_dir / "out"
        try:
            try:
                _docker_run_kernel_builder(
                    builder_tag,
                    work_dir,
                    ["env", "OUT_DIR=/work/out", "bash", "/work/build.sh"],
                    check=True,
                )
            except subprocess.CalledProcessError:
                _raise_if_linux_digest_mismatch(work_dir)
                raise
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
        kernel_path = image_dir / KERNEL_NAME
        temporary_kernel = image_dir / f".{KERNEL_NAME}.tmp"
        try:
            shutil.copy2(out_dir / f"vmlinux-{arch}.image", temporary_kernel)
            temporary_kernel.replace(kernel_path)
        finally:
            temporary_kernel.unlink(missing_ok=True)
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
        default="sbx",
        help="Image cache name under ~/.smolvm/images/ (default: sbx).",
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
        "--rootfs-size-mb",
        type=int,
        default=20480,
        help="Size of the ext4 root filesystem in MiB (default: 20480).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Image cache directory (default: ~/.smolvm/images).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text summary.",
    )


def main_from_args(args: argparse.Namespace) -> int:
    builder = SbxImageBuilder(cache_dir=args.cache_dir.expanduser() if args.cache_dir else None)
    base_image = args.base_image
    arch = "amd64" if builder._host_arch_key() == "x86_64" else "arm64"

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
                    selected_docker_containerfile = resources_dir / DEFAULT_DOCKER_CONTAINERFILE
                    _compose_containerfiles(
                        selected_base_containerfile,
                        selected_docker_containerfile,
                        selected_agent_containerfile,
                        combined_containerfile,
                    )
                    base_image = _build_containerfile_base_image(
                        args.base_image, combined_containerfile, context_dir=resources_dir
                    )

            rootfs_path = _build_rootfs(
                builder=builder,
                name=args.name,
                rootfs_size_mb=args.rootfs_size_mb,
                base_image=base_image,
                arch=arch,
            )
            kernel_path = _build_docker_kernel(
                image_dir=rootfs_path.parent, arch=arch, resources_dir=resources_dir
            )
    except Exception as exc:  # noqa: BLE001 - keep the CLI error friendly.
        print(f"sbx image build: failed to build image: {exc}", file=sys.stderr)
        return 1

    manifest_path = rootfs_path.parent / "smolvm-image.json"
    manifest = {
        "name": args.name,
        "kernel": kernel_path.name,
        "rootfs": rootfs_path.name,
        "boot_args": BOOT_ARGS,
        "sbx": {
            "agent": "pi",
            "features": [] if args.containerfile is not None else ["docker"],
            "launch_command": "pi",
            "run_user": "agent",
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
        "docker_containerfile": None if uses_custom else str(selected_docker_containerfile),
        "kernel_path": str(kernel_path),
        "rootfs_path": str(rootfs_path),
        "manifest_path": str(manifest_path),
        "boot_args": BOOT_ARGS,
        "rootfs_size_mb": args.rootfs_size_mb,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Built curated sbx image:")
    print(f"  name:        {payload['name']}")
    print(f"  base image:  {payload['base_image']}")
    print(f"  kernel:      {payload['kernel_path']}")
    print(f"  rootfs:      {payload['rootfs_path']}")
    print(f"  manifest:    {payload['manifest_path']}")
    print(f"  boot args:   {payload['boot_args']}")
    print()
    print("sbx config:")
    print("  [sbx]")
    print(f"  image = {str(manifest_path.parent)!r}")
    print("  run_user = 'agent'")
    image_path = str(manifest_path.parent)
    if manifest_path.parent.is_relative_to(Path.home()):
        image_path = f"~/{manifest_path.parent.relative_to(Path.home())}"
    print()
    print("Next:")
    print("  sbx run the-quest \\")
    print(f"    --image {shlex.quote(image_path)} \\")
    print("    --project-path . \\")
    print("    --writable-mounts \\")
    print("    --write-config")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the curated local image for sbx.")
    add_arguments(parser)
    return main_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
