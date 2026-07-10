from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest


def _load_module() -> types.ModuleType:
    return importlib.reload(importlib.import_module("sbx.image.build_debian"))


@pytest.fixture
def fake_smolvm_images(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeBuilder:
        def __init__(self, cache_dir: Path | None = None) -> None:
            self.cache_dir = cache_dir

        def _host_arch_key(self) -> str:
            return "x86_64"

        def _base_init_script(self, custom_commands: str = "") -> str:
            return f"init:{custom_commands}"

        def _default_init_script(self) -> str:
            return self._base_init_script()

        def build_debian_ssh_key(
            self,
            *,
            ssh_public_key: Path,
            name: str,
            rootfs_size_mb: int,
            base_image: str,
            kernel_url: str,
        ) -> tuple[Path, Path]:
            del ssh_public_key, rootfs_size_mb, base_image, kernel_url
            image_dir = tmp_path / name
            image_dir.mkdir()
            kernel = image_dir / "vmlinux.bin"
            rootfs = image_dir / "rootfs.ext4"
            kernel.write_text("kernel", encoding="utf-8")
            rootfs.write_text("rootfs", encoding="utf-8")
            (image_dir / "init-script.txt").write_text(
                self._default_init_script(), encoding="utf-8"
            )
            return kernel, rootfs

    smolvm_mod = types.ModuleType("smolvm")
    images_mod = types.ModuleType("smolvm.images")
    builder_mod = types.ModuleType("smolvm.images.builder")
    published_mod = types.ModuleType("smolvm.images.published")
    builder_mod.ImageBuilder = FakeBuilder
    published_mod.BASE_KERNELS = {"amd64": types.SimpleNamespace(image_url="https://kernel")}

    monkeypatch.setitem(sys.modules, "smolvm", smolvm_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images", images_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images.builder", builder_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images.published", published_mod)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=""),
    )


def test_packaged_pi_containerfile_uses_npm_global_install() -> None:
    module = _load_module()

    with module._packaged_resources() as resources:
        content = (resources / module.DEFAULT_AGENT_CONTAINERFILE).read_text(encoding="utf-8")

    assert 'ENV PATH="/home/agent/.local/bin:/home/agent/.nodejs/bin:${PATH}"' in content
    assert "npm install --global --ignore-scripts @earendil-works/pi-coding-agent" in content
    assert 'export PATH="$HOME/.local/bin:$HOME/.nodejs/bin:$PATH"' in content


def test_build_debian_image_omits_sdk_sketch_by_default(
    fake_smolvm_images: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key), "--name", "image"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Built Debian SmolVM image:" in out
    assert "sbx config:" in out
    assert "disk_size" not in out
    assert "run_user = 'agent'" in out
    assert "SDK usage sketch:" not in out
    assert "from smolvm import SmolVM" not in out
    manifest = json.loads((tmp_path / "image" / "smolvm-image.json").read_text())
    assert manifest["sbx"]["features"] == []


def test_build_debian_image_prints_sdk_sketch_when_requested(
    fake_smolvm_images: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key), "--name", "image", "--sdk-sketch"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "SDK usage sketch:" in out
    assert "from smolvm import SmolVM, VMConfig" in out


def test_build_debian_image_prints_sdk_sketch_for_existing_image_without_rebuild(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()
    (image / "smolvm-image.json").write_text(
        '{"kernel":"vmlinux.bin","rootfs":"rootfs.ext4","boot_args":"boot args"}',
        encoding="utf-8",
    )

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "SDK usage sketch:" in out
    assert f"kernel_path=Path('{image / 'vmlinux.bin'}')" in out
    assert f"rootfs_path=Path('{image / 'rootfs.ext4'}')" in out
    assert "boot_args='boot args'" in out


def test_print_sdk_sketch_reports_missing_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 2
    assert "manifest not found" in capsys.readouterr().err


def test_print_sdk_sketch_reports_invalid_manifest_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()
    (image / "smolvm-image.json").write_text("not json", encoding="utf-8")

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 2
    assert "invalid image manifest" in capsys.readouterr().err


def test_print_sdk_sketch_rejects_non_string_manifest_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()
    (image / "smolvm-image.json").write_text(
        '{"kernel":123,"rootfs":"rootfs.ext4"}',
        encoding="utf-8",
    )

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 2
    assert "kernel and rootfs must be strings" in capsys.readouterr().err


def test_print_sdk_sketch_rejects_non_string_boot_args(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()
    (image / "smolvm-image.json").write_text(
        '{"kernel":"vmlinux.bin","rootfs":"rootfs.ext4","boot_args":123}',
        encoding="utf-8",
    )

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 2
    assert "boot_args must be a string" in capsys.readouterr().err


def test_print_sdk_sketch_uses_default_boot_args(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    image = tmp_path / "image"
    image.mkdir()
    (image / "smolvm-image.json").write_text(
        '{"kernel":"vmlinux.bin","rootfs":"rootfs.ext4"}',
        encoding="utf-8",
    )

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 0
    assert "console=ttyS0 reboot=k panic=1" in capsys.readouterr().out


def test_build_debian_image_missing_ssh_key_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    module = _load_module()

    rc = module.main([])

    assert rc == 2
    assert "no SSH public key found" in capsys.readouterr().err


def test_build_debian_image_rejects_with_docker_and_custom_containerfile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    containerfile = tmp_path / "Containerfile"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")
    containerfile.write_text("FROM debian\n", encoding="utf-8")

    rc = module.main(
        [
            "--ssh-public-key",
            str(key),
            "--containerfile",
            str(containerfile),
            "--with-docker",
        ]
    )

    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_build_debian_image_import_error_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "smolvm.images.builder", None)

    rc = module.main(["--ssh-public-key", str(key)])

    assert rc == 127
    assert "smolvm is not installed" in capsys.readouterr().err


def test_build_debian_image_json_output(
    fake_smolvm_images: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key), "--name", "image", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "image"
    assert payload["rootfs_size_mb"] == 20480
    assert payload["rootfs_path"].endswith("rootfs.ext4")
    assert payload["manifest_path"].endswith("smolvm-image.json")


def test_build_debian_builder_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingBuilder:
        def __init__(self, cache_dir: Path | None = None) -> None:
            del cache_dir

        def _host_arch_key(self) -> str:
            return "x86_64"

        def build_debian_ssh_key(self, **kwargs: object) -> tuple[Path, Path]:
            del kwargs
            raise RuntimeError("boom")

    smolvm_mod = types.ModuleType("smolvm")
    images_mod = types.ModuleType("smolvm.images")
    builder_mod = types.ModuleType("smolvm.images.builder")
    published_mod = types.ModuleType("smolvm.images.published")
    builder_mod.ImageBuilder = FailingBuilder
    published_mod.BASE_KERNELS = {"amd64": types.SimpleNamespace(image_url="https://kernel")}
    monkeypatch.setitem(sys.modules, "smolvm", smolvm_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images", images_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images.builder", builder_mod)
    monkeypatch.setitem(sys.modules, "smolvm.images.published", published_mod)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=""),
    )

    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key)])

    assert rc == 1
    assert "failed to build image: boom" in capsys.readouterr().err


