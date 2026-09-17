#!/usr/bin/env bash
# Canonical staging pause intent for the three bounded worker services.
set -euo pipefail

readonly SERVICES=(ingestion-worker beat lifecycle-worker)
readonly LOCK_FILE=${HYPERP_DEPLOY_LOCK_FILE:-/tmp/hyperp-staging-deploy.lock}
command_name=${1:-}
repo_dir=${STAGING_REPO_DIR:-$(pwd)}
compose_file=${STAGING_COMPOSE_FILE:-.docker/staging/docker-compose.yml}
compose_project=${STAGING_COMPOSE_PROJECT:-hyperp-ada-asia}
state_dir=""
policy_helper=""
compose=()

fail() {
  printf '%s\n' "worker control: $*" >&2
  exit 1
}

is_service() {
  local candidate=${1:-}
  local service=""
  for service in "${SERVICES[@]}"; do
    [[ "$service" == "$candidate" ]] && return 0
  done
  return 1
}

acquire_lock() {
  [[ ${HYPERP_DEPLOY_LOCK_HELD:-false} == true ]] && return 0
  command -v flock >/dev/null || fail "flock is required"
  exec 9>"$LOCK_FILE"
  flock -w 300 9 || fail "timed out waiting for staging deployment/control lock"
}

initialize() {
  repo_dir=$(cd "$repo_dir" && pwd -P)
  cd "$repo_dir"
  git rev-parse --show-toplevel >/dev/null 2>&1 || fail "repository checkout is required"
  state_dir="$repo_dir/.docker/staging/data/worker-pauses"
  [[ ! -L "$repo_dir/.docker" && ! -L "$repo_dir/.docker/staging" ]] \
    || fail "pause state path is symlinked"
  mkdir -p "$state_dir"
  [[ ! -L "$state_dir" && -d "$state_dir" ]] || fail "pause state directory is unsafe"
  policy_helper="$repo_dir/scripts/deploy/scheduled_ingestion_policy.py"
  compose=(env COMPOSE_PROFILES= docker compose -p "$compose_project" -f "$compose_file")
}

marker_path() {
  printf '%s/%s' "$state_dir" "$1"
}

assert_untracked() {
  local relative=$1
  local inspection_status=0

  if git ls-files --error-unmatch -- "$relative" >/dev/null 2>&1; then
    fail "pause marker must not be tracked"
  else
    inspection_status=$?
  fi
  if [[ "$inspection_status" -eq 1 ]]; then
    return 0
  fi
  fail "could not inspect whether pause marker is tracked"
}

assert_marker() {
  local service=$1
  local marker
  marker=$(marker_path "$service")
  assert_untracked ".docker/staging/data/worker-pauses/$service"
  [[ ! -e "$marker" && ! -L "$marker" ]] && return 0
  [[ -f "$marker" && ! -L "$marker" ]] || fail "pause marker for $service is malformed"
  [[ ! -s "$marker" ]] || fail "pause marker for $service is malformed"
}

write_marker() {
  local service=$1
  local marker temporary
  marker=$(marker_path "$service")
  assert_marker "$service"
  temporary="$state_dir/.${service}.tmp.$$"
  (umask 077; : > "$temporary")
  mv -f "$temporary" "$marker"
  assert_marker "$service"
}

clear_marker() {
  local service=$1
  local marker
  marker=$(marker_path "$service")
  assert_marker "$service"
  rm -f -- "$marker"
}

marker_present() {
  local service=$1
  local marker
  marker=$(marker_path "$service")
  assert_marker "$service"
  [[ -f "$marker" ]]
}

migrate_legacy() {
  local legacy="$repo_dir/.lifecycle-worker-paused"
  local status=""
  [[ ! -e "$legacy" && ! -L "$legacy" ]] && return 0
  [[ -f "$legacy" && ! -L "$legacy" && ! -s "$legacy" ]] \
    || fail "legacy lifecycle pause marker is malformed"
  assert_untracked ".lifecycle-worker-paused"
  status=$(git status --porcelain --untracked-files=normal)
  [[ "$status" == "?? .lifecycle-worker-paused" ]] \
    || fail "legacy marker migration requires no unrelated dirty state"
  if marker_present lifecycle-worker; then
    fail "legacy and canonical lifecycle pause markers both exist"
  fi
  write_marker lifecycle-worker
  rm -f -- "$legacy"
  printf '%s\n' "legacy lifecycle pause intent migrated"
}

