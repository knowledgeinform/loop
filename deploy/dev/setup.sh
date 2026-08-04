#!/usr/bin/env bash
#
# One-time setup for the LOOP dev environment. Rootless: everything lands under
# ~/loop-dev, nothing needs sudo, Docker, or a group membership.
#
#   deploy/dev/setup.sh
#
# This exists because the deploying account is not in the `docker` group and
# cannot get in without an admin -- and docker-group membership is effectively
# root on a host that also serves production, so it is not worth requesting. CHAOS is already
# deployed this way (node + ttyd + cloudflared, all userspace); LOOP dev follows
# the same pattern.
#
# Installs:
#   .venv/            Python 3.11 virtualenv with LOOP's requirements
#   mongodb/          MongoDB 8.0.4 binaries, unpacked from the official tarball
#   var/mongo/        the dev database (empty until clone-prod-to-dev.sh runs)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MONGO_VERSION="${MONGO_VERSION:-8.0.4}"
MONGO_BUILD="mongodb-linux-x86_64-ubuntu2204-${MONGO_VERSION}"
PY="${PY:-python3.11}"

say() { printf '\n=== %s ===\n' "$1"; }

say "1/4  Check prerequisites"
command -v "$PY" >/dev/null || { echo "  $PY not found" >&2; exit 1; }
echo "  python : $("$PY" --version)"
# mongodump/mongosh talk to BOTH prod (read-only, for cloning) and dev.
for c in mongosh mongodump mongorestore; do
  command -v "$c" >/dev/null || { echo "  $c not found on PATH" >&2; exit 1; }
done
echo "  mongo tools : present"

say "2/4  Python virtualenv"
if [[ ! -d "$ROOT/.venv" ]]; then
  "$PY" -m venv "$ROOT/.venv"
  echo "  created .venv"
fi
# --no-cache-dir keeps the footprint down; /home is at 88%.
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/.venv/bin/pip" install --quiet --no-cache-dir -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/pip" install --quiet --no-cache-dir gunicorn
echo "  installed requirements.txt + gunicorn"

say "3/4  MongoDB server binaries"
if [[ ! -x "$ROOT/mongodb/bin/mongod" ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "  downloading $MONGO_BUILD ..."
  curl -fsSL -o "$tmp/mongo.tgz" \
    "https://fastdl.mongodb.org/linux/${MONGO_BUILD}.tgz"
  tar xzf "$tmp/mongo.tgz" -C "$tmp"
  mkdir -p "$ROOT/mongodb"
  cp -a "$tmp/$MONGO_BUILD/." "$ROOT/mongodb/"
  echo "  installed $("$ROOT/mongodb/bin/mongod" --version | head -1)"
else
  echo "  already present: $("$ROOT/mongodb/bin/mongod" --version | head -1)"
fi

say "4/4  Directories"
mkdir -p "$ROOT/var/mongo" "$ROOT/var/log" "$ROOT/var/run" \
         "$ROOT/var/media" "$ROOT/var/raw-uploads" "$ROOT/var/static" "$ROOT/var/sqlite"
if [[ ! -f "$ROOT/deploy/dev/.env.dev" ]]; then
  # A dev key must never be prod's: a leaked one would otherwise forge sessions.
  printf 'DEV_SECRET_KEY=%s\n' \
    "$("$ROOT/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(50))')" \
    > "$ROOT/deploy/dev/.env.dev"
  chmod 600 "$ROOT/deploy/dev/.env.dev"
  echo "  generated deploy/dev/.env.dev with a fresh SECRET_KEY"
fi
echo "  var/ ready"

cat <<EOF

Setup complete.

  start        deploy/dev/serve.sh
  clone data   deploy/dev/clone-prod-to-dev.sh
EOF
