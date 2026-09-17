#!/usr/bin/env bash
# Testable staging recreation planning and stopped-worker verification.
set -euo pipefail

action=${1:-}

is_paused() {
  local service=$1
  local comma_list=$2
  [[ ",$comma_list," == *",$service,"* ]]
}

case "$action" in
  plan)
    paused_services=${2:?comma-separated paused services are required}
    shift 2
    running_services=()
    stopped_services=()
    for service in "$@"; do
      if is_paused "$service" "$paused_services"; then
        stopped_services+=("$service")
      else
        running_services+=("$service")
      fi
    done
    printf 'RUNNING_RECREATE_SERVICES=%q\n' "${running_services[*]}"
    printf 'STOPPED_RECREATE_SERVICES=%q\n' "${stopped_services[*]}"
    ;;
  verify-paused)
    compose_file=${2:?compose file is required}
    shift 2
    compose_project=${STAGING_COMPOSE_PROJECT:-hyperp-ada-asia}
    compose=(env COMPOSE_PROFILES= docker compose -p "$compose_project" -f "$compose_file")
    for service in "$@"; do
      container_ids=$("${compose[@]}" ps -aq "$service")
      count=$(awk 'NF { count += 1 } END { print count + 0 }' <<< "$container_ids")
      if [[ "$count" -gt 1 ]]; then
        echo "Expected at most one $service container while paused." >&2
        exit 1
      fi
      if [[ "$count" -eq 1 ]]; then
        container_id=$(head -n 1 <<< "$container_ids")
        running=$(docker inspect -f '{{.State.Running}}' "$container_id")
        if [[ "$running" == true ]]; then
          echo "$service is running despite deliberate pause intent." >&2
          exit 1
        fi
      fi
    done
    echo "Paused worker containers are stopped."
    ;;
  *)
    echo "Usage: $0 {plan <paused-csv> [services...]|verify-paused <compose-file> [services...]}" >&2
    exit 2
    ;;
esac
