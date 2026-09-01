#!/usr/bin/env bash
# Testable staging deployment decisions and paused-worker verification.
set -euo pipefail

action=${1:-}
case "$action" in
  plan)
    paused=${2:?paused flag is required}
    shift 2
    build_services="$*"
    recreate_services="$build_services"
    if [[ "$paused" == true ]]; then
      recreate_services=""
      for service in $build_services; do
        if [[ "$service" != lifecycle-worker ]]; then
          recreate_services+="${recreate_services:+ }${service}"
        fi
      done
    fi
    printf 'BUILD_SERVICES=%q\n' "$build_services"
    printf 'RECREATE_SERVICES=%q\n' "$recreate_services"
    ;;
  verify-paused)
    compose_file=${2:?compose file is required}
    compose_project=${STAGING_COMPOSE_PROJECT:-hyperp-ada-asia}
    compose=(docker compose -p "$compose_project" -f "$compose_file")
    lifecycle_container_ids=$("${compose[@]}" ps -aq lifecycle-worker)
    lifecycle_container_count=$(
      awk 'NF { count += 1 } END { print count + 0 }' <<< "$lifecycle_container_ids"
    )
    if [[ "$lifecycle_container_count" -gt 1 ]]; then
      echo "Expected at most one lifecycle worker container while paused." >&2
      exit 1
    fi
    if [[ "$lifecycle_container_count" -eq 1 ]]; then
      container_id=$(head -n 1 <<< "$lifecycle_container_ids")
      container_running=$(docker inspect -f '{{.State.Running}}' "$container_id")
      if [[ "$container_running" == true ]]; then
        echo "Lifecycle worker is running despite the deliberate pause marker." >&2
        exit 1
      fi
    fi
    echo "Lifecycle worker is stopped; deliberate pause preserved."
    ;;
  *)
    echo "Usage: $0 {plan <paused> [services...]|verify-paused <compose-file>}" >&2
    exit 2
    ;;
esac
