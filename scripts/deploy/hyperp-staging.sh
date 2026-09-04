#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_SHA="${1:?expected commit SHA is required}"
HEALTH_URL_B64="${2:?base64-encoded staging health URL is required}"
REVISION_CHECK_MODE="${3:-strict}"
STAGING_DIR="/home/docker/hyperp.ada.asia/.docker/staging"
LOCK_FILE="/tmp/hyperp-staging-deploy.lock"
DEPLOYED_REVISION_FILE="${STAGING_DIR}/data/deployed-revision"
DEPLOYMENT_ATTEMPT_FILE="${STAGING_DIR}/data/deployment-attempt"
EXPECTED_SERVICES=(
  neo4j
  redis
  api
  frontend2
  web
  ingestion-worker
  lifecycle-worker
  beat
)
CURRENT_PHASE="preflight"
REPO_DIR=""
COMPOSE_FILE=""
LIFECYCLE_PAUSE_MARKER=""
LIFECYCLE_PAUSED=false
COMPOSE=()
CONFIGURED_SERVICES=()

safe_diagnostics() {
  local service=""
  local container_id=""
  local running=""
  local health=""

  if [[ -n "${REPO_DIR}" && -d "${REPO_DIR}" ]]; then
    printf '%s\n' '[hyperp-staging] safe Git state:' >&2
    git -C "${REPO_DIR}" status --short --branch >&2 || true
    printf '[hyperp-staging] expected revision: %s\n' "${EXPECTED_SHA}" >&2
    git -C "${REPO_DIR}" rev-parse HEAD >&2 || true
    git -C "${REPO_DIR}" rev-parse origin/main >&2 || true
    git -C "${REPO_DIR}" rev-parse origin/staging >&2 || true
  fi

  if (( ${#COMPOSE[@]} > 0 )); then
    printf '%s\n' '[hyperp-staging] safe Compose state:' >&2
    "${COMPOSE[@]}" ps >&2 || true
    for service in "${EXPECTED_SERVICES[@]}"; do
      container_id="$("${COMPOSE[@]}" ps -q "${service}" 2>/dev/null || true)"
      [[ -n "${container_id}" ]] || continue
      running="$(docker inspect --format '{{.State.Running}}' "${container_id}" 2>/dev/null || true)"
      health="$(
        docker inspect \
          --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
          "${container_id}" 2>/dev/null || true
      )"
      printf '[hyperp-staging] %s running=%s health=%s\n' \
        "${service}" "${running:-unknown}" "${health:-unknown}" >&2
    done
  fi

  if [[ -f "${DEPLOYED_REVISION_FILE}" ]]; then
    printf '[hyperp-staging] recorded revision: ' >&2
    cat "${DEPLOYED_REVISION_FILE}" >&2 || true
  fi
  if [[ -f "${DEPLOYMENT_ATTEMPT_FILE}" ]]; then
    printf '[hyperp-staging] deployment attempt: ' >&2
    cat "${DEPLOYMENT_ATTEMPT_FILE}" >&2 || true
  fi
}

fail() {
  printf '[hyperp-staging] deployment failed during %s: %s\n' \
    "${CURRENT_PHASE}" "$*" >&2
  safe_diagnostics
  exit 1
}

on_error() {
  local exit_code="$1"
  local line_number="$2"

  trap - ERR
  printf '[hyperp-staging] deployment failed during %s at script line %s (exit %s)\n' \
    "${CURRENT_PHASE}" "${line_number}" "${exit_code}" >&2
  safe_diagnostics
  exit "${exit_code}"
}

contains_service() {
  local expected_service="$1"
  local configured_service=""

  for configured_service in "${CONFIGURED_SERVICES[@]}"; do
    [[ "${configured_service}" == "${expected_service}" ]] && return 0
  done
  return 1
}

assert_main_contains_staging() {
  git -C "${REPO_DIR}" merge-base --is-ancestor "${EXPECTED_SHA}" origin/main \
    || fail "origin/main does not contain the staging revision"
}

assert_git_sync() {
  [[ "$(git -C "${REPO_DIR}" rev-parse HEAD)" == "${EXPECTED_SHA}" ]] \
    || fail "checkout does not match the expected revision"
  [[ "$(git -C "${REPO_DIR}" rev-parse origin/staging)" == "${EXPECTED_SHA}" ]] \
    || fail "origin/staging does not match the expected revision"
  [[ -z "$(git -C "${REPO_DIR}" status --porcelain --untracked-files=normal)" ]] \
    || fail "checkout is dirty"
  if [[ "${REVISION_CHECK_MODE}" == strict ]]; then
    assert_main_contains_staging
  fi
}

assert_runtime_contract() {
  local key=""
  local retired_key="WHATSADMIN_API_KEY"
  local ingestion_service=""
  local expected_setting=""
  local setting_name=""
  local setting_value=""
  local resolved_service=""

  for key in \
    FUNDBOX_API_BASE_URL \
    FUNDBOX_API_USERNAME \
    FUNDBOX_API_PASSWORD \
    FUNDBOX_API_PAGE_SIZE \
    FUNDBOX_API_TIMEOUT_SECONDS \
    FUNDBOX_API_MAX_ATTEMPTS \
    FUNDBOX_API_OVERLAP_SECONDS \
    GPT_API_BASE_URL \
    GPT_API_KEY \
    GPT_DEFAULT_MODEL \
    WHATSADMIN_API_BASE_URL \
    WHATSADMIN_EKO_API_KEY \
    WHATSADMIN_SPEEDZONE_API_KEY \
    WHATSADMIN_EKO_ENABLED \
    WHATSADMIN_SPEEDZONE_ENABLED \
    WHATSADMIN_LEGACY_ENTITY \
    WHATSADMIN_API_PAGE_SIZE \
    WHATSADMIN_API_TIMEOUT_SECONDS \
    WHATSADMIN_API_MAX_ATTEMPTS \
    WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS
  do
    grep -Eq "^[[:space:]]+${key}:" "${COMPOSE_FILE}" \
      || fail "staging Compose is missing runtime ingestion variable ${key}"
  done

  if grep -Eq "^[[:space:]]+${retired_key}:" "${COMPOSE_FILE}"; then
    fail "staging Compose still forwards retired variable ${retired_key}"
  fi

  export WHATSADMIN_API_PAGE_SIZE=25
  export WHATSADMIN_API_TIMEOUT_SECONDS=120
  export WHATSADMIN_API_MAX_ATTEMPTS=5
  export WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS=1

  mapfile -t CONFIGURED_SERVICES < <("${COMPOSE[@]}" config --services) \
    || fail "could not resolve Compose services"
  (( ${#CONFIGURED_SERVICES[@]} > 0 )) || fail "Compose configuration has no services"
  for key in "${EXPECTED_SERVICES[@]}"; do
    contains_service "${key}" || fail "Compose configuration is missing required service ${key}"
  done

  for ingestion_service in ingestion-worker lifecycle-worker beat; do
    resolved_service="$("${COMPOSE[@]}" config "${ingestion_service}")" \
      || fail "could not resolve ${ingestion_service} configuration"
    for expected_setting in \
      WHATSADMIN_API_PAGE_SIZE=25 \
      WHATSADMIN_API_TIMEOUT_SECONDS=120 \
      WHATSADMIN_API_MAX_ATTEMPTS=5 \
      WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS=1
    do
      setting_name="${expected_setting%%=*}"
      setting_value="${expected_setting#*=}"
      grep -Eq \
        "^[[:space:]]+${setting_name}: ['\"]?${setting_value}(\\.0)?['\"]?$" \
        <<< "${resolved_service}" \
        || fail "resolved ${ingestion_service} config did not apply ${expected_setting}"
    done
  done
}

assert_all_configured_services_running() {
  local configured_service=""
  local running_services=""

  running_services="$("${COMPOSE[@]}" ps --status running --services)" \
    || fail "could not list running Compose services"
  for configured_service in "${CONFIGURED_SERVICES[@]}"; do
    if [[ "${configured_service}" == lifecycle-worker && "${LIFECYCLE_PAUSED}" == true ]]; then
      continue
    fi
    grep -Fxq "${configured_service}" <<< "${running_services}" \
      || fail "${configured_service} is not running after deployment"
  done
}

assert_healthy() {
  local service="$1"
  local container_id=""
  local health=""

  container_id="$("${COMPOSE[@]}" ps -q "${service}")"
  [[ -n "${container_id}" ]] || fail "${service} has no container after deployment"
  health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "${container_id}"
  )"
  [[ "${health}" == healthy ]] || fail "${service} health is ${health}"
}

wait_service_stable() {
  local service="$1"
  local container_id=""
  local stable_checks=0
  local attempt=0

  container_id="$("${COMPOSE[@]}" ps -q "${service}")"
  [[ -n "${container_id}" ]] || fail "${service} has no container after deployment"
  for attempt in {1..12}; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "${container_id}")" == true ]]; then
      stable_checks=$((stable_checks + 1))
      if (( stable_checks >= 6 )); then
        printf '[hyperp-staging] service remained running: %s\n' "${service}"
        return 0
      fi
    else
      stable_checks=0
    fi
    sleep 5
  done
  fail "${service} did not remain running"
}

assert_internal_api_health() {
  "${COMPOSE[@]}" exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:3000/health', timeout=10).read()" </dev/null \
    || fail "internal API health request failed"
}

assert_external_health() {
  local health_url="$1"
  local body=""
  local attempt=0

  for attempt in {1..10}; do
    body="$(curl -fsS --max-time 15 "${health_url}" 2>/dev/null || true)"
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<< "${body}"; then
      printf '[hyperp-staging] external health check passed on attempt %s\n' "${attempt}"
      return 0
    fi
    sleep 5
  done
  fail "external staging health check failed"
}

write_deployed_revision() {
  local revision_tmp=""

  mkdir -p "$(dirname "${DEPLOYED_REVISION_FILE}")"
  revision_tmp="${DEPLOYED_REVISION_FILE}.tmp.$$"
  printf '%s\n' "${EXPECTED_SHA}" > "${revision_tmp}"
  mv -f "${revision_tmp}" "${DEPLOYED_REVISION_FILE}"
  [[ "$(cat "${DEPLOYED_REVISION_FILE}")" == "${EXPECTED_SHA}" ]] \
    || fail "deployed revision record does not match the expected revision"
  rm -f "${DEPLOYMENT_ATTEMPT_FILE}"
}

deployment_base_revision() {
  local before_sha="$1"
  local attempt_expected=""
  local attempt_base=""
  local deployment_base=""
  local attempt_tmp=""

  mkdir -p "$(dirname "${DEPLOYED_REVISION_FILE}")"
  if [[ "${REVISION_CHECK_MODE}" == skip ]]; then
    printf '%s\n' '[hyperp-staging] skipping persisted revision history checks for staging' >&2
    deployment_base="${before_sha}"
  elif [[ -f "${DEPLOYMENT_ATTEMPT_FILE}" ]]; then
    read -r attempt_expected attempt_base < "${DEPLOYMENT_ATTEMPT_FILE}" \
      || fail "deployment attempt record is malformed"
    if [[ "${attempt_expected}" == "${EXPECTED_SHA}" ]]; then
      deployment_base="${attempt_base}"
    elif [[ -f "${DEPLOYED_REVISION_FILE}" ]]; then
      deployment_base="$(cat "${DEPLOYED_REVISION_FILE}")"
    else
      fail "stale deployment attempt exists without a successful revision record"
    fi
  elif [[ -f "${DEPLOYED_REVISION_FILE}" ]]; then
    deployment_base="$(cat "${DEPLOYED_REVISION_FILE}")"
  else
    deployment_base="${before_sha}"
  fi

  [[ "${deployment_base}" =~ ^[0-9a-f]{40}$ ]] \
    || fail "deployment base revision is invalid"
  git -C "${REPO_DIR}" cat-file -e "${deployment_base}^{commit}" \
    || fail "deployment base revision is not available locally"
  git -C "${REPO_DIR}" merge-base --is-ancestor "${deployment_base}" "${EXPECTED_SHA}" \
    || fail "deployment base cannot advance to the expected revision"

  attempt_tmp="${DEPLOYMENT_ATTEMPT_FILE}.tmp.$$"
  printf '%s %s\n' "${EXPECTED_SHA}" "${deployment_base}" > "${attempt_tmp}"
  mv -f "${attempt_tmp}" "${DEPLOYMENT_ATTEMPT_FILE}"
  printf '%s\n' "${deployment_base}"
}

trap 'on_error $? $LINENO' ERR

[[ "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "expected commit SHA is invalid"
[[ "${REVISION_CHECK_MODE}" == strict || "${REVISION_CHECK_MODE}" == skip ]] \
  || fail "revision check mode must be strict or skip"
command -v base64 >/dev/null || fail "base64 is not installed"
command -v curl >/dev/null || fail "curl is not installed"
command -v docker >/dev/null || fail "docker is not installed"
command -v flock >/dev/null || fail "flock is not installed"
command -v git >/dev/null || fail "git is not installed"
[[ -d "${STAGING_DIR}" ]] || fail "staging directory is missing at ${STAGING_DIR}"
docker compose version --format '{{json .}}' >/dev/null 2>&1 \
  || fail "Docker Compose v2 is not available"

HEALTH_URL="$(printf '%s' "${HEALTH_URL_B64}" | base64 -d)" \
  || fail "staging health URL is not valid base64"
[[ "${HEALTH_URL}" =~ ^https?:// ]] || fail "staging health URL must use HTTP or HTTPS"

CURRENT_PHASE="acquiring deployment lock"
exec 9>"${LOCK_FILE}"
flock -w 300 9 || fail "timed out waiting for another HyperP staging deployment"

CURRENT_PHASE="checking staging checkout"
REPO_DIR="$(git -C "${STAGING_DIR}" rev-parse --show-toplevel 2>/dev/null)" \
  || fail "staging directory is not inside a Git checkout"
COMPOSE_FILE="${REPO_DIR}/.docker/staging/docker-compose.yml"
LIFECYCLE_PAUSE_MARKER="${REPO_DIR}/.lifecycle-worker-paused"
COMPOSE=(docker compose -p hyperp-ada-asia -f "${COMPOSE_FILE}")
[[ -f "${COMPOSE_FILE}" ]] || fail "Compose file is missing at ${COMPOSE_FILE}"
[[ -x "${REPO_DIR}/scripts/lifecycle-worker-deploy-guard.sh" ]] \
  || fail "lifecycle deployment guard is missing or not executable"
[[ "$(git -C "${REPO_DIR}" branch --show-current)" == staging ]] \
  || fail "staging checkout is not on staging"
[[ -z "$(git -C "${REPO_DIR}" status --porcelain --untracked-files=normal)" ]] \
  || fail "staging checkout is dirty"
git -C "${REPO_DIR}" remote get-url origin >/dev/null 2>&1 \
  || fail "staging checkout has no origin remote"

CURRENT_PHASE="fetching the expected revision"
git -C "${REPO_DIR}" fetch --quiet --prune origin \
  || fail "could not fetch origin"
[[ "$(git -C "${REPO_DIR}" rev-parse origin/staging)" == "${EXPECTED_SHA}" ]] \
  || fail "origin/staging does not match the pipeline revision"
if [[ "${REVISION_CHECK_MODE}" == strict ]]; then
  assert_main_contains_staging
else
  printf '%s\n' '[hyperp-staging] skipping origin/main ancestry check for staging' >&2
fi
git -C "${REPO_DIR}" merge-base --is-ancestor HEAD "${EXPECTED_SHA}" \
  || fail "staging checkout cannot fast-forward to the pipeline revision"
BEFORE_SHA="$(git -C "${REPO_DIR}" rev-parse HEAD)"
DEPLOYMENT_BASE_SHA="$(deployment_base_revision "${BEFORE_SHA}")"
git -C "${REPO_DIR}" merge --ff-only "${EXPECTED_SHA}"
assert_git_sync

CURRENT_PHASE="validating the Compose contract"
assert_runtime_contract

if [[ -f "${LIFECYCLE_PAUSE_MARKER}" ]]; then
  LIFECYCLE_PAUSED=true
  printf '%s\n' '[hyperp-staging] lifecycle pause marker is present; preserving pause'
  "${COMPOSE[@]}" stop lifecycle-worker
fi

CURRENT_PHASE="planning selective rebuild"
ALL_SERVICES=(api frontend2 ingestion-worker lifecycle-worker beat)
declare -A BUILD_NEEDED=()
declare -A RECREATE_NEEDED=()
RESTART_WEB=false
CHANGED_FILES="$(
  git -C "${REPO_DIR}" diff --name-only "${DEPLOYMENT_BASE_SHA}" "${EXPECTED_SHA}" || true
)"
printf '[hyperp-staging] changed files in %s..%s:\n%s\n' \
  "${DEPLOYMENT_BASE_SHA:0:8}" "${EXPECTED_SHA:0:8}" "${CHANGED_FILES:-<none>}"
while IFS= read -r changed_file; do
  [[ -n "${changed_file}" ]] || continue
  case "${changed_file}" in
    services/api/src/*|services/api/Dockerfile)
      BUILD_NEEDED[api]=1
      RECREATE_NEEDED[api]=1
      ;;
    services/frontend2/src/*|services/frontend2/public/*|\
      services/frontend2/Dockerfile|services/frontend2/package.json|\
      services/frontend2/package-lock.json|services/frontend2/next.config.ts|\
      services/frontend2/tsconfig.json)
      BUILD_NEEDED[frontend2]=1
      RECREATE_NEEDED[frontend2]=1
      ;;
    services/ingestion/src/*|services/ingestion/Dockerfile|infra/neo4j/init.cypher)
      BUILD_NEEDED[ingestion-worker]=1
      BUILD_NEEDED[lifecycle-worker]=1
      BUILD_NEEDED[beat]=1
      RECREATE_NEEDED[ingestion-worker]=1
      RECREATE_NEEDED[lifecycle-worker]=1
      RECREATE_NEEDED[beat]=1
      ;;
    pyproject.toml|uv.lock|services/api/pyproject.toml|services/ingestion/pyproject.toml)
      BUILD_NEEDED[api]=1
      BUILD_NEEDED[ingestion-worker]=1
      BUILD_NEEDED[lifecycle-worker]=1
      BUILD_NEEDED[beat]=1
      RECREATE_NEEDED[api]=1
      RECREATE_NEEDED[ingestion-worker]=1
      RECREATE_NEEDED[lifecycle-worker]=1
      RECREATE_NEEDED[beat]=1
      ;;
    .docker/staging/*|docker-compose.yml)
      for service in "${ALL_SERVICES[@]}"; do
        RECREATE_NEEDED["${service}"]=1
      done
      RESTART_WEB=true
      ;;
    services/nginx/*)
      RESTART_WEB=true
      ;;
  esac
done <<< "${CHANGED_FILES}"

BUILD_SERVICE_ARRAY=()
RECREATE_SERVICE_INPUT=()
for service in "${ALL_SERVICES[@]}"; do
  [[ -n "${BUILD_NEEDED[${service}]:-}" ]] && BUILD_SERVICE_ARRAY+=("${service}")
  [[ -n "${RECREATE_NEEDED[${service}]:-}" ]] && RECREATE_SERVICE_INPUT+=("${service}")
done

eval "$(
  "${REPO_DIR}/scripts/lifecycle-worker-deploy-guard.sh" \
    plan "${LIFECYCLE_PAUSED}" "${RECREATE_SERVICE_INPUT[@]}"
)"
read -r -a RECREATE_SERVICE_ARRAY <<< "${RECREATE_SERVICES}"
if [[ " ${RECREATE_SERVICES} " == *" api "* || \
  " ${RECREATE_SERVICES} " == *" frontend2 "* ]]; then
  RESTART_WEB=true
fi

CURRENT_PHASE="building and recreating changed services"
if (( ${#BUILD_SERVICE_ARRAY[@]} == 0 )); then
  printf '%s\n' '[hyperp-staging] no service code changed; skipping image builds'
else
  printf '[hyperp-staging] rebuilding services with code changes: %s\n' \
    "${BUILD_SERVICE_ARRAY[*]}"
  "${COMPOSE[@]}" build "${BUILD_SERVICE_ARRAY[@]}"
fi

if [[ " ${RECREATE_SERVICES} " == *" api "* ]]; then
  "${COMPOSE[@]}" run --rm --no-deps ingestion-worker \
    python -m src.person_completeness_control check
  "${COMPOSE[@]}" run --rm --no-deps ingestion-worker \
    python -m src.crm_deal_count_control check
fi

if (( ${#RECREATE_SERVICE_ARRAY[@]} > 0 )); then
  printf '[hyperp-staging] recreating changed services: %s\n' "${RECREATE_SERVICES}"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "${RECREATE_SERVICE_ARRAY[@]}"
  if [[ " ${RECREATE_SERVICES} " == *" api "* ]]; then
    "${COMPOSE[@]}" run --rm --no-deps ingestion-worker \
      python -m src.person_completeness_control check
    "${COMPOSE[@]}" run --rm --no-deps ingestion-worker \
      python -m src.crm_deal_count_control check
  fi
else
  printf '%s\n' '[hyperp-staging] no service configuration or code changed; skipping recreation'
fi

if [[ "${RESTART_WEB}" == true ]]; then
  "${COMPOSE[@]}" restart web
else
  printf '%s\n' '[hyperp-staging] web dependencies unchanged; skipping web restart'
fi

CURRENT_PHASE="verifying deployed services"
assert_all_configured_services_running
assert_healthy neo4j
assert_healthy redis
wait_service_stable ingestion-worker
if [[ "${LIFECYCLE_PAUSED}" == true ]]; then
  STAGING_COMPOSE_PROJECT=hyperp-ada-asia \
    "${REPO_DIR}/scripts/lifecycle-worker-deploy-guard.sh" \
    verify-paused "${COMPOSE_FILE}"
else
  wait_service_stable lifecycle-worker
fi
wait_service_stable beat
assert_internal_api_health
assert_external_health "${HEALTH_URL}"
assert_git_sync
write_deployed_revision

git -C "${REPO_DIR}" status --short --branch
"${COMPOSE[@]}" ps
printf '[hyperp-staging] deployed revision %s successfully\n' "${EXPECTED_SHA}"
