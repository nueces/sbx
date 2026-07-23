import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from sbx.image import build_debian, kernel_inputs


class FakeImageBuilder:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or Path.cwd()

    def _host_arch_key(self) -> str:
        return "x86_64"

    def _default_init_script(self) -> str:
        return "#!/bin/sh\n# init without an agent binary\n"

    def check_docker(self) -> bool:
        return True


def _fake_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[str], dict[str, object]]:
    combined: list[str] = []
    calls: dict[str, object] = {}

    class Builder(FakeImageBuilder):
        def __init__(self, cache_dir: Path | None = None) -> None:
            super().__init__(cache_dir or tmp_path)

    def fake_base(
        base_image: str, containerfile: Path, *, context_dir: Path | None = None
    ) -> str:
        del base_image, context_dir
        combined.append(containerfile.read_text(encoding="utf-8"))
        return "sbx-debian-base:test"

    def fake_rootfs(**kwargs: object) -> Path:
        calls["rootfs"] = kwargs
        image_dir = tmp_path / str(kwargs["name"])
        image_dir.mkdir(parents=True, exist_ok=True)
        rootfs = image_dir / "rootfs.ext4"
        rootfs.write_text("rootfs", encoding="utf-8")
        return rootfs

    def fake_kernel(**kwargs: object) -> Path:
        calls["kernel"] = kwargs
        kernel = Path(str(kwargs["image_dir"])) / "vmlinux.bin"
        kernel.write_text("kernel", encoding="utf-8")
        return kernel

    monkeypatch.setattr(build_debian, "SbxImageBuilder", Builder)
    monkeypatch.setattr(build_debian, "_build_containerfile_base_image", fake_base)
    monkeypatch.setattr(build_debian, "_build_rootfs", fake_rootfs)
    monkeypatch.setattr(build_debian, "_build_docker_kernel", fake_kernel)
    return combined, calls


def test_kernel_inputs_use_full_commits_and_sha256() -> None:
    assert len(kernel_inputs.KERNEL_INPUTS) == 7
    for repository, commit, path, digest in kernel_inputs.KERNEL_INPUTS.values():
        assert len(commit) == 40
        assert len(digest) == 64
        url = kernel_inputs.raw_url(repository, commit, path)
        assert commit in url
        assert "/main/" not in url
        assert "/master/" not in url


def test_download_verified_writes_only_matching_content(tmp_path: Path) -> None:
    content = b"reviewed input"
    source = ("example/repo", "a" * 40, "build.sh", hashlib.sha256(content).hexdigest())
    destination = tmp_path / "build.sh"

    kernel_inputs.download_verified(source, destination, fetcher=lambda url: content)

    assert destination.read_bytes() == content


def test_download_verified_rejects_mismatch_before_writing(tmp_path: Path) -> None:
    source = ("example/repo", "a" * 40, "build.sh", "0" * 64)
    destination = tmp_path / "build.sh"

    with pytest.raises(RuntimeError) as error:
        kernel_inputs.download_verified(source, destination, fetcher=lambda url: b"changed")

    message = str(error.value)
    assert "https://raw.githubusercontent.com/example/repo/" in message
    assert "expected: " + "0" * 64 in message
    assert f"actual:   {hashlib.sha256(b'changed').hexdigest()}" in message
    assert not destination.exists()


def test_linux_digest_mismatch_is_actionable(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"expected").hexdigest()
    (tmp_path / "linux.version").write_text("6.12.85\n", encoding="utf-8")
    (tmp_path / "linux.sha256").write_text(
        f"{expected}  linux-6.12.85.tar.xz\n", encoding="utf-8"
    )
    (tmp_path / "linux-6.12.85.tar.xz").write_bytes(b"changed")

    with pytest.raises(RuntimeError) as error:
        build_debian._raise_if_linux_digest_mismatch(tmp_path)

    message = str(error.value)
    assert "linux-6.12.85.tar.xz" in message
    assert f"expected: {expected}" in message
    assert f"actual:   {hashlib.sha256(b'changed').hexdigest()}" in message


def test_packaged_pi_containerfile_uses_npm_global_install() -> None:
    with build_debian._packaged_resources() as resources:
        content = (resources / build_debian.DEFAULT_AGENT_CONTAINERFILE).read_text(
            encoding="utf-8"
        )

    assert 'ENV PATH="/home/agent/.local/bin:/home/agent/.nodejs/bin:${PATH}"' in content
    assert "npm install --global --ignore-scripts @earendil-works/pi-coding-agent" in content


