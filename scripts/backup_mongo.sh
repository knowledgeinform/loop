#!/usr/bin/env bash
set -euo pipefail

ROOT="/srv/loop/app"
BACKUP_DIR="/srv/loop/data/backups"
PYTHON="/home/www-s4e/miniconda3/envs/loop/bin/python"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
mkdir -p "$BACKUP_DIR"
cd "$ROOT"

"$PYTHON" manage.py mongo_admin backup-run --archive "$BACKUP_DIR/loop-$TS.archive.gz" --yes
"$PYTHON" manage.py mongo_admin backup-raw --archive "$BACKUP_DIR/loop_raw-$TS.archive.gz" --yes

# Keep 7 days of backups.
find "$BACKUP_DIR" -name "loop-*.archive.gz" -mtime +7 -delete
find "$BACKUP_DIR" -name "loop_raw-*.archive.gz" -mtime +7 -delete
