#!/usr/bin/env bash
#
# Refresh the dev environment with a copy of production data. Rootless: no
# Docker, no sudo.
#
#   deploy/dev/clone-prod-to-dev.sh              # everything
#   deploy/dev/clone-prod-to-dev.sh --no-users   # skip accounts
#   deploy/dev/clone-prod-to-dev.sh --no-files   # database only
#   deploy/dev/clone-prod-to-dev.sh --keep-emails
#
# READ-ONLY WITH RESPECT TO PRODUCTION. It runs mongodump against prod's Mongo
# over TCP and copies files out of the prod data directory. It never writes to
# prod, never restarts it, and never touches the prod containers.
#
# HOW IT REACHES PROD WITHOUT DOCKER: prod's Mongo has no published host port,
# but its container sits on a Docker bridge that IS routable from the host, so
# mongodump can dial it directly. The address is discovered by probing the
# bridges rather than hardcoded, because a container restart reassigns it.
#
# WHAT GETS COPIED:
#   loop, loop_raw   ~1.1 GB   the catalogue
#   media              39 MB   XRD files, plots, batch archives and manifests
#   raw-uploads       6.2 MB   upload staging directories
#   users            240 KB    accounts (SQLite)
#
# Skipped: `local` (~13 GB of oplog -- replication bookkeeping, which is why the
# prod mongo dir is 14 GB but a faithful clone is ~1.2 GB), `admin`/`config`
# (server internals), staticfiles (regenerated), CHAOS (a different service).
#
# Copying media matters: without it, XRD plots and previously uploaded archives
# 404 in dev and records look broken in ways production is not.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROD_DATA_ROOT="${PROD_DATA_ROOT:-/srv/loop/data/LOOP}"
DEV_MONGO_PORT="${DEV_MONGO_PORT:-27019}"
PROD_MONGO_HOST="${PROD_MONGO_HOST:-}"   # set to skip discovery

CLONE_USERS=1
CLONE_FILES=1
SCRUB_EMAILS=1
for arg in "$@"; do
  case "$arg" in
    --no-users)    CLONE_USERS=0 ;;
    --no-files)    CLONE_FILES=0 ;;
    --keep-emails) SCRUB_EMAILS=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

WORKDIR="$(mktemp -d /tmp/loop-clone.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
say() { printf '\n=== %s ===\n' "$1"; }

say "1/6  Locate production Mongo"
if [[ -z "$PROD_MONGO_HOST" ]]; then
  for net in 172.21 172.18 172.17 172.19 172.20; do
    for h in $(seq 2 12); do
      cand="$net.0.$h"
      timeout 0.3 bash -c "echo > /dev/tcp/$cand/27017" 2>/dev/null || continue
      # Confirm it is really the LOOP catalogue before dumping anything.
      if mongosh "mongodb://$cand:27017/loop" --quiet \
           --eval 'db.recipes.countDocuments() >= 0' >/dev/null 2>&1; then
        PROD_MONGO_HOST="$cand"; break 2
      fi
    done
  done
fi
[[ -n "$PROD_MONGO_HOST" ]] || { echo "  could not find prod Mongo. Set PROD_MONGO_HOST." >&2; exit 1; }
echo "  prod mongo : $PROD_MONGO_HOST:27017"

mongosh "mongodb://127.0.0.1:$DEV_MONGO_PORT/loop" --quiet \
  --eval 'db.runCommand({ping:1}).ok' >/dev/null \
  || { echo "  dev mongo not running. Run deploy/dev/serve.sh first." >&2; exit 1; }
echo "  dev mongo  : 127.0.0.1:$DEV_MONGO_PORT"

say "2/6  Dump production catalogue"
for db in loop loop_raw; do
  mongodump --host "$PROD_MONGO_HOST" --port 27017 --db "$db" \
    --archive="$WORKDIR/$db.archive.gz" --gzip --quiet
  printf '  %-10s %s\n' "$db" "$(du -h "$WORKDIR/$db.archive.gz" | cut -f1)"
done

