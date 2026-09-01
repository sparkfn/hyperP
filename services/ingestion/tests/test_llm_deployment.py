"""Deployment contract tests for ingestion LLM connector configuration."""

from pathlib import Path


def test_deployment_forwards_dedicated_gpt_connector_configuration() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    examples = (
        (root / ".env.example").read_text(encoding="utf-8"),
        (root / "services/ingestion/.env.example").read_text(encoding="utf-8"),
    )
    deploy = (root / "scripts/deploy/hyperp-staging.sh").read_text(encoding="utf-8")

    for name in ("GPT_API_BASE_URL", "GPT_API_KEY", "GPT_DEFAULT_MODEL"):
        assert f"{name}: ${{{name}" in compose
        assert name in deploy
        for example in examples:
            assert f"{name}=" in example

    assert "staging Compose is missing runtime ingestion variable" in deploy
