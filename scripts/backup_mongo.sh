#!/usr/bin/env bash
set -euo pipefail

ROOT="${LOOP_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${LOOP_BACKUP_DIR:-$ROOT/backups}"
PYTHON_BIN="${LOOP_PYTHON_BIN:-python}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
mkdir -p "$BACKUP_DIR"
cd "$ROOT"

# The mongodump archives are a fast-restore convenience, not the record of
# truth: they are opaque binaries tied to a compatible mongod. The JSON archive
# under ARCHIVE_ROOT is what actually needs to survive, and it is what the
# offsite backup should cover. Check it agrees with the database first, so a
# silent divergence surfaces here rather than during a restore.
"$PYTHON_BIN" manage.py loop_archive verify || echo "WARNING: JSON archive diverges from MongoDB; run 'manage.py loop_archive verify' for detail"

"$PYTHON_BIN" manage.py mongo_admin backup-run --archive "$BACKUP_DIR/loop-$TS.archive.gz" --yes
"$PYTHON_BIN" manage.py mongo_admin backup-raw --archive "$BACKUP_DIR/loop_raw-$TS.archive.gz" --yes

# Keep 7 days of backups.
find "$BACKUP_DIR" -name "loop-*.archive.gz" -mtime +7 -delete
find "$BACKUP_DIR" -name "loop_raw-*.archive.gz" -mtime +7 -delete
