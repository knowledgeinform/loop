#!/usr/bin/env bash
#
# Start (or restart) the LOOP dev environment. Rootless -- three userspace
# processes, no Docker, no sudo.
#
#   deploy/dev/serve.sh            # start everything
#   deploy/dev/serve.sh stop       # stop everything
#   deploy/dev/serve.sh reload     # restart LOOP only (deploys)
#   deploy/dev/serve.sh status     # what is running
#
#   mongod    127.0.0.1:27019   dev database        var/mongo
#   gunicorn  127.0.0.1:8011    LOOP under /loop
#   http      127.0.0.1:8081    the landing page
#
# Every listener binds to loopback only. The Cloudflare tunnel is the sole way
# in from outside, and Cloudflare Access is what restricts who gets that far.
#
# Processes are started with setsid + nohup rather than `systemctl --user`
# because lingering is disabled for this account (Linger=no), so user units
# would be killed at logout. This is the same approach the CHAOS deployment
# uses, and it survives logout the same way.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN="$ROOT/var/run"
LOG="$ROOT/var/log"
VENV="$ROOT/.venv/bin"

# Sourced first: everything below may be overridden from it. Hostnames and the
# secret key live there, untracked, so the repo carries no deployment specifics.
[[ -f "$ROOT/deploy/dev/.env.dev" ]] && . "$ROOT/deploy/dev/.env.dev"

WEB_PORT="${DEV_WEB_PORT:-8011}"
MONGO_PORT="${DEV_MONGO_PORT:-27019}"
LANDING_PORT="${DEV_LANDING_PORT:-8081}"
HOSTNAME_PUBLIC="${DEV_HOSTNAME:-localhost}"
CHAOS_URL="${DEV_CHAOS_URL:-}"

export DJANGO_SETTINGS_MODULE=loop.settings
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS="$HOSTNAME_PUBLIC,localhost,127.0.0.1"
export SUBPATH=/loop DJANGO_SUBPATH=/loop
export DJANGO_SECRET_KEY="${DEV_SECRET_KEY:?run deploy/dev/setup.sh first}"
# Dev holds a copy of real accounts. Console backend means a stray password
# reset prints to a log file instead of reaching somebody's inbox.
export DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
# Same reason, one step further: /admin/ and /accounts/signup/ are dropped from
# the URL conf, so the cloned superuser hashes have no login form in front of
# them here. Today only Cloudflare Access keeps that reachable set small; this
# does not depend on the allow-list staying short. Set DEV_LOCKDOWN=0 in
# .env.dev to get the admin back for a debugging session.
export DEV_LOCKDOWN="${DEV_LOCKDOWN:-1}"
export MONGODB_URI="mongodb://127.0.0.1:$MONGO_PORT/loop"
export MONGODB_RAW_URI="mongodb://127.0.0.1:$MONGO_PORT/loop_raw"
export SQLITE_PATH="$ROOT/var/sqlite/db.sqlite3"
export MEDIA_ROOT="$ROOT/var/media"
export RAW_UPLOADS_ROOT="$ROOT/var/raw-uploads"
export STATIC_ROOT="$ROOT/var/static"
export HF_HOME="$ROOT/var/hf-cache"
export SYNTHESIS_LLM_ENABLED=False   # no outbound LLM calls, no spend
export MPLBACKEND=Agg

pidfile() { echo "$RUN/$1.pid"; }
alive()   { [[ -f "$(pidfile "$1")" ]] && kill -0 "$(cat "$(pidfile "$1")")" 2>/dev/null; }

# Every service runs inside deploy/dev/sandbox.sh, which hides the Cloudflare
# credentials, SSH keys, and production data from the confined process. The dev
# site runs the branch under test and faces the internet, so it is the least
# trustworthy code here; an RCE in it should not reach the account credential.
# Set DEV_NO_SANDBOX=1 only to debug the sandbox itself.
SANDBOX=("$ROOT/deploy/dev/sandbox.sh")
if [[ "${DEV_NO_SANDBOX:-0}" == "1" ]]; then
  SANDBOX=()
  echo "  !! DEV_NO_SANDBOX=1 -- services are NOT confined"
elif ! command -v bwrap >/dev/null; then
  echo "  !! bwrap not found -- services will NOT be confined" >&2
  SANDBOX=()
fi

# Each service gets its own process group via setsid, so stop can signal the
# whole tree -- bwrap wrapper, the service, and its workers -- with one kill.
#
# Two earlier approaches failed and are worth not repeating. Killing the recorded
# pid alone left the real service running, because bwrap's children survive their
# wrapper. Tagging the service with an environment variable and using `pkill -f`
# failed differently: `env VAR=1 bwrap ...` execs into bwrap, so the tag never
# appears in argv and matched nothing. Both produced the same silent failure --
# stop reported success, the old process kept the port, the new one died with
# EADDRINUSE, and the site went on serving stale code.
start_bg() {  # start_bg <name> <logfile> <cmd...>
  local name="$1" log="$2"; shift 2
  if alive "$name"; then echo "  $name already running (pid $(cat "$(pidfile "$name")"))"; return; fi
  setsid nohup "${SANDBOX[@]}" "$@" >>"$log" 2>&1 &
  local pid=$!
  echo "$pid" > "$(pidfile "$name")"
  echo "  started $name (pid $pid)"
}

