import hashlib
import json
import os
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

    def fake_base(base_image: str, containerfile: Path, *, context_dir: Path | None = None) -> str:
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

    monkeypatch.setattr(build_debian, "ImageBuilder", Builder)
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
    (tmp_path / "linux.sha256").write_text(f"{expected}  linux-6.12.85.tar.xz\n", encoding="utf-8")
    (tmp_path / "linux-6.12.85.tar.xz").write_bytes(b"changed")

    with pytest.raises(RuntimeError) as error:
        build_debian._raise_if_linux_digest_mismatch(tmp_path)

    message = str(error.value)
    assert "linux-6.12.85.tar.xz" in message
    assert f"expected: {expected}" in message
    assert f"actual:   {hashlib.sha256(b'changed').hexdigest()}" in message


def test_packaged_image_is_user_neutral() -> None:
    with build_debian._packaged_resources() as resources:
        base = (resources / build_debian.DEFAULT_BASE_CONTAINERFILE).read_text(encoding="utf-8")
        docker = (resources / build_debian.DEFAULT_DOCKER_CONTAINERFILE).read_text(encoding="utf-8")
        helper = (resources / "scripts/sbx-start-rootless-docker").read_text(encoding="utf-8")
        assert not (resources / "Containers/Agents/Pi.Containerfile").exists()

    assert "useradd" not in base
    assert "/home/agent" not in base + docker
    assert "USER agent" not in base + docker
    assert "1000" not in docker
    assert "/opt/sbx/user-home" not in base + docker
    assert "pi-coding-agent" not in base + docker
    assert "specify-cli" not in base + docker
    assert "github/spec-kit" not in base + docker
    assert "UV_INSTALL_DIR=/usr/local/bin" in base
    assert "SBX_DOCKER_USER:?" in helper
    assert "start-stop-daemon --start --background" in helper
    assert "--exec /usr/bin/dockerd-rootless.sh" in helper
    assert "daemon_running" in helper
    assert "running but unresponsive" in helper
    assert "kill" not in helper
    assert "nohup" not in helper


def test_rootless_docker_helper_detaches_and_waits_for_readiness(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()

    def stub(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)

    stub(
        "id",
        'case "${1-}" in\n'
        '  -u) [ "$#" -eq 1 ] && echo 0 || echo 1234 ;;\n'
        "  -gn) echo developer ;;\n"
        "esac\n",
    )
    stub(
        "sudo",
        'shift 2\n[ "${1-}" = env ] && shift\n'
        'while [ "$#" -gt 0 ] && [ "${1#*=}" != "$1" ]; do export "$1"; shift; done\n'
        'command="$1"; shift\nexec "$SBX_TEST_BIN/$command" "$@"\n',
    )
    stub(
        "docker",
        'printf "call\\n" >> "$SBX_TEST_STATE/docker-calls"\n[ -f "$SBX_TEST_STATE/started" ]\n',
    )
    stub(
        "start-stop-daemon",
        'printf "%s\\n" "$*" > "$SBX_TEST_STATE/launch"\n: > "$SBX_TEST_STATE/started"\n',
    )
    stub("pgrep", "exit 1\n")
    for name in ("awk", "chmod", "chown", "mkdir", "mknod", "mount", "sleep", "touch"):
        stub(name, ":\n")

    with build_debian._packaged_resources() as resources:
        helper = resources / "scripts/sbx-start-rootless-docker"
        completed = subprocess.run(
            ["sh", str(helper), str(tmp_path / "dockerd.log")],
            check=False,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "SBX_DOCKER_RESOLV_CONF": str(tmp_path / "resolv.conf"),
                "SBX_DOCKER_USER": "developer",
                "SBX_TEST_BIN": str(bin_dir),
                "SBX_TEST_STATE": str(state_dir),
            },
        )

    assert completed.returncode == 0, completed.stderr
    launch = (state_dir / "launch").read_text(encoding="utf-8")
    assert "--background" in launch
    assert "--exec /usr/bin/dockerd-rootless.sh" in launch
    assert (state_dir / "docker-calls").read_text(encoding="utf-8").count("call") == 2


def test_compose_containerfiles_includes_optional_nested_kvm_layer(tmp_path: Path) -> None:
    base = tmp_path / "Base.Containerfile"
    docker = tmp_path / "Docker.Containerfile"
    nested = tmp_path / "NestedKvm.Containerfile"
    agent = tmp_path / "Pi.Containerfile"
    output = tmp_path / "Combined.Containerfile"
    base.write_text("FROM debian AS sbx-base\nRUN echo base\n", encoding="utf-8")
    docker.write_text("RUN echo docker\n", encoding="utf-8")
    nested.write_text("RUN echo nested\n", encoding="utf-8")
    agent.write_text("FROM sbx-base AS sbx-final\nRUN echo pi\n", encoding="utf-8")

    build_debian._compose_containerfiles(base, docker, agent, output)
    assert "nested" not in output.read_text(encoding="utf-8")

    build_debian._compose_containerfiles(
        base, docker, agent, output, nested_kvm_containerfile=nested
    )
    combined = output.read_text(encoding="utf-8")
    assert combined.index("RUN echo base") < combined.index("RUN echo docker")
    assert combined.index("RUN echo docker") < combined.index("RUN echo nested")
    assert combined.index("RUN echo nested") < combined.index("RUN echo pi")


