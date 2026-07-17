from pathlib import Path

from celery.schedules import crontab

from src.config import Settings
from src.schedule_builders import add_sgbankruptcy_schedule


def test_sgbankruptcy_api_environment_and_schedule_are_configured() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    root_example = (root / ".env.example").read_text(encoding="utf-8")
    ingestion_example = (root / "services/ingestion/.env.example").read_text(
        encoding="utf-8"
    )
    names = {
        "SGBANKRUPTCY_API_BASE_URL",
        "SGBANKRUPTCY_API_KEY",
        "SGBANKRUPTCY_API_PAGE_SIZE",
        "SGBANKRUPTCY_API_TIMEOUT_SECONDS",
        "SGBANKRUPTCY_API_MAX_ATTEMPTS",
        "SGBANKRUPTCY_INGEST_CRON",
    }
    for name in names:
        assert name in compose
        assert f"{name}=" in root_example
        assert f"{name}=" in ingestion_example
    settings = Settings(neo4j_password="test", _env_file=None)
    assert settings.sgbankruptcy_api_max_attempts == 3
    assert settings.sgbankruptcy_ingest_cron == ""


def test_sgbankruptcy_cron_builds_real_beat_schedule() -> None:
    schedule: dict[str, dict[str, object]] = {}

    add_sgbankruptcy_schedule(schedule, "15 2 * * *")

    entry = schedule["sgbankruptcy-ingest"]
    assert entry["task"] == "src.tasks.run_ingestion_task"
    assert entry["args"] == ("sgbankruptcy", "api")
    assert isinstance(entry["schedule"], crontab)