def test_compose_containerfiles_includes_docker_between_base_and_agent(tmp_path: Path) -> None:
    base = tmp_path / "Base.Containerfile"
    docker = tmp_path / "Docker.Containerfile"
    agent = tmp_path / "Pi.Containerfile"
    output = tmp_path / "Combined.Containerfile"
    base.write_text("FROM debian AS sbx-base\nRUN echo base\n", encoding="utf-8")
    docker.write_text("RUN echo docker\n", encoding="utf-8")
    agent.write_text("FROM sbx-base AS sbx-final\nRUN echo pi\n", encoding="utf-8")

    build_debian._compose_containerfiles(base, docker, agent, output)

    combined = output.read_text(encoding="utf-8")
    assert combined.index("RUN echo base") < combined.index("RUN echo docker")
    assert combined.index("RUN echo docker") < combined.index("RUN echo pi")


def test_default_build_always_uses_docker_kernel_and_feature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    combined, calls = _fake_build(monkeypatch, tmp_path)

    rc = build_debian.main([])

    assert rc == 0
    assert "# ---- Docker layer ----" in combined[0]
    rootfs_call = calls["rootfs"]
    assert isinstance(rootfs_call, dict)
    assert rootfs_call["name"] == "sbx"
    assert rootfs_call["rootfs_size_mb"] == 20480
    assert rootfs_call["base_image"] == "sbx-debian-base:test"
    assert rootfs_call["arch"] == "amd64"
    manifest = json.loads((tmp_path / "sbx" / "smolvm-image.json").read_text())
    assert manifest["kernel"] == "vmlinux.bin"
    assert manifest["sbx"]["features"] == ["docker"]
    assert manifest["sbx"]["run_user"] == "agent"
    output = capsys.readouterr().out
    assert "kernel:" in output
    assert "sbx run the-quest" in output
    assert f"--image {tmp_path / 'sbx'}" in output
    assert "--write-config" in output


def test_custom_containerfile_is_conservative_about_docker_feature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    combined, _ = _fake_build(monkeypatch, tmp_path)
    custom = tmp_path / "Containerfile"
    custom.write_text("FROM debian:stable-slim\n", encoding="utf-8")

    rc = build_debian.main(["--containerfile", str(custom), "--name", "custom"])

    assert rc == 0
    assert combined == ["FROM debian:stable-slim\n"]
    manifest = json.loads((tmp_path / "custom" / "smolvm-image.json").read_text())
    assert manifest["kernel"] == "vmlinux.bin"
    assert manifest["sbx"]["features"] == []


def test_removed_build_flags_are_rejected() -> None:
    for option in ("--with-docker", "--kernel-url", "--ssh-public-key"):
        with pytest.raises(SystemExit) as error:
            build_debian.main([option, "value"])
        assert error.value.code == 2