def test_packaged_nested_kvm_resources_are_minimal_and_safe() -> None:
    with build_debian._packaged_resources() as resources:
        containerfile = (resources / build_debian.DEFAULT_NESTED_KVM_CONTAINERFILE).read_text()
        fragment = (resources / build_debian.DEFAULT_NESTED_KVM_KERNEL_FRAGMENT).read_text()
        helper = (resources / "scripts" / "sbx-prepare-kvm").read_text()

    for package in ("qemu-system-x86", "qemu-utils", "e2fsprogs"):
        assert package in containerfile
    assert "sbx-prepare-kvm" in containerfile
    assert set(fragment.splitlines()) == {f"{name}=y" for name in build_debian.NESTED_KVM_CONFIG}
    assert "root:kvm" in helper
    assert "chmod 0660 /dev/kvm" in helper
    assert "usermod -aG kvm" in helper
    assert "0666" not in helper
    assert "mknod" not in helper


def test_default_build_always_uses_docker_kernel_and_feature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    combined, calls = _fake_build(monkeypatch, tmp_path)

    rc = build_debian.main([])

    assert rc == 0
    assert "# ---- Docker layer ----" in combined[0]
    assert "# ---- Optional tooling layer ----" not in combined[0]
    assert "Nested KVM layer" not in combined[0]
    rootfs_call = calls["rootfs"]
    assert isinstance(rootfs_call, dict)
    assert rootfs_call["name"] == "sbx"
    assert rootfs_call["rootfs_size_mb"] == 20480
    assert rootfs_call["base_image"] == "sbx-debian-base:test"
    assert rootfs_call["arch"] == "amd64"
    manifest = json.loads((tmp_path / "sbx" / "smolvm-image.json").read_text())
    assert manifest["kernel"] == "vmlinux.bin"
    assert manifest["sbx"] == {"features": ["docker"]}
    kernel_call = calls["kernel"]
    assert isinstance(kernel_call, dict)
    assert kernel_call["nested_kvm"] is False
    output = capsys.readouterr().out
    assert "kernel:" in output
    assert "sbx run the-quest" in output
    assert f"--image {tmp_path / 'sbx'}" in output
    assert "--runtime-install pi" in output
    assert "--write-config" in output
    assert "nested KVM:" not in output


def test_nested_kvm_build_composes_resources_manifest_and_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    combined, calls = _fake_build(monkeypatch, tmp_path)

    rc = build_debian.main(["--name", "nested", "--nested-kvm", "--json"])

    assert rc == 0
    assert "# ---- Nested KVM layer ----" in combined[0]
    assert "qemu-system-x86" in combined[0]
    kernel_call = calls["kernel"]
    assert isinstance(kernel_call, dict)
    assert kernel_call["nested_kvm"] is True
    manifest = json.loads((tmp_path / "nested" / "smolvm-image.json").read_text())
    assert manifest["sbx"]["features"] == ["docker", "nested-kvm"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["nested_kvm"] is True
    assert payload["nested_kvm_containerfile"].endswith("NestedKvm.Containerfile")


def test_nested_kvm_rejects_unsupported_host_before_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class ArmBuilder(FakeImageBuilder):
        def _host_arch_key(self) -> str:
            return "aarch64"

    monkeypatch.setattr(build_debian, "ImageBuilder", ArmBuilder)
    monkeypatch.setattr(build_debian.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        build_debian,
        "_build_containerfile_base_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build must not start")),
    )

    assert build_debian.main(["--nested-kvm", "--cache-dir", str(tmp_path)]) == 2
    assert "requires a Linux x86_64 host" in capsys.readouterr().err


