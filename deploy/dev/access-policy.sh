#!/usr/bin/env bash
#
# Create (or update) the Cloudflare Access application that gates the dev suite,
# allowing only the addresses in deploy/dev/access-emails.txt (untracked).
#
#   export CF_API_TOKEN=...        # never stored in this repo
#   export CF_ACCOUNT_ID=...
#   deploy/dev/access-policy.sh              # apply
#   deploy/dev/access-policy.sh --show       # print current config, change nothing
#
# WHY A SCRIPT AND NOT JUST THE DASHBOARD
#
# The policy is a security control, so how it is built belongs in the repo where
# a change is reviewable. Configured only in a web UI, "who can reach production
# data" is untracked and an accidental deletion is silent. The addresses
# themselves stay out of git -- see access-emails.txt.
#
# TOKEN SCOPE: create at dash.cloudflare.com/profile/api-tokens with
#   Account -> Access: Apps and Policies -> Edit
# Nothing else. The token is read from the environment and never written to disk.
#
# ORDERING MATTERS: run this BEFORE creating the DNS record. If DNS and the
# tunnel ingress go live first, the site is publicly reachable -- with real data
# and an open signup page -- until the policy lands.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[[ -f "$_here/.env.dev" ]] && . "$_here/.env.dev"

# ---------------------------------------------------------------------------
# The allow-list lives in an untracked file, one address per line, rather than in
# this script. Committing it would put collaborators' addresses in git history
# permanently and advertise who can reach a system holding unpublished data --
# neither is undoable once pushed. See access-emails.txt.example.
EMAIL_FILE="${EMAIL_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/access-emails.txt}"

ALLOWED_EMAILS=()
if [[ -f "$EMAIL_FILE" ]]; then
  while IFS= read -r line; do
    line="${line%%#*}"                        # allow trailing comments
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [[ -n "$line" ]] && ALLOWED_EMAILS+=("$line")
  done < "$EMAIL_FILE"
fi

APP_NAME="${APP_NAME:-LOOP dev}"
APP_DOMAIN="${APP_DOMAIN:-${DEV_HOSTNAME:-}}"
SESSION_DURATION="${SESSION_DURATION:-24h}"

API="https://api.cloudflare.com/client/v4"
: "${CF_API_TOKEN:?set CF_API_TOKEN (Account -> Access: Apps and Policies -> Edit)}"
: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID}"
: "${APP_DOMAIN:?set DEV_HOSTNAME in deploy/dev/.env.dev (or APP_DOMAIN)}"

cf() {  # cf <METHOD> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$API$path" \
      -H "Authorization: Bearer $CF_API_TOKEN" \
      -H "Content-Type: application/json" \
      --data "$body"
  else
    curl -sS -X "$method" "$API$path" -H "Authorization: Bearer $CF_API_TOKEN"
  fi
}

# python3 rather than jq: jq is not installed everywhere, python3 is.
json() { python3 -c "$1" ; }

die_on_error() {  # reads an API response on stdin
  python3 -c '
import json, sys
r = json.load(sys.stdin)
if not r.get("success"):
    for e in r.get("errors") or [{"message": "unknown error"}]:
        print("  API error %s: %s" % (e.get("code", "?"), e.get("message")), file=sys.stderr)
    sys.exit(1)
print(json.dumps(r.get("result")))
'
}

say() { printf '\n=== %s ===\n' "$1"; }

# --- show mode -------------------------------------------------------------
if [[ "${1:-}" == "--show" ]]; then
  say "Current Access applications"
  cf GET "/accounts/$CF_ACCOUNT_ID/access/apps" | python3 -c '
import json, sys
for a in (json.load(sys.stdin).get("result") or []):
    print("  %-24s %-30s session=%s" % (a.get("name"), a.get("domain"), a.get("session_duration")))
'
  exit 0
fi

# --- the allow-list must exist and be non-empty ---------------------------
# Applying an empty policy would lock everyone out, so refuse rather than guess.
if [[ ${#ALLOWED_EMAILS[@]} -eq 0 ]]; then
  cat >&2 <<MSG
No addresses found in:
  $EMAIL_FILE

Create it (one email per line) before running. It is gitignored on purpose:

  cp deploy/dev/access-emails.txt.example deploy/dev/access-emails.txt
  \$EDITOR deploy/dev/access-emails.txt
MSG
  exit 2
fi

say "1/3  Look for an existing app on $APP_DOMAIN"
APP_ID="$(cf GET "/accounts/$CF_ACCOUNT_ID/access/apps" | python3 -c "
import json, sys
want = '$APP_DOMAIN'
for a in (json.load(sys.stdin).get('result') or []):
    if a.get('domain') == want:
        print(a['id']); break
")"
if [[ -n "$APP_ID" ]]; then
  echo "  found: $APP_ID (will update)"
else
  echo "  none — will create"
fi

say "2/3  Create or update the application"
APP_BODY="$(python3 -c "
import json
print(json.dumps({
    'name': '''$APP_NAME''',
    'domain': '$APP_DOMAIN',
    'type': 'self_hosted',
    'session_duration': '$SESSION_DURATION',
    # Cloudflare handles the login page; the origin never sees anonymous traffic.
    'app_launcher_visible': True,
    'auto_redirect_to_identity': False,
}))
")"
if [[ -n "$APP_ID" ]]; then
  cf PUT "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID" "$APP_BODY" | die_on_error >/dev/null
  echo "  updated app $APP_ID"
else
  APP_ID="$(cf POST "/accounts/$CF_ACCOUNT_ID/access/apps" "$APP_BODY" \
    | die_on_error | json 'import json,sys; print(json.load(sys.stdin)["id"])')"
  echo "  created app $APP_ID"
fi

say "3/3  Apply the allow-list policy"
POLICY_BODY="$(python3 -c "
import json
emails = json.loads('''$(printf '%s\n' "${ALLOWED_EMAILS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')''')
print(json.dumps({
    'name': 'dev-suite testers',
    'decision': 'allow',
    # Include is OR'd. Only these addresses; everything else is refused at the edge.
    'include': [{'email': {'email': e}} for e in emails],
}))
")"

# Replace any existing policy of the same name so re-running is idempotent.
EXISTING="$(cf GET "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" | python3 -c "
import json, sys
for p in (json.load(sys.stdin).get('result') or []):
    if p.get('name') == 'dev-suite testers':
        print(p['id']); break
")"
if [[ -n "$EXISTING" ]]; then
  cf PUT "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies/$EXISTING" "$POLICY_BODY" \
    | die_on_error >/dev/null
  echo "  updated policy $EXISTING"
else
  cf POST "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" "$POLICY_BODY" \
    | die_on_error >/dev/null
  echo "  created policy"
fi

echo "  allowed:"
printf '    %s\n' "${ALLOWED_EMAILS[@]}"

cat <<EOF

Done. https://$APP_DOMAIN is now gated by Cloudflare Access.

Each listed address gets a one-time code by email, then reaches the site.
Anything else is refused at Cloudflare's edge and never touches mintaka.

Next, only now that the gate exists:
  cloudflared tunnel route dns chaos $APP_DOMAIN
  # then add the ingress rules from deploy/dev/cloudflared-ingress.example.yml
EOF