stop_one() {  # stop_one <name> [port]
  local name="$1" port="${2:-}" killed=0 pid pgid
  if [[ -f "$(pidfile "$name")" ]]; then
    pid="$(cat "$(pidfile "$name")")"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [[ -n "$pgid" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null && killed=1   # negative pid == whole group
    fi
  fi
  rm -f "$(pidfile "$name")"

  # Confirm by port, not by exit status. A stop that returns while the socket is
  # still held lets the next start die with EADDRINUSE, leaving the OLD process
  # serving -- so the site quietly runs stale code, which is worse than a failure.
  if [[ -n "$port" ]]; then
    for _ in $(seq 1 15); do
      ss -tln 2>/dev/null | grep -q "127.0.0.1:$port " || break
      sleep 1
    done
    if ss -tln 2>/dev/null | grep -q "127.0.0.1:$port "; then
      echo "  $name ignored TERM, sending KILL to group"
      [[ -n "${pgid:-}" ]] && kill -KILL -- "-$pgid" 2>/dev/null || true
      # Last resort: whatever still holds the port, by fd owner.
      for p in $(ss -tlnp 2>/dev/null | grep ":$port " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
        kill -KILL "$p" 2>/dev/null || true
      done
      sleep 2
      ss -tln 2>/dev/null | grep -q "127.0.0.1:$port " \
        && { echo "  !! port $port STILL held -- refusing to start stale" >&2; return 1; }
    fi
  fi
  [[ "$killed" == "1" ]] && echo "  stopped $name" || echo "  $name not running"
  return 0
}

# A service counts as up when its port answers, not when a pid exists. mongod
# reported "running" by pid while actually dead, which is how the earlier
# failure stayed hidden.
wait_for_port() {  # wait_for_port <port> <seconds> <name>
  local port="$1" secs="$2" name="$3"
  for _ in $(seq 1 "$secs"); do
    if ss -tln 2>/dev/null | grep -q "127.0.0.1:$port "; then
      echo "  $name up on $port"; return 0
    fi
    sleep 1
  done
  echo "  !! $name did NOT come up on $port -- check $LOG/" >&2
  return 1
}

case "${1:-start}" in

stop)
  stop_one landing "$LANDING_PORT"; stop_one web "$WEB_PORT"; stop_one mongo "$MONGO_PORT"
  exit 0
  ;;

reload)
  # What a code deploy actually needs. Only gunicorn is restarted:
  #
  #   mongod   holds the database; bouncing it on every deploy is slow and
  #            risks the data for no benefit, since code changes never affect it.
  #   landing  serves the page that shows the deploy spinner. Stopping it means
  #            anyone loading e4e/ mid-deploy gets a 502 from the very page whose
  #            job is to say "deploying" -- so it stays up and is re-rendered in
  #            place, which is enough because http.server reads from disk per
  #            request.
  #
  # The outage is therefore limited to LOOP itself, for as long as gunicorn takes.
  RELOAD_ONLY=1
  ;;

status)
  for s in mongo web landing; do
    if alive "$s"; then echo "  $s  RUNNING  pid $(cat "$(pidfile "$s")")"
    else echo "  $s  stopped"; fi
  done
  echo
  for p in "$MONGO_PORT" "$WEB_PORT" "$LANDING_PORT"; do
    ss -tln 2>/dev/null | grep -q "127.0.0.1:$p " \
      && echo "  127.0.0.1:$p  listening" || echo "  127.0.0.1:$p  not listening"
  done
  exit 0
  ;;

start) RELOAD_ONLY=0 ;;
*) echo "usage: serve.sh [start|stop|reload|status]" >&2; exit 2 ;;
esac

mkdir -p "$RUN" "$LOG" "$ROOT/var/mongo"

# The landing page is rendered into var/ rather than served from the repo, so the
# committed copy holds placeholders and the deployed copy holds real hostnames.
LANDING_SRC="$ROOT/deploy/dev/landing"
LANDING_OUT="$ROOT/var/landing"
mkdir -p "$LANDING_OUT"
# Only the short SHA is substituted, not the commit subject: a subject can
# contain |, & or \ and would corrupt the sed expression. The subject is
# carried in status.json instead, where JSON quoting handles it.
DEV_BRANCH_NAME="$(git -C "$ROOT" branch --show-current 2>/dev/null || echo '?')"
DEV_COMMIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
sed -e "s|__DEV_HOSTNAME__|$HOSTNAME_PUBLIC|g" \
    -e "s|__CHAOS_URL__|${CHAOS_URL:-#}|g" \
    -e "s|__BRANCH__|$DEV_BRANCH_NAME|g" \
    -e "s|__COMMIT__|$DEV_COMMIT_SHA|g" \
    "$LANDING_SRC/index.html" > "$LANDING_OUT/index.html"
