"""Regression contract for root and tracked staging Compose documents."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

type ComposeScalar = str | int | float | bool | None
type ComposeValue = ComposeScalar | list["ComposeValue"] | dict[str, "ComposeValue"]
type ComposeDocument = dict[str, ComposeValue]
type PathStep = str | int

_ROOT = Path(__file__).resolve().parents[3]
_ROOT_COMPOSE = _ROOT / "docker-compose.yml"
_STAGING_COMPOSE = _ROOT / ".docker" / "staging" / "docker-compose.yml"


class _Missing:
    """Sentinel for a Compose path that must be absent."""


MISSING = _Missing()


@dataclass(frozen=True)
class ComposeTransformation:
    """One exact, documented root-to-staging transformation."""

    path: tuple[PathStep, ...]
    source: ComposeValue | _Missing
    destination: ComposeValue | _Missing


def _volumes(*entries: str) -> ComposeValue:
    return cast(ComposeValue, list(entries))


_ROOT_INGESTION_VOLUMES = _volumes(
    "./.dumps:/app/dumps:ro",
    "./config:/app/config:ro",
    "./data/restricted/sales-prediction:/app/restricted/sales-prediction",
    "./data/restricted/sales-prediction-backup:/app/restricted/sales-prediction-backup",
    "./data/restricted/crm-deal-identity-repair:/app/restricted/crm-deal-identity-repair",
    "./data/restricted/crm-deal-identity-repair-backup:/app/restricted/crm-deal-identity-repair-backup",
)
_STAGING_INGESTION_VOLUMES = _volumes(
    "./data/restricted/sales-prediction:/app/restricted/sales-prediction",
    "./data/restricted/sales-prediction-backup:/app/restricted/sales-prediction-backup",
    "../../.dumps:/app/dumps:ro",
    "../../config:/app/config:ro",
    "./.evidence/issue147-artifacts-primary-20260815:/artifacts-primary",
    "./.evidence/issue147-artifacts-backup-20260815:/artifacts-backup",
)
_ROOT_LIFECYCLE_VOLUMES = _volumes(
    "./config:/app/config:ro",
    "./data/restricted/sales-prediction:/app/restricted/sales-prediction",
    "./data/restricted/sales-prediction-backup:/app/restricted/sales-prediction-backup",
    "./data/restricted/crm-deal-identity-repair:/app/restricted/crm-deal-identity-repair",
    "./data/restricted/crm-deal-identity-repair-backup:/app/restricted/crm-deal-identity-repair-backup",
)
_STAGING_LIFECYCLE_VOLUMES = _volumes(
    "./data/restricted/sales-prediction:/app/restricted/sales-prediction",
    "./data/restricted/sales-prediction-backup:/app/restricted/sales-prediction-backup",
    "../../config:/app/config:ro",
)

_DEAL_INTELLIGENCE_HEALTHCHECKS: dict[str, ComposeValue] = {
    "api": cast(
        ComposeValue,
        {
            "test": ["CMD", "deal-intelligence-health", "--component", "api"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 10,
        },
    ),
    "worker": cast(
        ComposeValue,
        {
            "test": ["CMD", "deal-intelligence-health", "--component", "worker"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 10,
        },
    ),
    "scheduler": cast(
        ComposeValue,
        {
            "test": ["CMD", "deal-intelligence-health", "--component", "scheduler"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 10,
        },
    ),
}

_ROOT_ONLY_ENVIRONMENT: tuple[tuple[str, str], ...] = (
    ("DEPLOYMENT_ENVIRONMENT", "${DEPLOYMENT_ENVIRONMENT:-development}"),
    ("CRM_DEAL_IDENTITY_REPAIR_ENABLED", "${CRM_DEAL_IDENTITY_REPAIR_ENABLED:-false}"),
    (
        "CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_PRIMARY_ROOT",
        "/app/restricted/crm-deal-identity-repair",
    ),
    (
        "CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_BACKUP_ROOT",
        "/app/restricted/crm-deal-identity-repair-backup",
    ),
    (
        "CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_SIGNING_KEY_ID",
        "${CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_SIGNING_KEY_ID:-}",
    ),
    (
        "CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_SIGNING_KEY_SECRET",
        "${CRM_DEAL_IDENTITY_REPAIR_ARTIFACT_SIGNING_KEY_SECRET:-}",
    ),
    ("CRM_DEAL_IDENTITY_REPAIR_REPOSITORY_SHA", "${CRM_DEAL_IDENTITY_REPAIR_REPOSITORY_SHA:-}"),
    ("CRM_DEAL_IDENTITY_REPAIR_IMAGE_DIGEST", "${CRM_DEAL_IDENTITY_REPAIR_IMAGE_DIGEST:-}"),
)

EXCEPTIONS: tuple[ComposeTransformation, ...] = (
    ComposeTransformation(("name",), MISSING, "hyperp-ada-asia"),
    *(
        ComposeTransformation(("x-ingestion-env", key), value, MISSING)
        for key, value in _ROOT_ONLY_ENVIRONMENT
    ),
    *(
        ComposeTransformation(("services", name, "build", "context"), ".", "../..")
        for name in (
            "api",
            "frontend2",
            "ingestion-worker",
            "lifecycle-worker",
            "beat",
            "deal-intelligence-migrate",
        )
    ),
    ComposeTransformation(
        ("services", "api", "volumes"),
        _volumes("./.dumps:/app/dumps:ro"),
        _volumes("../../.dumps:/app/dumps:ro"),
    ),
    ComposeTransformation(("services", "web", "mem_limit"), "256M", "128M"),
    ComposeTransformation(("services", "web", "ports"), _volumes("${NGINX_PORT:-80}:80"), MISSING),
    ComposeTransformation(
        ("services", "web", "volumes"),
        _volumes("./services/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro"),
        _volumes("../../services/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro"),
    ),
    ComposeTransformation(("services", "web", "networks"), MISSING, _volumes("default", "traefik")),
    ComposeTransformation(("services", "ingestion-worker", "build", "target"), MISSING, "base"),
    ComposeTransformation(
        ("services", "ingestion-worker", "env_file"),
        MISSING,
        _volumes("./.evidence/issue147-smoke-20260815/stage-history-runtime.env"),
    ),
    ComposeTransformation(
        ("services", "ingestion-worker", "volumes"),
        _ROOT_INGESTION_VOLUMES,
        _STAGING_INGESTION_VOLUMES,
    ),
    ComposeTransformation(
        ("services", "lifecycle-worker", "volumes"),
        _ROOT_LIFECYCLE_VOLUMES,
        _STAGING_LIFECYCLE_VOLUMES,
    ),
    ComposeTransformation(("services", "beat", "cpus"), 0.5, 0.25),
    ComposeTransformation(("services", "beat", "mem_limit"), "512M", "256M"),
    ComposeTransformation(
        ("services", "beat", "volumes"),
        _volumes("./data/celerybeat:/var/celerybeat", "./config:/app/config:ro"),
        _volumes("./data/celerybeat:/var/celerybeat", "../../config:/app/config:ro"),
    ),
    ComposeTransformation(
        ("networks",),
        MISSING,
        cast(ComposeValue, {"traefik": {"external": True, "name": "traefik"}}),
    ),
)


def _read_compose(path: Path) -> ComposeDocument:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} must contain a YAML mapping"
    return cast(ComposeDocument, document)


def _at(document: ComposeDocument, path: tuple[PathStep, ...]) -> ComposeValue | _Missing:
    current: ComposeValue = document
    for step in path:
        assert isinstance(step, str), "exception paths must address mappings"
        assert isinstance(current, dict), f"{path!r} has a non-mapping parent"
        if step not in current:
            return MISSING
        current = current[step]
    return current


def _apply(root: ComposeDocument, transformation: ComposeTransformation) -> None:
    actual = _at(root, transformation.path)
    assert actual is not MISSING, f"exception source is absent: {transformation.path!r}"
    assert actual == transformation.source, f"exception source changed: {transformation.path!r}"
    parent = _at(root, transformation.path[:-1])
    key = transformation.path[-1]
    assert isinstance(parent, dict) and isinstance(key, str)
    if transformation.destination is MISSING:
        del parent[key]
    else:
        parent[key] = transformation.destination


def _transformed_root() -> ComposeDocument:
    root = deepcopy(_read_compose(_ROOT_COMPOSE))
    for transformation in EXCEPTIONS:
        if transformation.source is MISSING:
            assert _at(root, transformation.path) is MISSING, (
                f"exception source unexpectedly exists: {transformation.path!r}"
            )
            parent = _at(root, transformation.path[:-1])
            key = transformation.path[-1]
            assert isinstance(parent, dict) and isinstance(key, str)
            assert transformation.destination is not MISSING
            parent[key] = transformation.destination
        else:
            _apply(root, transformation)
    return root


def _assert_contract(staging: ComposeDocument) -> None:
    assert _transformed_root() == staging


def test_root_and_staging_compose_documents_match_after_exact_exceptions() -> None:
    _assert_contract(_read_compose(_STAGING_COMPOSE))


def test_staging_compose_critical_invariants() -> None:
    staging = _read_compose(_STAGING_COMPOSE)
    services = staging["services"]
    assert isinstance(services, dict)
    assert set(services) == {
        "deal-intelligence-postgres",
        "deal-intelligence-migrate",
        "deal-intelligence-api",
        "deal-intelligence-worker",
        "deal-intelligence-scheduler",
        "neo4j",
        "redis",
        "api",
        "frontend2",
        "web",
        "ingestion-worker",
        "lifecycle-worker",
        "beat",
    }
    web = services["web"]
    ingestion_worker = services["ingestion-worker"]
    beat = services["beat"]
    deal_intelligence_postgres = services["deal-intelligence-postgres"]
    deal_intelligence_migrate = services["deal-intelligence-migrate"]
    deal_intelligence_api = services["deal-intelligence-api"]
    deal_intelligence_worker = services["deal-intelligence-worker"]
    deal_intelligence_scheduler = services["deal-intelligence-scheduler"]
    assert isinstance(web, dict)
    assert isinstance(ingestion_worker, dict)
    assert isinstance(beat, dict)
    assert isinstance(deal_intelligence_postgres, dict)
    assert isinstance(deal_intelligence_migrate, dict)
    assert isinstance(deal_intelligence_api, dict)
    assert isinstance(deal_intelligence_worker, dict)
    assert isinstance(deal_intelligence_scheduler, dict)
    assert "ports" not in web
    assert web["networks"] == ["default", "traefik"]
    assert ingestion_worker["build"] == {
        "context": "../..",
        "dockerfile": "services/ingestion/Dockerfile",
        "target": "base",
    }
    assert ingestion_worker["env_file"] == [
        "./.evidence/issue147-smoke-20260815/stage-history-runtime.env"
    ]
    assert beat["cpus"] == 0.25
    assert beat["mem_limit"] == "256M"
    assert deal_intelligence_postgres["image"] == "postgres:16-alpine"
    assert deal_intelligence_postgres["profiles"] == [
        "deal-intelligence",
        "deal-intelligence-writer",
        "deal-intelligence-scheduler",
    ]
    assert "ports" not in deal_intelligence_postgres
    assert deal_intelligence_postgres["volumes"] == [
        "${DEAL_INTELLIGENCE_POSTGRES_DATA_ROOT:-./data/deal-intelligence-postgres}:"
        "/var/lib/postgresql/data"
    ]
    assert deal_intelligence_postgres["healthcheck"] == {
        "test": ["CMD-SHELL", 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'],
        "interval": "10s",
        "timeout": "5s",
        "retries": 10,
    }
    assert deal_intelligence_postgres["environment"] == {
        "POSTGRES_DB": "${DEAL_INTELLIGENCE_POSTGRES_DB:-deal_intelligence}",
        "POSTGRES_USER": "${DEAL_INTELLIGENCE_POSTGRES_USER:-deal_intelligence}",
        "POSTGRES_PASSWORD": "${DEAL_INTELLIGENCE_POSTGRES_PASSWORD:-}",
    }
    assert deal_intelligence_migrate["build"] == {
        "context": "../..",
        "dockerfile": "services/deal_intelligence/Dockerfile",
    }
    assert deal_intelligence_migrate["command"] == [
        "deal-intelligence-migrate",
        "upgrade",
        "heads",
    ]
    assert deal_intelligence_migrate["profiles"] == [
        "deal-intelligence",
        "deal-intelligence-writer",
        "deal-intelligence-scheduler",
    ]
    assert "healthcheck" not in deal_intelligence_migrate
    assert "ports" not in deal_intelligence_migrate
    assert deal_intelligence_migrate["depends_on"] == {
        "deal-intelligence-postgres": {"condition": "service_healthy"}
    }
    expected_deal_intelligence_environment = {
        "DEAL_INTELLIGENCE_DATABASE_URL": "${DEAL_INTELLIGENCE_DATABASE_URL:-}",
        "DEAL_INTELLIGENCE_DATABASE_HOST": "deal-intelligence-postgres",
        "DEAL_INTELLIGENCE_DATABASE_PORT": "5432",
        "DEAL_INTELLIGENCE_DATABASE_NAME": "${DEAL_INTELLIGENCE_POSTGRES_DB:-deal_intelligence}",
        "DEAL_INTELLIGENCE_DATABASE_USER": "${DEAL_INTELLIGENCE_POSTGRES_USER:-deal_intelligence}",
        "DEAL_INTELLIGENCE_DATABASE_PASSWORD": "${DEAL_INTELLIGENCE_POSTGRES_PASSWORD:-}",
    }
    assert deal_intelligence_migrate["environment"] == expected_deal_intelligence_environment
    for service, component in (
        (deal_intelligence_api, "api"),
        (deal_intelligence_worker, "worker"),
        (deal_intelligence_scheduler, "scheduler"),
    ):
        assert service["image"] == "hyperp-deal-intelligence:local"
        assert "build" not in service
        assert "ports" not in service
        assert service["healthcheck"] == _DEAL_INTELLIGENCE_HEALTHCHECKS[component]
        assert service["depends_on"] == {
            "deal-intelligence-migrate": {"condition": "service_completed_successfully"}
        }
        assert service["environment"] == expected_deal_intelligence_environment
    assert deal_intelligence_api["command"] == [
        "deal-intelligence-api",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ]
    assert deal_intelligence_worker["command"] == ["deal-intelligence-worker"]
    assert deal_intelligence_scheduler["command"] == ["deal-intelligence-scheduler"]
    assert deal_intelligence_api["profiles"] == ["deal-intelligence"]
    assert deal_intelligence_worker["profiles"] == ["deal-intelligence-writer"]
    assert deal_intelligence_scheduler["profiles"] == ["deal-intelligence-scheduler"]
    assert staging["networks"] == {"traefik": {"external": True, "name": "traefik"}}


def test_unapproved_staging_mutation_fails_contract() -> None:
    mutated = deepcopy(_read_compose(_STAGING_COMPOSE))
    services = mutated["services"]
    assert isinstance(services, dict)
    api = services["api"]
    assert isinstance(api, dict)
    api["mem_limit"] = "2G"

    with pytest.raises(AssertionError):
        _assert_contract(mutated)


def test_missing_exception_source_fails_contract() -> None:
    root = _read_compose(_ROOT_COMPOSE)
    ingestion_environment = root["x-ingestion-env"]
    assert isinstance(ingestion_environment, dict)
    del ingestion_environment["DEPLOYMENT_ENVIRONMENT"]

    with pytest.raises(AssertionError, match="exception source is absent"):
        for transformation in EXCEPTIONS:
            if transformation.source is not MISSING:
                _apply(root, transformation)
