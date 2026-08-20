from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_STAGING_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "deploy-staging.yml"


def test_compose_forwards_fundbox_api_configuration() -> None:
    compose = _COMPOSE_PATH.read_text(encoding="utf-8")

    for variable in (
        "FUNDBOX_API_BASE_URL",
        "FUNDBOX_API_USERNAME",
        "FUNDBOX_API_PASSWORD",
        "FUNDBOX_API_PAGE_SIZE",
        "FUNDBOX_API_TIMEOUT_SECONDS",
        "FUNDBOX_API_MAX_ATTEMPTS",
        "FUNDBOX_API_OVERLAP_SECONDS",
    ):
        assert f"{variable}: ${{{variable}" in compose


def test_staging_deploy_checks_fundbox_env_keys_without_values() -> None:
    workflow = _STAGING_WORKFLOW_PATH.read_text(encoding="utf-8")

    for variable in (
        "FUNDBOX_API_BASE_URL",
        "FUNDBOX_API_USERNAME",
        "FUNDBOX_API_PASSWORD",
        "FUNDBOX_API_PAGE_SIZE",
        "FUNDBOX_API_TIMEOUT_SECONDS",
        "FUNDBOX_API_MAX_ATTEMPTS",
        "FUNDBOX_API_OVERLAP_SECONDS",
    ):
        assert variable in workflow
    assert 'grep -Eq "^[[:space:]]+${key}:" .docker/staging/docker-compose.yml' in workflow
    assert "Staging Compose is missing runtime ingestion variable" in workflow
    assert "must resolve to a non-empty value" not in workflow
    assert "$COMPOSE config --format json" not in workflow
    assert "$COMPOSE build $SERVICES" in workflow
    assert "docker inspect" in workflow
    assert '"ingestion-worker" "lifecycle-worker" "beat"' in workflow
    assert "stable_checks" in workflow
    assert 'if [ "$stable_checks" -lt 6 ]' in workflow
    assert "Staging service did not remain running" in workflow