# A card with no configured URL would be a dead link, so hide it instead.
[[ -z "$CHAOS_URL" ]] && sed -i 's|<a class="door door--chaos"|<a hidden class="door door--chaos"|' "$LANDING_OUT/index.html"

# The landing page polls this file and shows a spinner until state is "ready".
# Written before anything starts so a reload mid-startup reports honestly.
STATUS="$LANDING_OUT/status.json"
write_status() {  # write_status <state>
  local branch commit subject when
  branch="$(git -C "$ROOT" branch --show-current 2>/dev/null || echo '?')"
  commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
  # Quote the subject via python so a message containing " or \ cannot produce
  # invalid JSON and break the page's poller.
  subject="$("$VENV/python" -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))' \
             <<<"$(git -C "$ROOT" log -1 --format=%s 2>/dev/null)" 2>/dev/null || echo '""')"
  when="$(git -C "$ROOT" log -1 --format=%cI 2>/dev/null || echo '')"
  printf '{"state":"%s","branch":"%s","commit":"%s","subject":%s,"committed":"%s","updated":"%s"}\n' \
    "$1" "$branch" "$commit" "$subject" "$when" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$STATUS"
}
write_status starting

if [[ "${RELOAD_ONLY:-0}" == "0" ]]; then
echo "=== mongod ==="
start_bg mongo "$LOG/mongod.log" \
  "$ROOT/mongodb/bin/mongod" \
    --dbpath "$ROOT/var/mongo" \
    --port "$MONGO_PORT" \
    --bind_ip 127.0.0.1 \
    --wiredTigerCacheSizeGB 1
# Fail here rather than letting Django start against a dead database and produce
# a confusing 500 later.
wait_for_port "$MONGO_PORT" 40 mongod || exit 1
else
  echo "=== mongod (left running) ==="
fi

[[ "${RELOAD_ONLY:-0}" == "1" ]] && stop_one web "$WEB_PORT"

echo "=== django ==="
"$VENV/python" "$ROOT/manage.py" migrate --noinput >>"$LOG/django.log" 2>&1
if [[ "${CHEMSCREEN_BOOTSTRAP_ON_START:-1}" != "0" ]]; then
  "$VENV/python" "$ROOT/manage.py" ensure_prediction_data --minimum 20 >>"$LOG/django.log" 2>&1
fi
"$VENV/python" "$ROOT/manage.py" collectstatic --noinput >>"$LOG/django.log" 2>&1
echo "  migrations + static files done"
start_bg web "$LOG/gunicorn.log" \
  "$VENV/gunicorn" \
    --chdir "$ROOT" \
    --workers 2 --worker-class gthread --threads 4 --timeout 120 \
    `# Cloudflare Access appends a ~2 KB JWT to the redirect query string and adds` \
    `# a Cf-Access-Jwt-Assertion header. Both exceed gunicorn's defaults (4094-byte` \
    `# request line), which returns "Request Line is too large" before Django is` \
    `# ever reached. 8190 is gunicorn's maximum for the request line.` \
    --limit-request-line 8190 \
    --limit-request-fields 200 \
    --limit-request-field_size 32768 \
    --bind "127.0.0.1:$WEB_PORT" \
    `# Not loop.wsgi directly: the tunnel cannot strip the /loop prefix that` \
    `# FORCE_SCRIPT_NAME assumes a proxy has removed. See wsgi_subpath.py.` \
    --pythonpath "$ROOT/deploy/dev" \
    wsgi_subpath:application

wait_for_port "$WEB_PORT" 90 gunicorn || exit 1   # sentence-transformers preload is slow

echo "=== landing page ==="
start_bg landing "$LOG/landing.log" \
  "$VENV/python" -m http.server "$LANDING_PORT" \
    --bind 127.0.0.1 --directory "$LANDING_OUT"
wait_for_port "$LANDING_PORT" 15 landing || exit 1

# Only now is every service actually answering, so this is the earliest honest
# point to say "ready" -- the banner clears and watching browsers reload.
write_status ready

cat <<EOF

Running.

  landing   http://127.0.0.1:$LANDING_PORT/       -> https://$HOSTNAME_PUBLIC/
  LOOP      http://127.0.0.1:$WEB_PORT/loop/      -> https://$HOSTNAME_PUBLIC/loop/
  mongo     127.0.0.1:$MONGO_PORT

  logs      $LOG/
  stop      deploy/dev/serve.sh stop
EOF
