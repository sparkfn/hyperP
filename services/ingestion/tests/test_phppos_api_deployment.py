from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch
from src.config import Settings

API_ENV_NAMES = {
    "PHPPOS_API_BASE_URL",
    "PHPPOS_API_CLIENT_ID",
    "PHPPOS_API_CLIENT_SECRET",
    "PHPPOS_API_REFRESH_TOKEN",
    "SPEEDZONE_PHPPOS_API_TENANT_ID",
    "EKO_PHPPOS_API_TENANT_ID",
}

NUMERIC_API_DEFAULTS = {
    "PHPPOS_API_PAGE_SIZE": "500",
    "PHPPOS_API_TIMEOUT_SECONDS": "30.0",
    "PHPPOS_API_MAX_ATTEMPTS": "3",
}


def test_api_ingestion_environment_is_forwarded_and_documented(
    monkeypatch: MonkeyPatch,
) -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    root_example = (root / ".env.example").read_text(encoding="utf-8")
    ingestion_example = (root / "services/ingestion/.env.example").read_text(encoding="utf-8")

    for name in API_ENV_NAMES:
        assert f"{name}: ${{{name}:-}}" in compose
        assert f"{name}=" in root_example
        assert f"{name}=" in ingestion_example

    for name, default in NUMERIC_API_DEFAULTS.items():
        assert f"{name}: ${{{name}:-{default}}}" in compose
        assert f"{name}={default.removesuffix('.0')}" in root_example
        assert f"{name}={default.removesuffix('.0')}" in ingestion_example
        monkeypatch.setenv(name, default)

    settings = Settings(neo4j_password="test", _env_file=None)
    assert settings.phppos_api_page_size == 500
    assert settings.phppos_api_timeout_seconds == 30.0
    assert settings.phppos_api_max_attempts == 3