say "3/6  Restore into dev (--drop replaces dev; prod untouched)"
for db in loop loop_raw; do
  # --batchSize=50 is required, not tuning. Material documents average ~316 KB,
  # so mongorestore's default batch of 1000 builds a single wire message over
  # mongod's 48,000,000-byte limit. mongod rejects it as a ProtocolError and
  # closes the connection, and mongorestore reports only "broken pipe" -- the
  # restore stops around 5,000 of 35,576 documents while claiming 0 failures.
  # 50 x 316 KB is roughly 16 MB per batch, comfortably under the cap.
  #
  # Not --quiet: it hid this failure completely on the first two attempts.
  if ! mongorestore --host 127.0.0.1 --port "$DEV_MONGO_PORT" \
       --archive="$WORKDIR/$db.archive.gz" --gzip --drop --batchSize=50 \
       > "$WORKDIR/restore-$db.log" 2>&1; then
    echo "  !! restore of $db FAILED:" >&2
    tail -5 "$WORKDIR/restore-$db.log" | sed 's/^/     /' >&2
    exit 1
  fi
  echo "  restored $db ($(grep -oE '[0-9]+ document\(s\) restored successfully' \
    "$WORKDIR/restore-$db.log" | tail -1))"
done

if [[ "$CLONE_FILES" == "1" ]]; then
  say "4/6  Copy uploaded files"
  for pair in "media:$ROOT/var/media" "raw-uploads:$ROOT/var/raw-uploads"; do
    name="${pair%%:*}"; dest="${pair#*:}"
    src="$PROD_DATA_ROOT/$name"
    [[ -d "$src" ]] || { echo "  skip $name (not found)"; continue; }
    mkdir -p "$dest"
    # -a preserves timestamps so incremental re-runs are cheap.
    rsync -a --delete "$src/" "$dest/"
    printf '  %-12s %s  (%s files)\n' "$name" \
      "$(du -sh "$dest" | cut -f1)" "$(find "$dest" -type f | wc -l | tr -d ' ')"
  done
else
  say "4/6  Skipping files (--no-files)"
fi

if [[ "$CLONE_USERS" == "1" ]]; then
  say "5/6  Copy user accounts"
  mkdir -p "$ROOT/var/sqlite"
  cp "$PROD_DATA_ROOT/users/db.sqlite3" "$ROOT/var/sqlite/db.sqlite3"
  echo "  users db copied ($(du -h "$ROOT/var/sqlite/db.sqlite3" | cut -f1))"
  if [[ "$SCRUB_EMAILS" == "1" ]]; then
    # Defence in depth behind the console email backend: blank addresses mean
    # dev cannot reach a real person even if the backend is misconfigured.
    ( set -a; . "$ROOT/deploy/dev/.env.dev"; set +a
      DJANGO_SETTINGS_MODULE=loop.settings \
      DJANGO_SECRET_KEY="$DEV_SECRET_KEY" \
      SQLITE_PATH="$ROOT/var/sqlite/db.sqlite3" \
      MONGODB_URI="mongodb://127.0.0.1:$DEV_MONGO_PORT/loop" \
      MPLBACKEND=Agg \
      "$ROOT/.venv/bin/python" "$ROOT/manage.py" shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
print(f'  scrubbed {U.objects.exclude(email=\"\").update(email=\"\")} email addresses')
" )
  fi
else
  say "5/6  Skipping accounts (--no-users)"
fi

say "6/6  Verify"
mongosh "mongodb://127.0.0.1:$DEV_MONGO_PORT/loop" --quiet --eval '
["recipes","materials","ml_embeddings","doi_mappings"].forEach(function (c) {
  print("  " + String(db[c].countDocuments()).padStart(7) + "  " + c);
});
var lit = db.recipes.aggregate([
  {$unwind: "$literature"},
  {$group: {_id: "$literature.extracted_by", n: {$sum: 1}}},
  {$sort: {n: -1}}
]).toArray();
print("  literature entries by uploader:");
lit.forEach(function (r) { print("    " + String(r.n).padStart(5) + "  " + (r._id || "(blank)")); });
'

cat <<EOF

Done. Dev mirrors prod as of $(date -u '+%Y-%m-%d %H:%M UTC').

  restart dev   deploy/dev/serve.sh
  site          https://\${DEV_HOSTNAME:-localhost}/loop/

Production was not modified.
EOF
