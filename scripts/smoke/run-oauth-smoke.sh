#!/usr/bin/env bash
#
# Docker smoke test for the machine OAuth2 flow.
#
# Brings the stack up (if needed), waits for the API to be healthy, then runs
# scripts/smoke/oauth_smoke.py INSIDE the api container so it can both reach
# nginx over the docker network and call the service layer directly. Exits
# non-zero if any assertion fails.
#
# Usage:
#   scripts/smoke/run-oauth-smoke.sh             # rebuild api + up + wait + smoke
#   SKIP_BUILD=1 scripts/smoke/run-oauth-smoke.sh # up without rebuilding the api image
#   SKIP_UP=1 scripts/smoke/run-oauth-smoke.sh    # assume stack already running as-is
#
# The api image is rebuilt --no-cache by default: the OAuth source is baked into
# the image at build time, so without a rebuild the smoke would silently test
# stale code. SKIP_BUILD/SKIP_UP are for fast re-runs against a known-current stack.
#
set -euo pipefail

cd "$(dirname "$0")/../.."

SMOKE_SCRIPT="scripts/smoke/oauth_smoke.py"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-150}"

if [[ "${SKIP_UP:-0}" != "1" ]]; then
  if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    echo ">> Rebuilding api image (--no-cache) so the smoke tests current source…"
    docker compose build --no-cache api
  fi
  echo ">> Starting stack (docker compose up -d)…"
  docker compose up -d
fi

# Derive the host port nginx is published on (NGINX_PORT override; default 80)
# rather than assuming :80, so this works regardless of the local .env.
web_addr="$(docker compose port web 80 2>/dev/null | head -1)"
web_port="${web_addr##*:}"
web_port="${web_port:-80}"
HEALTH_URL="http://localhost:${web_port}/api/health"

echo ">> Waiting for API health at ${HEALTH_URL} (up to ${HEALTH_TIMEOUT}s)…"
for ((attempt = 1; attempt <= HEALTH_TIMEOUT; attempt++)); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    echo ">> API healthy after ${attempt}s."
    break
  fi
  if [[ "${attempt}" == "${HEALTH_TIMEOUT}" ]]; then
    echo "!! API did not become healthy within ${HEALTH_TIMEOUT}s." >&2
    docker compose logs --tail 50 api >&2 || true
    exit 1
  fi
  sleep 1
done

echo ">> Running OAuth smoke flow inside the api container…"
# -T disables pseudo-TTY so stdin piping works; the script imports src.* from
# the container WORKDIR (/app/services/api).
docker compose exec -T api python - < "${SMOKE_SCRIPT}"
