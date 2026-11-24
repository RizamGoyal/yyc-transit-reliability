#!/usr/bin/env bash
# Install (or update) the collector service, nightly backup timer and heartbeat timer for the current user.
# Run from the repo folder:  bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")"
for unit in collector.service backup.service backup.timer heartbeat.service heartbeat.timer; do
  sed "s/__USER__/$USER/g" "$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
  echo "installed /etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now collector.service
sudo systemctl enable --now backup.timer
sudo systemctl enable --now heartbeat.timer
echo "done: collector running, backup + heartbeat timers scheduled"
systemctl list-timers backup.timer heartbeat.timer --no-pager
