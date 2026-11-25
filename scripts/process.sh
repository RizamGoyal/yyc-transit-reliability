#!/usr/bin/env bash
# Nightly processing: yesterday's service day, plus any of the last 7 days that are missing
# (e.g. the VM was off). Run by deploy/process.timer at low CPU/IO priority.
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python scripts/run_day.py --catch-up 7

# Tell healthchecks.io processing finished. If run_day failed, set -e stops the script
# before this line, no ping is sent, and healthchecks.io emails you.
if [[ -n "${PROCESS_HEALTHCHECK_URL:-}" ]]; then
  curl -fsS -m 10 --retry 3 -o /dev/null "$PROCESS_HEALTHCHECK_URL" || echo "warning: process ping failed"
fi
