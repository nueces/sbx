from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build-debian-image.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sbx_build_debian_image", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_smolvm_images(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeBuilder:
        def __init__(self, cache_dir: Path | None = None) -> None:
            self.cache_dir = cache_dir

        def _host_arch_key(self) -> str:
            return "x86_64"

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


def test_build_debian_image_omits_sdk_sketch_by_default(
    fake_smolvm_images: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key), "--name", "image"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Built Debian SmolVM image:" in out
    assert "sbx config:" in out
    assert "SDK usage sketch:" not in out
    assert "from smolvm import SmolVM" not in out


def test_build_debian_image_prints_sdk_sketch_when_requested(
    fake_smolvm_images: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
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
    module = _load_script()
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
    module = _load_script()
    image = tmp_path / "image"
    image.mkdir()

    rc = module.main(["--print-sdk-sketch", str(image)])

    assert rc == 2
    assert "manifest not found" in capsys.readouterr().err


def test_print_sdk_sketch_reports_invalid_manifest_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
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
    module = _load_script()
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
    module = _load_script()
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
    module = _load_script()
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
    module = _load_script()

    rc = module.main([])

    assert rc == 2
    assert "no SSH public key found" in capsys.readouterr().err


def test_build_debian_image_import_error_returns_127(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
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
    module = _load_script()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key), "--name", "image", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "image"
    assert payload["rootfs_path"].endswith("rootfs.ext4")
    assert payload["manifest_path"].endswith("smolvm-image.json")


def test_build_debian_image_builder_failure_returns_1(
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

    module = _load_script()
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 fake", encoding="utf-8")

    rc = module.main(["--ssh-public-key", str(key)])

    assert rc == 1
    assert "failed to build image: boom" in capsys.readouterr().err


def test_compose_containerfiles_combines_base_and_agent(tmp_path: Path) -> None:
    module = _load_script()
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


def test_build_containerfile_base_image_runs_expected_docker_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_script()
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
    module = _load_script()

    with pytest.raises(FileNotFoundError, match="Containerfile not found"):
        module._build_containerfile_base_image("debian:stable-slim", tmp_path / "missing")
