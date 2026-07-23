from __future__ import annotations

from src.config import Settings

# Fundbox API connection settings default to empty strings so the ingestion
# service can boot before credentials are provisioned. Empty or invalid config
# is rejected at dispatch time by FundboxApiCredentials (see
# test_fundbox_api_client.py), not at Settings load — these tests pin that the
# app starts cleanly even when a schedule is configured without credentials.


def test_fundbox_api_settings_load_with_empty_config() -> None:
    settings = Settings(
        neo4j_password="test",
        fundbox_ingest_cron="0 */6 * * *",
        _env_file=None,
    )

    assert settings.fundbox_api_base_url == ""
    assert settings.fundbox_api_username == ""
    assert settings.fundbox_api_password.get_secret_value() == ""


def test_fundbox_schedule_accepts_complete_api_connection_settings() -> None:
    settings = Settings(
        neo4j_password="test",
        fundbox_ingest_cron="0 */6 * * *",
        fundbox_api_base_url="https://fundbox.test/api/v1",
        fundbox_api_username="hyperp",
        fundbox_api_password="secret",
        _env_file=None,
    )

    assert settings.fundbox_api_base_url == "https://fundbox.test/api/v1"


def test_fundbox_api_settings_load_with_invalid_base_url() -> None:
    # A hostless URL must not block startup; it is rejected when the client is
    # constructed (see test_client_credentials_reject_hostless_base_url).
    settings = Settings(
        neo4j_password="test",
        fundbox_ingest_cron="0 */6 * * *",
        fundbox_api_base_url="https://",
        fundbox_api_username="hyperp",
        fundbox_api_password="secret",
        _env_file=None,
    )

    assert settings.fundbox_api_base_url == "https://"


def test_fundbox_api_settings_load_with_plaintext_http() -> None:
    # A plaintext URL must not block startup; it is rejected when the client is
    # constructed (see test_client_credentials_reject_plaintext_http).
    settings = Settings(
        neo4j_password="test",
        fundbox_ingest_cron="0 */6 * * *",
        fundbox_api_base_url="http://fundbox.test/api/v1",
        fundbox_api_username="hyperp",
        fundbox_api_password="secret",
        _env_file=None,
    )

    assert settings.fundbox_api_base_url == "http://fundbox.test/api/v1"
