#!/usr/bin/env bash
# Preserve an operator-intended lifecycle-consumer pause across staging deploys.
set -euo pipefail

command_name=${1:-}
repo_dir=${STAGING_REPO_DIR:-$(pwd)}
compose_file=${STAGING_COMPOSE_FILE:-.docker/staging/docker-compose.yml}
compose_project=${STAGING_COMPOSE_PROJECT:-hyperp-ada-asia}
marker="$repo_dir/.lifecycle-worker-paused"

cd "$repo_dir"
compose=(docker compose -p "$compose_project" -f "$compose_file")

consumer_container_id() {
  "${compose[@]}" ps -q lifecycle-worker
}

case "$command_name" in
  pause)
    "${compose[@]}" stop lifecycle-worker
    container_id=$(consumer_container_id)
    if [[ -n "$container_id" ]]; then
      echo "Lifecycle worker is still running after stop; pause marker not created" >&2
      exit 1
    fi
    marker_tmp="${marker}.tmp.$$"
    : > "$marker_tmp"
    mv -f "$marker_tmp" "$marker"
    echo "Lifecycle worker paused; marker: $marker"
    ;;
  resume)
    "${compose[@]}" up -d --no-deps lifecycle-worker
    container_id=$(consumer_container_id)
    if [[ -z "$container_id" ]]; then
      echo "Lifecycle worker did not start; pause marker preserved" >&2
      exit 1
    fi
    rm -f "$marker"
    echo "Lifecycle worker resumed"
    ;;
  status)
    if [[ -f "$marker" ]]; then
      echo "pause_marker=present"
    else
      echo "pause_marker=absent"
    fi
    container_id=$(consumer_container_id)
    if [[ -n "$container_id" ]]; then
      echo "consumer_running=true"
    else
      echo "consumer_running=false"
    fi
    ;;
  *)
    echo "Usage: $0 {pause|resume|status}" >&2
    exit 2
    ;;
esac