def test_compose_containerfiles_combines_base_and_agent(tmp_path: Path) -> None:
    module = _load_module()
    base = tmp_path / "Base.Containerfile"
    agent = tmp_path / "Pi.Containerfile"
    output = tmp_path / "Combined.Containerfile"
    base.write_text("FROM debian AS sbx-base\nRUN echo base\n", encoding="utf-8")
    agent.write_text("FROM sbx-base AS sbx-final\nRUN echo pi\n", encoding="utf-8")

    module._compose_containerfiles(base, agent, output)

    combined = output.read_text(encoding="utf-8")
    assert "FROM debian AS sbx-base" in combined
    assert "# ---- Agent/tooling layer ----" in combined
    assert "FROM sbx-base AS sbx-final" in combined


def test_build_debian_image_with_docker_inserts_fragment_and_uses_docker_kernel(
    fake_smolvm_images: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module()
    key = tmp_path / "id_ed25519.pub"
    base = tmp_path / "Base.Containerfile"
    docker = tmp_path / "Docker.Containerfile"
    agent = tmp_path / "Pi.Containerfile"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")
    base.write_text("FROM debian AS sbx-base\nRUN echo base\n", encoding="utf-8")
    docker.write_text("USER root\nRUN echo docker\nUSER agent\n", encoding="utf-8")
    agent.write_text("FROM sbx-base AS sbx-final\nRUN echo pi\n", encoding="utf-8")
    combined = ""
    docker_kernel_args: dict[str, object] = {}

    def fake_build_base_image(
        base_image: str, containerfile: Path, *, context_dir: Path | None = None
    ) -> str:
        nonlocal combined
        del base_image, context_dir
        combined = containerfile.read_text(encoding="utf-8")
        return "sbx-debian-base:docker"

    def fake_build_docker_kernel(
        *, image_dir: Path, arch: str, resources_dir: Path | None = None
    ) -> Path:
        docker_kernel_args.update(
            {"image_dir": image_dir, "arch": arch, "resources_dir": resources_dir}
        )
        kernel = image_dir / "vmlinux-docker.bin"
        kernel.write_text("docker kernel", encoding="utf-8")
        return kernel

    monkeypatch.setattr(module, "DEFAULT_DOCKER_CONTAINERFILE", docker)
    monkeypatch.setattr(module, "_build_containerfile_base_image", fake_build_base_image)
    monkeypatch.setattr(module, "_build_docker_kernel", fake_build_docker_kernel)

    rc = module.main(
        [
            "--ssh-public-key",
            str(key),
            "--base-containerfile",
            str(base),
            "--agent-containerfile",
            str(agent),
            "--with-docker",
            "--name",
            "docker-image",
        ]
    )

    assert rc == 0
    assert combined.index("RUN echo base") < combined.index("RUN echo docker")
    assert combined.index("RUN echo docker") < combined.index("RUN echo pi")
    assert "# ---- Docker layer ----" in combined
    assert docker_kernel_args["arch"] == "amd64"
    manifest = json.loads((tmp_path / "docker-image" / "smolvm-image.json").read_text())
    assert manifest["kernel"] == "vmlinux-docker.bin"
    assert manifest["sbx"]["features"] == ["docker"]
    assert manifest["sbx"]["launch_command"] == "pi"
    init_script = (tmp_path / "docker-image" / "init-script.txt").read_text(encoding="utf-8")
    assert "sbx-start-rootless-docker" in init_script


def test_build_docker_kernel_downloads_inputs_appends_fragment_and_copies_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module()
    fragment = tmp_path / "docker.config.fragment"
    builder_file = tmp_path / "Containers" / "Build" / "Kernel.Containerfile"
    fragment.write_text("CONFIG_VETH=y\n", encoding="utf-8")
    builder_file.parent.mkdir(parents=True)
    builder_file.write_text("FROM debian\n", encoding="utf-8")
    downloads: list[str] = []
    runs: list[list[str]] = []

    def fake_download(url: str, output: Path) -> None:
        downloads.append(url)
        if output.name == "config.fragment":
            output.write_text("CONFIG_BASE=y\n", encoding="utf-8")
        else:
            output.write_text("file\n", encoding="utf-8")

    def fake_run(command: list[str], *, check: bool) -> None:
        runs.append(command)
        if command[:3] == ["docker", "run", "--rm"] and "bash" in command:
            assert check is True
            work_dir = Path(command[command.index("-v") + 1].split(":", 1)[0])
            out_dir = work_dir / "out"
            out_dir.mkdir()
            (out_dir / "vmlinux-amd64.image").write_text("kernel", encoding="utf-8")
            (out_dir / "vmlinux-amd64.config").write_text("config", encoding="utf-8")
            config = (work_dir / "config.fragment").read_text(encoding="utf-8")
            assert "CONFIG_BASE=y" in config
            assert "CONFIG_VETH=y" in config

    monkeypatch.setattr(module, "DEFAULT_DOCKER_KERNEL_FRAGMENT", fragment)
    monkeypatch.setattr(module, "DEFAULT_KERNEL_BUILDER_DOCKERFILE", builder_file)
    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    kernel = module._build_docker_kernel(image_dir=tmp_path, arch="amd64")

    assert kernel == tmp_path / "vmlinux-docker.bin"
    assert kernel.read_text(encoding="utf-8") == "kernel"
    assert any(url.endswith("/kernel/microvm/build.sh") for url in downloads)
    assert any(url.endswith("/contrib/check-config.sh") for url in downloads)
    assert runs[0][:3] == ["docker", "build", "-f"]
    assert any("OUT_DIR=/work/out" in command for command in runs)
    assert any("/work/check-config.sh" in command for command in runs)
    assert runs[-1][-4:] == [
        "chown",
        "-R",
        f"{module.os.getuid()}:{module.os.getgid()}",
        "/work",
    ]


def test_build_containerfile_base_image_runs_expected_docker_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM debian:stable-slim\nUSER agent\n", encoding="utf-8")
    commands: list[list[str]] = []
    wrapper_text: list[str] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        commands.append(command)
        if command[:3] == ["docker", "build", "-t"]:
            wrapper_text.append((Path(command[-1]) / "Dockerfile").read_text(encoding="utf-8"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    root_tag = module._build_containerfile_base_image("debian:stable-slim", containerfile)

    assert root_tag.startswith("sbx-debian-base:")
    assert commands[0][:4] == ["docker", "build", "--build-arg", "BASE_IMAGE=debian:stable-slim"]
    assert commands[0][-3:-1] == [
        "-t",
        root_tag.replace("sbx-debian-base:", "sbx-debian-base-user:"),
    ]
    assert commands[1][:4] == ["docker", "build", "-t", root_tag]
    user_tag = root_tag.replace("sbx-debian-base:", "sbx-debian-base-user:")
    assert wrapper_text == [f"FROM {user_tag}\nUSER root\n"]


def test_build_containerfile_base_image_missing_file(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(FileNotFoundError, match="Containerfile not found"):
        module._build_containerfile_base_image("debian:stable-slim", tmp_path / "missing")
