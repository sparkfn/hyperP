"""Deployment contract tests for sales-prediction restricted artifacts."""

from __future__ import annotations

from pathlib import Path

from src.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"


def _compose() -> str:
    return _COMPOSE_PATH.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    return compose.split(f"  {service}:", 1)[1].split("\n\n", 1)[0]


def test_compose_declares_sales_prediction_artifact_env() -> None:
    compose = _compose()
    assert "SALES_PREDICTION_ARTIFACT_PRIMARY_ROOT: /app/restricted/sales-prediction" in compose
    assert (
        "SALES_PREDICTION_ARTIFACT_BACKUP_ROOT: /app/restricted/sales-prediction-backup" in compose
    )
    assert "SALES_PREDICTION_ARTIFACT_SIGNING_KEY_ID:" in compose
    assert "SALES_PREDICTION_ARTIFACT_SIGNING_KEY_SECRET:" in compose


def test_compose_mounts_restricted_sales_prediction_roots_on_both_workers() -> None:
    compose = _compose()
    for service in ("ingestion-worker", "lifecycle-worker"):
        block = _service_block(compose, service)
        assert "./data/restricted/sales-prediction:/app/restricted/sales-prediction" in block
        assert (
            "./data/restricted/sales-prediction-backup:/app/restricted/sales-prediction-backup"
            in block
        )


def test_settings_defaults_match_compose_container_paths() -> None:
    settings = Settings(neo4j_password="test")
    assert settings.sales_prediction_artifact_primary_root == "/app/restricted/sales-prediction"
    assert (
        settings.sales_prediction_artifact_backup_root == "/app/restricted/sales-prediction-backup"
    )