def test_build_debian_image_json_has_no_obsolete_kernel_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_build(monkeypatch, tmp_path)

    rc = build_debian.main(["--name", "image", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kernel_path"].endswith("vmlinux.bin")
    assert "with_docker" not in payload
    assert "kernel_url" not in payload
    assert "kernel_source" not in payload


def test_build_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_build(monkeypatch, tmp_path)
    monkeypatch.setattr(
        build_debian, "_build_rootfs", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert build_debian.main([]) == 1
    assert "failed to build image: boom" in capsys.readouterr().err


def test_build_rootfs_omits_guest_agent_and_reuses_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    class RootfsBuilder:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def _build_rootfs(self, **kwargs: object) -> None:
            calls.append(kwargs)
            Path(str(kwargs["rootfs_path"])).write_text("rootfs", encoding="utf-8")

    builder = FakeImageBuilder(tmp_path)
    monkeypatch.setattr(build_debian, "DockerRootfsBuilder", RootfsBuilder)

    first = build_debian._build_rootfs(
        builder=builder,
        name="image",
        rootfs_size_mb=20,
        base_image="base:test",
        arch="amd64",
    )
    second = build_debian._build_rootfs(
        builder=builder,
        name="image",
        rootfs_size_mb=20,
        base_image="base:test",
        arch="amd64",
    )

    assert first == second == tmp_path / "image" / "rootfs.ext4"
    assert len(calls) == 2
    dockerfile = str(calls[0]["dockerfile"])
    context = calls[1]["context_files"]
    assert "COPY smolvm-guest-agent" not in dockerfile
    assert "smolvm-guest-agent" not in context


def test_build_docker_kernel_uses_only_verified_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs: list[list[str]] = []
    downloads: list[str] = []

    def fake_download(source: tuple[str, str, str, str], destination: Path) -> None:
        downloads.append(destination.name)
        destination.write_text("# verified input\n", encoding="utf-8")

    monkeypatch.setattr(kernel_inputs, "download_verified", fake_download)

    def fake_run(command: list[str], *, check: bool) -> None:
        runs.append(command)
        if command[:3] == ["docker", "run", "--rm"] and "bash" in command:
            assert check is True
            work_dir = Path(command[command.index("-v") + 1].split(":", 1)[0])
            assert (work_dir / "build.sh").is_file()
            assert (work_dir / "check-config.sh").is_file()
            assert "CONFIG_VETH=y" in (work_dir / "config.fragment").read_text()
            out = work_dir / "out"
            out.mkdir()
            (out / "vmlinux-amd64.image").write_text("kernel", encoding="utf-8")
            (out / "vmlinux-amd64.config").write_text("config", encoding="utf-8")

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)

    kernel = build_debian._build_docker_kernel(image_dir=tmp_path, arch="amd64")

    assert kernel == tmp_path / "vmlinux.bin"
    assert kernel.read_text(encoding="utf-8") == "kernel"
    assert set(downloads) == set(kernel_inputs.KERNEL_INPUTS)
    assert any("/work/build.sh" in command for command in runs)
    assert any("/work/check-config.sh" in command for command in runs)
    assert runs[-1][-4:] == [
        "chown",
        "-R",
        f"{build_debian.os.getuid()}:{build_debian.os.getgid()}",
        "/work",
    ]


def test_kernel_input_mismatch_stops_before_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        kernel_inputs,
        "download_verified",
        lambda source, destination: (_ for _ in ()).throw(RuntimeError("digest mismatch")),
    )
    monkeypatch.setattr(
        build_debian.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Docker must not run")),
    )

    with pytest.raises(RuntimeError, match="digest mismatch"):
        build_debian._build_docker_kernel(image_dir=tmp_path, arch="amd64")


def test_kernel_check_failure_preserves_existing_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        kernel_inputs,
        "download_verified",
        lambda source, destination: destination.write_text("# verified input\n", encoding="utf-8"),
    )
    kernel = tmp_path / "vmlinux.bin"
    kernel.write_text("old kernel", encoding="utf-8")

    def fake_run(command: list[str], *, check: bool) -> None:
        if command[:3] == ["docker", "run", "--rm"] and "bash" in command:
            work_dir = Path(command[command.index("-v") + 1].split(":", 1)[0])
            out = work_dir / "out"
            out.mkdir()
            (out / "vmlinux-amd64.image").write_text("new kernel", encoding="utf-8")
            (out / "vmlinux-amd64.config").write_text("config", encoding="utf-8")
        if "/work/check-config.sh" in command:
            raise subprocess.CalledProcessError(1, command)
        assert check in {True, False}

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        build_debian._build_docker_kernel(image_dir=tmp_path, arch="amd64")

    assert kernel.read_text(encoding="utf-8") == "old kernel"


def test_build_containerfile_base_image_runs_expected_docker_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM debian:stable-slim\nUSER agent\n", encoding="utf-8")
    commands: list[list[str]] = []
    wrapper_text: list[str] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        commands.append(command)
        if command[:3] == ["docker", "build", "-t"]:
            wrapper_text.append((Path(command[-1]) / "Dockerfile").read_text(encoding="utf-8"))

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)

    root_tag = build_debian._build_containerfile_base_image("debian:stable-slim", containerfile)

    assert root_tag.startswith("sbx-debian-base:")
    assert commands[0][:4] == ["docker", "build", "--build-arg", "BASE_IMAGE=debian:stable-slim"]
    assert commands[1][:4] == ["docker", "build", "-t", root_tag]
    user_tag = root_tag.replace("sbx-debian-base:", "sbx-debian-base-user:")
    assert wrapper_text == [f"FROM {user_tag}\nUSER root\n"]


def test_build_containerfile_base_image_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Containerfile not found"):
        build_debian._build_containerfile_base_image("debian:stable-slim", tmp_path / "missing")
