from __future__ import annotations

from pathlib import Path

from src.config import Settings

WHATSADMIN_ENTITY_ENV = {
    "WHATSADMIN_EKO_API_KEY": "hk_replace_with_eko_handle_key",
    "WHATSADMIN_SPEEDZONE_API_KEY": "hk_replace_with_speedzone_handle_key",
    "WHATSADMIN_EKO_ENABLED": "false",
    "WHATSADMIN_SPEEDZONE_ENABLED": "false",
    "WHATSADMIN_LEGACY_ENTITY": "",
}

WHATSADMIN_STAGING_CONTRACT = (
    "WHATSADMIN_API_BASE_URL",
    *WHATSADMIN_ENTITY_ENV,
    "WHATSADMIN_API_PAGE_SIZE",
    "WHATSADMIN_API_TIMEOUT_SECONDS",
    "WHATSADMIN_API_MAX_ATTEMPTS",
    "WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS",
)

WHATSADMIN_RESILIENCE_DEFAULTS = {
    "WHATSADMIN_API_PAGE_SIZE": "25",
    "WHATSADMIN_API_TIMEOUT_SECONDS": "120",
    "WHATSADMIN_API_MAX_ATTEMPTS": "5",
    "WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS": "1",
}


def test_whatsadmin_entity_environment_is_forwarded_and_documented() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    examples = [
        (root / ".env.example").read_text(encoding="utf-8"),
        (root / "services/ingestion/.env.example").read_text(encoding="utf-8"),
    ]

    assert "WHATSADMIN_API_KEY" not in compose
    assert "whatsadmin_api_key" not in Settings.model_fields
    for example in examples:
        assert "WHATSADMIN_API_KEY" not in example

    for name, placeholder in WHATSADMIN_ENTITY_ENV.items():
        assert f"{name}: ${{{name}:-{placeholder if name.endswith('_ENABLED') else ''}}}" in compose
        for example in examples:
            assert f"{name}={placeholder}" in example


def test_whatsadmin_operations_doc_explains_global_key_migration() -> None:
    root = Path(__file__).parents[3]
    design = (
        root / "docs/superpowers/specs/profile-unifier-whatsadmin-api-ingestion-design.md"
    ).read_text(encoding="utf-8")

    assert "Migration from the global credential" in design
    assert "WHATSADMIN_API_KEY" in design
    assert "WHATSADMIN_EKO_API_KEY" in design
    assert "WHATSADMIN_SPEEDZONE_API_KEY" in design
    assert "WHATSADMIN_LEGACY_ENTITY" in design
    assert "fail closed" in design
    assert "hk_replace_with_eko_handle_key" in design
    assert "hk_replace_with_speedzone_handle_key" in design


def test_whatsadmin_timeout_resilience_defaults_are_aligned() -> None:
    root = Path(__file__).parents[3]
    settings = Settings(neo4j_password="test", _env_file=None)
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/deploy-staging.yml").read_text(encoding="utf-8")
    examples = [
        (root / ".env.example").read_text(encoding="utf-8"),
        (root / "services/ingestion/.env.example").read_text(encoding="utf-8"),
    ]

    assert settings.whatsadmin_api_page_size == 25
    assert settings.whatsadmin_api_timeout_seconds == 120.0
    assert settings.whatsadmin_api_max_attempts == 5
    assert settings.whatsadmin_api_retry_base_delay_seconds == 1.0
    for name, value in WHATSADMIN_RESILIENCE_DEFAULTS.items():
        compose_value = f"{value}.0" if name.endswith("SECONDS") else value
        assert f"{name}: ${{{name}:-{compose_value}}}" in compose
        assert f"export {name}={value}" in workflow
        for example in examples:
            assert f"{name}={value}" in example
    assert "for ingestion_service in ingestion-worker lifecycle-worker beat" in workflow
    assert 'grep -Fxq "$ingestion_service" <<< "$configured_services"' in workflow
    assert "Staging Compose is missing required service" in workflow
    assert 'resolved_service=$($COMPOSE config "$ingestion_service")' in workflow


def test_staging_preflight_requires_tenant_contract_before_build() -> None:
    root = Path(__file__).parents[3]
    workflow = (root / ".github/workflows/deploy-staging.yml").read_text(encoding="utf-8")
    preflight = workflow[: workflow.index("# --- Decide which service images")]

    for name in WHATSADMIN_STAGING_CONTRACT:
        assert name in preflight
    assert 'retired_key="WHATSADMIN_API_KEY"' in preflight
    assert "Update .docker/staging/docker-compose.yml before rebuilding" in preflight
    assert workflow.index("WHATSADMIN_EKO_API_KEY") < workflow.index("merge --ff-only")
    assert workflow.index('retired_key="WHATSADMIN_API_KEY"') < workflow.index("merge --ff-only")
    assert workflow.index("WHATSADMIN_EKO_API_KEY") < workflow.index("$COMPOSE build")


def test_operations_doc_includes_host_managed_staging_migration() -> None:
    root = Path(__file__).parents[3]
    design = (
        root / "docs/superpowers/specs/profile-unifier-whatsadmin-api-ingestion-design.md"
    ).read_text(encoding="utf-8")

    assert ".docker/staging/docker-compose.yml" in design
    assert "before rebuilding" in design
    for name in WHATSADMIN_STAGING_CONTRACT:
        assert name in design