def test_nested_kvm_rejects_non_linux_and_custom_containerfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_debian.platform, "system", lambda: "Darwin")
    assert build_debian.main(["--nested-kvm", "--cache-dir", str(tmp_path)]) == 2
    assert "detected Darwin x86_64" in capsys.readouterr().err

    monkeypatch.setattr(build_debian.platform, "system", lambda: "Linux")
    custom = tmp_path / "Containerfile"
    custom.write_text("FROM debian:stable-slim\n", encoding="utf-8")
    assert build_debian.main(["--nested-kvm", "--containerfile", str(custom)]) == 2
    assert "cannot be combined with --containerfile" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("security_options", "expected"),
    [('["name=rootless"]', "0:0"), ('["name=seccomp"]', f"{os.getuid()}:{os.getgid()}")],
)
def test_docker_bind_mount_owner(
    monkeypatch: pytest.MonkeyPatch, security_options: str, expected: str
) -> None:
    monkeypatch.setattr(
        build_debian.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, security_options, ""),
    )

    assert build_debian._docker_bind_mount_owner() == expected


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
            volume = command[command.index("-v") + 1]
            assert volume.endswith(":/work:z")
            work_dir = Path(volume.split(":", 1)[0])
            assert (work_dir / "build.sh").is_file()
            assert (work_dir / "check-config.sh").is_file()
            assert "CONFIG_VETH=y" in (work_dir / "config.fragment").read_text()
            out = work_dir / "out"
            out.mkdir()
            (out / "vmlinux-amd64.image").write_text("kernel", encoding="utf-8")
            (out / "vmlinux-amd64.config").write_text("config", encoding="utf-8")

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)
    monkeypatch.setattr(
        build_debian, "_docker_bind_mount_owner", lambda: f"{os.getuid()}:{os.getgid()}"
    )

    kernel = build_debian._build_docker_kernel(image_dir=tmp_path, arch="amd64")

    assert kernel == tmp_path / "vmlinux.bin"
    assert kernel.read_text(encoding="utf-8") == "kernel"
    assert set(downloads) == set(kernel_inputs.KERNEL_INPUTS)
    assert any("/work/build.sh" in command for command in runs)
    assert any("/work/check-config.sh" in command for command in runs)
    assert runs[-1][-4:] == ["chown", "-R", f"{os.getuid()}:{os.getgid()}", "/work"]


def test_nested_kvm_final_config_validation(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text(
        "CONFIG_VIRTUALIZATION=y\nCONFIG_KVM=y\nCONFIG_KVM_INTEL=y\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="CONFIG_KVM_AMD=y.*got unset"):
        build_debian._verify_nested_kvm_config(config)

    config.write_text(
        "\n".join(f"{name}=y" for name in build_debian.NESTED_KVM_CONFIG),
        encoding="utf-8",
    )
    build_debian._verify_nested_kvm_config(config)


def test_nested_kvm_kernel_fragment_failure_preserves_existing_kernel(
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
            fragment = (work_dir / "config.fragment").read_text(encoding="utf-8")
            assert "CONFIG_KVM_INTEL=y" in fragment
            assert "CONFIG_KVM_AMD=y" in fragment
            out = work_dir / "out"
            out.mkdir()
            (out / "vmlinux-amd64.image").write_text("new kernel", encoding="utf-8")
            (out / "vmlinux-amd64.config").write_text(
                "CONFIG_VIRTUALIZATION=y\nCONFIG_KVM=y\nCONFIG_KVM_INTEL=y\n",
                encoding="utf-8",
            )
        assert check in {True, False}

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)
    monkeypatch.setattr(
        build_debian, "_docker_bind_mount_owner", lambda: f"{os.getuid()}:{os.getgid()}"
    )

    with pytest.raises(RuntimeError, match="CONFIG_KVM_AMD"):
        build_debian._build_docker_kernel(image_dir=tmp_path, arch="amd64", nested_kvm=True)

    assert kernel.read_text(encoding="utf-8") == "old kernel"


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
    monkeypatch.setattr(
        build_debian, "_docker_bind_mount_owner", lambda: f"{os.getuid()}:{os.getgid()}"
    )

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

    def fake_run(
        command: list[str], *, check: bool, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert check is True
        commands.append(command)
        if command[:3] == ["docker", "build", "-t"]:
            wrapper_text.append((Path(command[-1]) / "Dockerfile").read_text(encoding="utf-8"))
        stdout = "sha256:user-image\n" if command[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)

    root_tag = build_debian._build_containerfile_base_image("debian:stable-slim", containerfile)

    expected_digest = hashlib.sha256(b"sha256:user-image").hexdigest()[:12]
    assert root_tag == f"sbx-debian-base:{expected_digest}"
    assert commands[0][:4] == ["docker", "build", "--build-arg", "BASE_IMAGE=debian:stable-slim"]
    user_tag = commands[0][commands[0].index("-t") + 1]
    assert commands[1] == ["docker", "image", "inspect", "--format", "{{.Id}}", user_tag]
    assert commands[2][:4] == ["docker", "build", "-t", root_tag]
    assert wrapper_text == [f"FROM {user_tag}\nUSER root\n"]


def test_build_containerfile_base_image_tag_tracks_built_image_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM debian:stable-slim\nCOPY payload /payload\n", encoding="utf-8")
    image_ids = iter(["sha256:first", "sha256:second"])

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = next(image_ids) if command[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(build_debian.subprocess, "run", fake_run)

    first = build_debian._build_containerfile_base_image("debian:stable-slim", containerfile)
    second = build_debian._build_containerfile_base_image("debian:stable-slim", containerfile)

    assert first != second


def test_build_containerfile_base_image_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Containerfile not found"):
        build_debian._build_containerfile_base_image("debian:stable-slim", tmp_path / "missing")
