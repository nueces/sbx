import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from sbx import gateway, gateway_credentials

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "network-credentials.md"


def _example(heading):
    section = re.split(
        r"^#{2,3} " + re.escape(heading) + "\n", GUIDE.read_text(), flags=re.MULTILINE
    )[1]
    return tomllib.loads(re.search(r"```toml\n(.*?)```", section, re.DOTALL)[1])


def _validate(document):
    config = gateway.parse_config(document, platform="linux")
    assert config is not None
    described = []
    for credential in config.credentials:
        if credential.method == "plugin:aws-codeartifact":
            header = gateway_credentials.aws_codeartifact().describe(
                credential.id, credential.options
            )["header"]
            credential = replace(credential, header=header)
        else:
            assert credential.method == "command"
        described.append(credential)
    return gateway.finalize_credentials(config, described)


def test_credential_guide_toml_fences_parse():
    examples = re.findall(r"```toml\n(.*?)```", GUIDE.read_text(), re.DOTALL)
    assert examples
    for source in examples:
        tomllib.loads(source)


def test_direct_ip_examples_are_explicit_and_credential_free():
    section = (
        GUIDE.read_text().split("## Direct IPv4 policies\n", 1)[1].split("## Credentials\n", 1)[0]
    )
    examples = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)
    assert len(examples) == 2
    for source in examples:
        config = _validate(tomllib.loads(source))
        assert not config.credentials and len(config.rules) == 1
        assert gateway._valid_ip(config.rules[0].host)


def test_full_credential_example_uses_shipped_methods_and_explicit_bindings():
    config = _validate(_example("Full example"))
    credentials = {credential.id: credential for credential in config.credentials}
    github = credentials["github"]
    assert github.method == "command" and github.fetch == "startup"
    assert github.bindings[0].policies == ("github-api", "github-uploads")
    assert github.bindings[0].header == "Authorization: Bearer {placeholder}"
    assert credentials["codeartifact"].header == {
        "name": "Authorization",
        "format": "basic-password",
        "username": "aws",
    }


@pytest.mark.parametrize(
    "heading",
    [
        "Credentials",
        "One credential, different headers",
        "Two credentials on one request",
        "Fetching at startup for authentication",
        "Text output",
        "JSON output",
    ],
)
def test_credential_fragments_validate_with_documented_policy_context(heading):
    document = _example("Full example")
    document["network"]["policy"].extend(
        {"id": name, "mode": "http-mediated", "hosts": [f"{name}.example.com"], "ports": [443]}
        for name in ("service-api", "service-legacy", "custom-api")
    )
    document["network"]["credential"] = _example(heading)["network"]["credential"]
    _validate(document)


def test_binding_variants_validate_as_replacements_not_overlapping_additions():
    section = GUIDE.read_text().split("## Bindings\n", 1)[1].split("### One credential", 1)[0]
    examples = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)
    assert len(examples) == 3
    for source in examples:
        document = _example("Full example")
        github = next(c for c in document["network"]["credential"] if c["id"] == "github")
        github["bindings"] = tomllib.loads(source)["network"]["credential"]["bindings"]
        _validate(document)


def test_aws_environment_variant_separates_inherited_names_from_provider_options():
    document = _example("Full example")
    aws = next(c for c in document["network"]["credential"] if c["id"] == "codeartifact")
    aws["options"] = _example("AWS environment credentials instead of a profile")["network"][
        "credential"
    ]["options"]
    config = _validate(document)
    credential = next(c for c in config.credentials if c.id == "codeartifact")
    assert credential.inherit_environment == (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    )
    assert "inherit_environment" not in credential.options and "profile" not in credential.options
