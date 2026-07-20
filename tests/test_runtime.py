from sbx.runtime import env_boolean, smolvm_env


def test_env_boolean_accepts_common_truthy_values() -> None:
    for value in ("1", "true", "TRUE", " yes ", "on"):
        assert env_boolean(value) is True


def test_env_boolean_rejects_falsey_values() -> None:
    for value in (None, "", "0", "false", "no", "off"):
        assert env_boolean(value) is False


def test_smolvm_env_preserves_custom_env_and_disables_notice_by_default() -> None:
    env = smolvm_env({"HOME": "/tmp/home"})

    assert env["HOME"] == "/tmp/home"
    assert env["SMOLVM_DISABLE_VERSION_CHECK"] == "1"


def test_smolvm_env_opt_in_leaves_version_check_enabled() -> None:
    env = smolvm_env({"SBX_SMOLVM_VERSION_NOTICES": "yes"})

    assert "SMOLVM_DISABLE_VERSION_CHECK" not in env
