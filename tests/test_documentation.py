"""Documentation contracts; never execute example shell commands or start a VM."""

import contextlib
import io
import re
import shlex
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from sbx import cli, gateway
from sbx.image import build_debian

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = sorted(DOCS.glob("*.md")) + [ROOT / "README.md", ROOT / "gateway/README.md"]
HISTORICAL = (
    "rootless-network-gateway-merge-readiness.md",
    "pi-package-node-version-issue.md",
    "per-mount-readonly-writable-draft.md",
)


def prose(text):
    return re.sub(r"^```.*?^```\s*$", "", text, flags=re.MULTILINE | re.DOTALL)


def links(path):
    return re.findall(r"\[[^\]]*\]\(([^)]+)\)", prose(path.read_text()))


def anchors(path):
    counts, result = {}, set()
    for heading in re.findall(r"^#{1,6} (.+)$", prose(path.read_text()), re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


@pytest.mark.parametrize("path", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_local_documentation_links(path):
    for target in links(path):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        assert destination.exists(), (path, target)
        if parsed.fragment and destination.suffix == ".md":
            assert unquote(parsed.fragment) in anchors(destination), (path, target)


@pytest.mark.parametrize("target", ["missing.md", "peer.md#missing"])
def test_link_check_rejects_missing_files_and_anchors(tmp_path, target):
    page = tmp_path / "page.md"
    page.write_text(f"[broken]({target})\n")
    (tmp_path / "peer.md").write_text("# Existing heading\n")
    with pytest.raises(AssertionError):
        test_local_documentation_links(page)


def test_anchor_check_ignores_code_and_supports_duplicate_headings(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("# A heading\n```bash\n# Not a heading\n```\n## A heading\n")
    assert anchors(page) == {"a-heading", "a-heading-1"}


def test_index_covers_maintained_pages_and_not_private_history():
    index = DOCS / "README.md"
    destinations = {(index.parent / urlsplit(link).path).resolve() for link in links(index)}
    expected = {path.resolve() for path in DOCS.glob("*.md") if path != index}
    assert expected <= destinations
    assert "docs/README.md" in links(ROOT / "README.md")
    for path in PAGES:
        text = path.read_text()
        assert "/home/nueces" not in text, path
        assert "docs/history" not in text, path
        for name in HISTORICAL:
            assert name not in text, (path, name)
    assert not any((DOCS / name).exists() for name in HISTORICAL)


@pytest.mark.parametrize("path", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_toml_examples_are_syntactically_valid(path):
    for source in re.findall(r"```toml\n(.*?)```", path.read_text(), re.DOTALL):
        tomllib.loads(source)


def test_gateway_policy_and_literal_examples_use_the_real_schema():
    text = (DOCS / "rootless-network-gateway.md").read_text()
    for heading in ("Network policies", "Direct IPv4 policies"):
        section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        examples = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)
        assert examples
        for source in examples:
            config = gateway.parse_config(tomllib.loads(source), platform="linux")
            assert config is not None and config.rules and not config.credentials


def test_default_allow_example_is_a_complete_allow_config():
    text = (DOCS / "rootless-network-gateway.md").read_text()
    section = text.split("## Default-allow egress\n", 1)[1].split("\n## ", 1)[0]
    examples = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)
    assert len(examples) == 1
    config = gateway.parse_config(tomllib.loads(examples[0]), platform="linux")
    assert config is not None and config.default == "allow" and config.rules


@pytest.mark.parametrize("path", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_literal_cli_examples_parse_without_running_handlers(path):
    parser = cli.build_parser()
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", path.read_text(), re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line.startswith("sbx "):
                continue
            tokens = shlex.split(line, comments=True)
            if any("..." in token for token in tokens):
                continue
            for operator in ("|", "&&", ";"):
                if operator in tokens:
                    tokens = tokens[: tokens.index(operator)]
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    parser.parse_args(tokens[1:])
                except SystemExit as exc:
                    assert exc.code == 0, (path, line)


def test_cli_check_rejects_unsupported_example_options(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("```bash\nsbx run --not-a-real-option\n```\n")
    with pytest.raises(AssertionError):
        test_literal_cli_examples_parse_without_running_handlers(page)


def test_example_and_readme_match_the_default_image_and_release_conventions():
    document = tomllib.loads((ROOT / "sbx.toml.example").read_text())
    parser = cli.build_parser()
    image = parser.parse_args(["image", "build"])
    assert document["sbx"]["image"] == f"~/.smolvm/images/{image.name}"
    assert image.name == "sbx"
    readme = (ROOT / "README.md").read_text()
    assert (
        len(
            re.findall(
                r"^uv tool install git\+https://github.com/nueces/sbx.git@v\d+\.\d+\.\d+$",
                readme,
                re.MULTILINE,
            )
        )
        == 1
    )
    assert "uv tool install --editable ." in readme
    assert "unreleased" in readme.lower()
    assert build_debian.BOOT_ARGS in (DOCS / "current-local-image-usage.md").read_text()


def test_complete_project_examples_load_without_touching_host_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", [])
    monkeypatch.setattr(cli, "_find_project_config", lambda: None)
    sources = [(ROOT / "sbx.toml.example").read_text()]
    sources += re.findall(r"```toml\n(.*?)```", (ROOT / "README.md").read_text(), re.DOTALL)
    for source in sources:
        path = tmp_path / "example.toml"
        path.write_text(source)
        document = cli.load_config(str(path))
        settings = document.get("sbx", {})
        if "memory" in settings:
            cli._validate_memory(settings["memory"])
        if "cpus" in settings:
            cli._validate_cpus(settings["cpus"])
        if "boot_timeout" in settings:
            cli._validate_boot_timeout(settings["boot_timeout"])
        cli._runtime_install(cli.build_parser().parse_args(["run"]), document)
        gateway.parse_config(document, platform="linux")


def test_complete_smoke_config_has_no_inherited_credential_or_mount_selection(
    tmp_path, monkeypatch
):
    text = (DOCS / "rootless-network-gateway-manual-validation.md").read_text()
    source = re.search(r'cat >"\$CONFIG" <<EOF\n(.*?)\nEOF', text, re.DOTALL)[1]
    for name in ("PROJECT_DIR", "IMAGE_DIR"):
        source = source.replace(f"${name}", str(tmp_path / name.lower()))
    path = tmp_path / "smoke.toml"
    path.write_text(source)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATHS", [])
    monkeypatch.setattr(cli, "_find_project_config", lambda: None)
    config = cli.load_config(str(path))
    sbx = config["sbx"]
    assert sbx["env"] == sbx["mount"] == sbx["runtime_install"] == sbx["port_forwards"] == []
    assert sbx["copy_host_credentials"] is sbx["git_config"] is sbx["stop_on_exit"] is False
    assert sbx["project_path"] == str(tmp_path / "project_dir")
    assert sbx["auth_port"] == "none"
    network = gateway.parse_config(config, platform="linux")
    assert network is not None and not network.credentials and len(network.rules) == 1


def test_rollback_preserves_and_verifies_evidence_before_reset():
    text = (DOCS / "rootless-network-gateway.md").read_text().split("## Rollback checklist\n", 1)[1]
    steps = re.findall(r"^\d+\. (.+)$", text, re.MULTILINE)
    assert "Stop affected" in steps[0]
    assert "Preserve" in steps[1] and "before any removal" in steps[1]
    assert "Verify" in steps[2]
    assert "Only after preservation" in steps[3]
    assert "explicit security-policy change" in steps[4]
    assert "URLs/query strings can contain secrets" in text


def test_operator_smoke_shares_only_a_disposable_project():
    text = (DOCS / "rootless-network-gateway-manual-validation.md").read_text()
    assert "mktemp -d" in text
    assert 'PROJECT_DIR="$SMOKE_DIR/project"' in text
    assert 'EVIDENCE_DIR="$SMOKE_DIR/host-evidence"' in text
    assert 'project_path = "$PROJECT_DIR"' in text
    assert "stop_on_exit = false" in text
    assert 'project_path = "$ROOT"' not in text
    assert "uv build" not in text and "rm -rf" not in text
    assert "flowchart LR" not in text
    assert "rootless-network-gateway-components.mmd" in text
