#!/usr/bin/env bash
# Nightly backup of irreplaceable data to Google Cloud Storage.
#   - GPS files and schedule versions: copied with rsync (only new/changed files upload)
#   - polls, alerts, schedule-version records: exported to a small meta.db and uploaded
# Trip predictions are NOT backed up: they're a secondary cross-check and would exceed free storage.
# Remote files are never deleted, so a lost local disk can't wipe the backup.
set -euo pipefail
cd "$(dirname "$0")/.."

BUCKET="${BACKUP_BUCKET:?Set BACKUP_BUCKET in .env}"
DATA_DIR="${DATA_DIR:-data}"
mkdir -p "$DATA_DIR/backup"

.venv/bin/python scripts/export_meta.py "$DATA_DIR/collector.db" "$DATA_DIR/backup/meta.db"

gcloud storage rsync --recursive "$DATA_DIR/positions" "gs://$BUCKET/positions"
gcloud storage rsync --recursive "$DATA_DIR/static" "gs://$BUCKET/static"
gcloud storage cp "$DATA_DIR/backup/meta.db" "gs://$BUCKET/meta.db"

echo "backup ok $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Tell healthchecks.io the backup finished. If any step above failed, set -e stops
# the script before this line, no ping is sent, and healthchecks.io emails you.
if [[ -n "${BACKUP_HEALTHCHECK_URL:-}" ]]; then
  curl -fsS -m 10 --retry 3 -o /dev/null "$BACKUP_HEALTHCHECK_URL" || echo "warning: backup ping failed"
fi
