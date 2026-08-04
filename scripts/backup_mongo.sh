#!/usr/bin/env bash
set -euo pipefail

ROOT="${LOOP_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${LOOP_BACKUP_DIR:-$ROOT/backups}"
PYTHON_BIN="${LOOP_PYTHON_BIN:-python}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
mkdir -p "$BACKUP_DIR"
cd "$ROOT"

"$PYTHON_BIN" manage.py mongo_admin backup-run --archive "$BACKUP_DIR/loop-$TS.archive.gz" --yes
"$PYTHON_BIN" manage.py mongo_admin backup-raw --archive "$BACKUP_DIR/loop_raw-$TS.archive.gz" --yes

# Keep 7 days of backups.
find "$BACKUP_DIR" -name "loop-*.archive.gz" -mtime +7 -delete
find "$BACKUP_DIR" -name "loop_raw-*.archive.gz" -mtime +7 -delete
