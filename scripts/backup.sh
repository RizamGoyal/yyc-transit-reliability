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
