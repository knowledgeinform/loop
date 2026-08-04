#!/usr/bin/env bash
#
# Deploy whatever is on origin/dev-environment. RUNS ON MINTAKA -- no Mac needed.
#
#   deploy/dev/pull-deploy.sh              # deploy if the branch moved
#   deploy/dev/pull-deploy.sh --force      # deploy even if unchanged
#   deploy/dev/pull-deploy.sh --install    # install the 2-minute cron timer
#   deploy/dev/pull-deploy.sh --uninstall
#   deploy/dev/pull-deploy.sh --status
#
# Push to dev-environment and it is live on the dev host within ~2 minutes.
#
# CREDENTIALS: reads ~/.git-credentials (mode 600), which lives in $HOME rather
# than inside ~/loop-dev. The sandbox only bind-mounts loop-dev, so the confined
# web/mongo/landing processes cannot read it -- but this script, which runs
# unsandboxed, can. That separation is the whole point of the file's location.
#
# The token is a classic PAT with `repo` scope, which carries WRITE access to
# every repository the account can reach. A shell on this box can therefore push
# to production source. A read-only deploy key would remove that exposure and is
# worth switching to when repo admin is available.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRANCH="${DEV_BRANCH:-dev-environment}"
STAMP="$ROOT/var/.last-deployed"
LOG="$ROOT/var/log/pull-deploy.log"
# Must match where serve.sh renders the page and where the landing server
# serves from (var/landing), or the "deploying" state is written to a file
# nobody reads and the spinner never appears during the git phase.
STATUS="$ROOT/var/landing/status.json"
CRON_MARK="# loop-dev auto-deploy"

mkdir -p "$ROOT/var/log" "$ROOT/var/landing"
log() { printf '%s %s\n' "$(date -u '+%F %T')" "$*" >> "$LOG"; }

case "${1:-}" in
--install)
  # -o (--close) closes the lock descriptor before exec. Without it, every
  # long-lived process the deploy starts -- mongod, the landing server --
  # inherits the open fd and holds the lock for its entire life, so every
  # later `flock -n` fails and exits silently. Deploys then stop happening
  # with nothing written to the log.
  # The lock also lives under var/run/ rather than var/, so it is beside the
  # other runtime state instead of loose in the tree.
  line="*/2 * * * * cd $ROOT && flock -n -o var/run/deploy.lock deploy/dev/pull-deploy.sh >> $LOG 2>&1 $CRON_MARK"
  # flock -n: a slow deploy must not have a second one started on top of it.
  # `|| true` on the grep: with no crontab yet it matches nothing and exits 1,
  # which under `set -e` aborted the subshell before the new line was written --
  # the install silently did nothing and reported success.
  ( { crontab -l 2>/dev/null || true; } | grep -vF "$CRON_MARK" || true; echo "$line" ) | crontab -
  echo "  installed: checks origin/$BRANCH every 2 minutes"
  echo "  log: $LOG"
  exit 0 ;;
--uninstall)
  { crontab -l 2>/dev/null || true; } | grep -vF "$CRON_MARK" | crontab - || true
  echo "  removed"
  exit 0 ;;
--status)
  echo "  branch        : $BRANCH"
  echo "  local  HEAD   : $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
  git -C "$ROOT" fetch -q origin 2>/dev/null || true
  echo "  origin HEAD   : $(git -C "$ROOT" rev-parse --short "origin/$BRANCH" 2>/dev/null || echo missing)"
  echo "  last deployed : $(cat "$STAMP" 2>/dev/null | cut -c1-7 || echo none)"
  crontab -l 2>/dev/null | grep -qF "$CRON_MARK" && echo "  timer         : installed" || echo "  timer         : not installed"
  [[ -f "$LOG" ]] && { echo "  recent:"; tail -4 "$LOG" | sed 's/^/    /'; }
  exit 0 ;;
esac

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

cd "$ROOT"

if ! git fetch -q origin 2>/dev/null; then
  log "fetch failed (network or credentials); will retry"
  exit 0                       # transient: not an error worth alerting on
fi

target="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"
[[ "$target" == "none" ]] && { log "origin/$BRANCH missing"; exit 0; }

if [[ "$FORCE" == "0" && -f "$STAMP" && "$(cat "$STAMP")" == "$target" ]]; then
  exit 0                       # nothing new, stay silent
fi

log "deploying ${target:0:7} on $BRANCH"
printf '{"state":"deploying","branch":"%s","commit":"%s"}\n' "$BRANCH" "${target:0:7}" > "$STATUS"

# -f is required, not defensive. Files first delivered here by rsync arrive
# untracked; once they are committed upstream, a plain checkout refuses to
# overwrite them ("Please move or remove them before you switch branches") and
# the deploy aborts while still reporting the old commit as live. -f overwrites
# them. Server state is protected by .gitignore instead: var/ (the database),
# .venv/, mongodb/, .env.dev and access-emails.txt are all ignored, so a forced
# checkout cannot touch them.
git checkout -q -f -B "$BRANCH" "$target"
git submodule update --init --recursive >>"$LOG" 2>&1

# reload, not stop+start: keeps mongod and the landing page (with its deploy
# spinner) up, so only LOOP itself is briefly unavailable.
if deploy/dev/serve.sh reload >>"$LOG" 2>&1; then
  # Stamp only after services are confirmed up, so a failure retries next tick
  # instead of being recorded as deployed.
  echo "$target" > "$STAMP"
  log "deployed ${target:0:7}"
else
  log "!! deploy of ${target:0:7} FAILED -- see above"
  printf '{"state":"failed","branch":"%s","commit":"%s"}\n' "$BRANCH" "${target:0:7}" > "$STATUS"
  exit 1
fi
