from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from src.config import Settings
from src.connectors.whatsadmin_api.credentials import WhatsAdminCredentialResolver
from src.errors import SourceNotConfiguredError


def test_settings_store_separate_whatsadmin_entity_credentials() -> None:
    settings = Settings(
        neo4j_password="test",
        whatsadmin_eko_api_key=SecretStr("hk_eko_test"),
        whatsadmin_speedzone_api_key=SecretStr("hk_speedzone_test"),
        _env_file=None,
    )

    assert settings.whatsadmin_eko_api_key.get_secret_value() == "hk_eko_test"
    assert settings.whatsadmin_speedzone_api_key.get_secret_value() == "hk_speedzone_test"
    assert "whatsadmin_api_key" not in Settings.model_fields


@pytest.mark.parametrize(
    ("enabled_field", "expected_entity"),
    [
        ("whatsadmin_eko_enabled", "eko"),
        ("whatsadmin_speedzone_enabled", "speedzone"),
    ],
)
def test_enabled_whatsadmin_entity_requires_its_own_key(
    enabled_field: str,
    expected_entity: str,
) -> None:
    values: dict[str, object] = {
        "neo4j_password": "test",
        "whatsadmin_api_base_url": "https://whatsadmin.test",
        "whatsadmin_eko_api_key": SecretStr("hk_eko_test"),
        "whatsadmin_speedzone_api_key": SecretStr("hk_speedzone_test"),
        enabled_field: True,
        "_env_file": None,
    }
    values[f"whatsadmin_{expected_entity}_api_key"] = SecretStr("")

    with pytest.raises(ValidationError, match=expected_entity) as exc_info:
        Settings(**values)  # type: ignore[arg-type]

    error = str(exc_info.value)
    assert "hk_eko_test" not in error
    assert "hk_speedzone_test" not in error


def test_enabled_whatsadmin_entity_requires_shared_base_url() -> None:
    with pytest.raises(ValidationError, match="base URL"):
        Settings(
            neo4j_password="test",
            whatsadmin_eko_enabled=True,
            whatsadmin_eko_api_key=SecretStr("hk_eko_test"),
            _env_file=None,
        )


def test_enabled_whatsadmin_entity_rejects_non_handle_key_without_leaking_it() -> None:
    invalid_key = "invalid-key-must-not-leak"

    with pytest.raises(ValidationError, match="eko") as exc_info:
        Settings(
            neo4j_password="test",
            whatsadmin_api_base_url="https://whatsadmin.test",
            whatsadmin_eko_enabled=True,
            whatsadmin_eko_api_key=SecretStr(invalid_key),
            _env_file=None,
        )

    assert invalid_key not in str(exc_info.value)


def test_enabled_whatsadmin_entities_reject_a_shared_key_without_leaking_it() -> None:
    shared_key = "hk_shared_key_must_not_leak"

    with pytest.raises(ValidationError, match="distinct") as exc_info:
        Settings(
            neo4j_password="test",
            whatsadmin_api_base_url="https://whatsadmin.test",
            whatsadmin_eko_enabled=True,
            whatsadmin_speedzone_enabled=True,
            whatsadmin_eko_api_key=SecretStr(shared_key),
            whatsadmin_speedzone_api_key=SecretStr(shared_key),
            _env_file=None,
        )

    assert shared_key not in str(exc_info.value)


def test_resolver_returns_only_the_requested_entity_credential() -> None:
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr("hk_eko_test"),
        speedzone_api_key=SecretStr("hk_speedzone_test"),
    )

    eko = resolver.resolve("eko")
    speedzone = resolver.resolve("speedzone")

    assert eko.entity_key == "eko"
    assert eko.api_key.get_secret_value() == "hk_eko_test"
    assert speedzone.entity_key == "speedzone"
    assert speedzone.api_key.get_secret_value() == "hk_speedzone_test"


def test_resolver_default_job_resolves_both_entities_in_stable_order() -> None:
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr("hk_eko_test"),
        speedzone_api_key=SecretStr("hk_speedzone_test"),
    )

    credentials = resolver.resolve_job(None)

    assert [item.entity_key for item in credentials] == ["eko", "speedzone"]


def test_resolver_rejects_unknown_entity() -> None:
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr("hk_eko_test"),
        speedzone_api_key=SecretStr("hk_speedzone_test"),
    )

    with pytest.raises(ValueError, match="Unknown WhatsAdmin entity.*fundbox"):
        resolver.resolve("fundbox")


def test_resolver_missing_key_never_falls_back_or_leaks_other_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    other_key = "hk_speedzone_must_not_leak"
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr(""),
        speedzone_api_key=SecretStr(other_key),
    )

    with pytest.raises(SourceNotConfiguredError, match="eko") as exc_info:
        resolver.resolve("eko")

    output = f"{exc_info.value}\n{caplog.text}"
    assert other_key not in output


@pytest.mark.parametrize(
    "invalid_key",
    ["not-a-handle-key-secret", "hk_", " hk_padded_key"],
)
def test_resolver_rejects_invalid_key_without_exposing_it(
    caplog: pytest.LogCaptureFixture,
    invalid_key: str,
) -> None:
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr(invalid_key),
        speedzone_api_key=SecretStr("hk_speedzone_test"),
    )

    with pytest.raises(SourceNotConfiguredError, match="eko") as exc_info:
        resolver.resolve("eko")

    output = f"{exc_info.value}\n{caplog.text}"
    assert invalid_key not in output


def test_default_resolution_fails_before_returning_partial_credentials() -> None:
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr("hk_eko_test"),
        speedzone_api_key=SecretStr(""),
    )

    with pytest.raises(SourceNotConfiguredError, match="speedzone"):
        resolver.resolve_job(None)


def test_resolver_rejects_shared_entity_key_without_exposing_it() -> None:
    shared_key = "hk_shared_key_must_not_leak"
    resolver = WhatsAdminCredentialResolver(
        base_url="https://whatsadmin.test",
        eko_api_key=SecretStr(shared_key),
        speedzone_api_key=SecretStr(shared_key),
    )

    with pytest.raises(SourceNotConfiguredError, match="distinct") as exc_info:
        resolver.resolve("eko")

    assert shared_key not in str(exc_info.value)
