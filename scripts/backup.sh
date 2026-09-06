#!/usr/bin/env bash
# Hourly database snapshot. Keeps the newest 48 hourly copies (two days) plus one
# per day for 30 days. Run from cron or the systemd timer in deploy/.
set -euo pipefail
cd "$(dirname "$0")/.."
GQ="${GQ_BIN:-.venv/bin/gq}"
DATA_DIR="${GQ_DATA_DIR:-./data}"

snapshot=$("$GQ" backup --keep 48)
echo "backup: $snapshot"

# Daily copy: first snapshot of each day is kept for 30 days.
daily_dir="$DATA_DIR/backups/daily"
mkdir -p "$daily_dir"
today=$(date -u +%Y%m%d)
if [ -z "$(ls "$daily_dir"/giveaway-"$today"* 2>/dev/null)" ]; then
  cp "$snapshot" "$daily_dir/"
fi
find "$daily_dir" -name 'giveaway-*.sqlite3' -mtime +30 -delete

# Optional off-site sync: set BACKUP_RCLONE_REMOTE=remote:bucket/path
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  rclone sync "$DATA_DIR/backups" "$BACKUP_RCLONE_REMOTE" --quiet
fi