consumer_running() {
  local service=$1
  local container_id

  if ! container_id=$("${compose[@]}" ps -q "$service"); then
    fail "could not inspect whether $service is running"
  fi
  [[ -n "$container_id" ]]
}

pause() {
  local service=$1
  write_marker "$service"
  "${compose[@]}" stop "$service" || fail "$service stop failed; pause marker retained"
  consumer_running "$service" && fail "$service is still running; pause marker retained"
  printf 'service=%s\npause_marker=present\nconsumer_running=false\n' "$service"
}

assert_resume_admitted() {
  local policy_probe=""
  local policy_prepare=""
  local policy_inspect=""

  command -v python3 >/dev/null || fail "python3 is required for resume admission"
  [[ -f "$policy_helper" && ! -L "$policy_helper" ]] \
    || fail "scheduled-ingestion policy helper is unavailable"
  if ! policy_probe="$(
    COMPOSE_PROFILES= python3 "$policy_helper" probe \
      --compose-file "$compose_file" --compose-project "$compose_project"
  )"; then
    fail "could not probe effective scheduled-ingestion policy before resume"
  fi
  eval "$policy_probe"
  if ! policy_prepare="$(
    COMPOSE_PROFILES= python3 "$policy_helper" prepare \
      --compose-file "$compose_file" --compose-project "$compose_project" \
      --repository-root "$repo_dir"
  )"; then
    fail "could not prepare effective scheduled-ingestion policy before resume"
  fi
  eval "$policy_prepare"
  if ! policy_inspect="$(
    COMPOSE_PROFILES= python3 "$policy_helper" inspect \
      --compose-file "$compose_file" --compose-project "$compose_project"
  )"; then
    fail "could not read back effective scheduled-ingestion policy before resume"
  fi
  eval "$policy_inspect"
  if [[ "${SCHEDULE_POLICY_ENABLED:-false}" == true ]]; then
    COMPOSE_PROFILES= python3 "$policy_helper" admit \
      --compose-file "$compose_file" --compose-project "$compose_project" \
      || fail "scheduled-ingestion policy closes worker resume outside the drain-safe window"
  fi
}

resume() {
  local service=$1
  marker_present "$service" || fail "$service has no deliberate pause marker to resume"
  assert_resume_admitted
  "${compose[@]}" up -d --no-deps "$service"
  consumer_running "$service" || fail "$service did not start; pause marker preserved"
  clear_marker "$service"
  printf 'service=%s\npause_marker=absent\nconsumer_running=true\n' "$service"
}

enforce() {
  local service=$1
  marker_present "$service" || fail "$service has no deliberate pause marker to enforce"
  "${compose[@]}" stop "$service" || fail "$service stop failed; pause marker retained"
  consumer_running "$service" && fail "$service remains running; pause marker retained"
  printf 'service=%s\npause_marker=present\nconsumer_running=false\n' "$service"
}

status_one() {
  local service=$1
  if marker_present "$service"; then
    printf 'service=%s\npause_marker=present\n' "$service"
  else
    printf 'service=%s\npause_marker=absent\n' "$service"
  fi
  if consumer_running "$service"; then
    printf '%s\n' "consumer_running=true"
  else
    printf '%s\n' "consumer_running=false"
  fi
}

intent() {
  local service=""
  local variable_name=""

  for service in "${SERVICES[@]}"; do
    variable_name=${service//-/_}
    variable_name=${variable_name^^}
    if marker_present "$service"; then
      printf '%s_PAUSED=true\n' "$variable_name"
    else
      printf '%s_PAUSED=false\n' "$variable_name"
    fi
  done
}

acquire_lock
initialize
if [[ "$command_name" != migrate-legacy ]]; then
  migrate_legacy
fi
case "$command_name" in
  pause|resume|enforce)
    service=${2:-}
    is_service "$service" \
      || fail "usage: $0 {pause|resume|enforce} {ingestion-worker|beat|lifecycle-worker}"
    "$command_name" "$service"
    ;;
  status)
    if [[ $# -eq 1 ]]; then
      for service in "${SERVICES[@]}"; do
        status_one "$service"
      done
    else
      service=${2:-}
      is_service "$service" || fail "usage: $0 status [ingestion-worker|beat|lifecycle-worker]"
      status_one "$service"
    fi
    ;;
  migrate-legacy)
    migrate_legacy
    ;;
  intent)
    intent
    ;;
  *)
    fail "usage: $0 {pause|resume|status} [ingestion-worker|beat|lifecycle-worker]"
    ;;
esac
