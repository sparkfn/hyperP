#!/usr/bin/env bash
# Compatibility wrapper for the historical lifecycle-only operator surface.
set -euo pipefail

command_name=${1:-}
case "$command_name" in
  pause|resume)
    exec "$(dirname "$0")/worker-control.sh" "$command_name" lifecycle-worker
    ;;
  status)
    output=$("$(dirname "$0")/worker-control.sh" status lifecycle-worker)
    printf '%s\n' "$output" | grep -Ev '^service=lifecycle-worker$'
    ;;
  *)
    echo "Usage: $0 {pause|resume|status}" >&2
    exit 2
    ;;
esac
