from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_STAGING_DEPLOY_PATH = _REPO_ROOT / "scripts" / "deploy" / "hyperp-staging.sh"


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
    deploy = _STAGING_DEPLOY_PATH.read_text(encoding="utf-8")

    for variable in (
        "FUNDBOX_API_BASE_URL",
        "FUNDBOX_API_USERNAME",
        "FUNDBOX_API_PASSWORD",
        "FUNDBOX_API_PAGE_SIZE",
        "FUNDBOX_API_TIMEOUT_SECONDS",
        "FUNDBOX_API_MAX_ATTEMPTS",
        "FUNDBOX_API_OVERLAP_SECONDS",
    ):
        assert variable in deploy
    assert 'grep -Eq "^[[:space:]]+${key}:" "${COMPOSE_FILE}"' in deploy
    assert "staging Compose is missing runtime ingestion variable" in deploy
    assert "must resolve to a non-empty value" not in deploy
    assert '"${COMPOSE[@]}" build "${BUILD_SERVICE_ARRAY[@]}"' in deploy
    assert "docker inspect" in deploy
    assert "wait_service_stable ingestion-worker" in deploy
    assert "wait_service_stable lifecycle-worker" in deploy
    assert "wait_service_stable beat" in deploy
    assert "stable_checks" in deploy
    assert "did not remain running" in deploy
