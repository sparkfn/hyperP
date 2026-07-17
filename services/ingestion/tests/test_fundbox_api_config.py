from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.config import Settings


def test_fundbox_schedule_requires_api_connection_settings() -> None:
    with pytest.raises(ValidationError, match="Fundbox API configuration"):
        Settings(
            neo4j_password="test",
            fundbox_consumer_backend_ingest_cron="0 */6 * * *",
            _env_file=None,
        )


def test_fundbox_schedule_accepts_complete_api_connection_settings() -> None:
    settings = Settings(
        neo4j_password="test",
        fundbox_consumer_backend_ingest_cron="0 */6 * * *",
        fundbox_api_base_url="https://fundbox.test/api/v1",
        fundbox_api_username="hyperp",
        fundbox_api_password="secret",
        _env_file=None,
    )

    assert settings.fundbox_api_base_url == "https://fundbox.test/api/v1"


def test_fundbox_schedule_rejects_invalid_api_base_url() -> None:
    with pytest.raises(ValidationError, match="Fundbox API configuration"):
        Settings(
            neo4j_password="test",
            fundbox_consumer_backend_ingest_cron="0 */6 * * *",
            fundbox_api_base_url="https://",
            fundbox_api_username="hyperp",
            fundbox_api_password="secret",
            _env_file=None,
        )


def test_fundbox_schedule_rejects_plaintext_http() -> None:
    with pytest.raises(ValidationError, match="Fundbox API configuration"):
        Settings(
            neo4j_password="test",
            fundbox_consumer_backend_ingest_cron="0 */6 * * *",
            fundbox_api_base_url="http://fundbox.test/api/v1",
            fundbox_api_username="hyperp",
            fundbox_api_password="secret",
            _env_file=None,
        )
