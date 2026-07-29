from pathlib import Path

from src.config import Settings


def test_sgbankruptcy_api_environment_is_configured() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    root_example = (root / ".env.example").read_text(encoding="utf-8")
    ingestion_example = (root / "services/ingestion/.env.example").read_text(encoding="utf-8")
    names = {
        "SGBANKRUPTCY_API_BASE_URL",
        "SGBANKRUPTCY_API_KEY",
        "SGBANKRUPTCY_API_PAGE_SIZE",
        "SGBANKRUPTCY_API_TIMEOUT_SECONDS",
        "SGBANKRUPTCY_API_MAX_ATTEMPTS",
    }
    for name in names:
        assert name in compose
        assert f"{name}=" in root_example
        assert f"{name}=" in ingestion_example
    settings = Settings(neo4j_password="test", _env_file=None)
    assert settings.sgbankruptcy_api_max_attempts == 3


def test_sgbankruptcy_is_scheduled_in_the_fixed_friday_group() -> None:
    from src.scheduled_ingestion_groups import scheduled_ingestion_group

    group = scheduled_ingestion_group("sgbankruptcy")

    assert group.weekday == "friday"
    assert [task.source_key for task in group.tasks] == ["sgbankruptcy"]
