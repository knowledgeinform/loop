#!/usr/bin/env bash
#
# Prove the sandbox actually confines the dev stack.
#
#   deploy/dev/verify-sandbox.sh
#
# Run this after any change to sandbox.sh or serve.sh. A sandbox nobody checks
# is a sandbox that quietly stops working -- one wrong --bind and the confined
# process can read the Cloudflare account credential again.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$ROOT/deploy/dev/sandbox.sh"
fails=0

check() {  # check <description> <expect: hidden|visible> <path>
  local desc="$1" expect="$2" path="$3" got
  got=$("$SANDBOX" /bin/sh -c "[ -e '$path' ] && echo visible || echo hidden" 2>/dev/null)
  if [[ "$got" == "$expect" ]]; then
    printf '  PASS  %-34s %s\n' "$desc" "$got"
  else
    printf '  FAIL  %-34s expected %s, got %s\n' "$desc" "$expect" "$got"
    fails=$((fails + 1))
  fi
}

echo "=== what the dev stack must NOT be able to reach ==="
check "Cloudflare account credential" hidden "$HOME/.cloudflared/cert.pem"
check "tunnel credentials"            hidden "$HOME/.cloudflared"
check "SSH keys"                      hidden "$HOME/.ssh"
check "production data"               hidden "/srv/loop/data/LOOP"
check "production Mongo files"        hidden "/srv/loop/data/LOOP/mongodb"
check "production user table"         hidden "/srv/loop/data/LOOP/users/db.sqlite3"
check "other users' homes"            hidden "/home/www-s4e"

echo
echo "=== what it must still reach, or dev cannot run ==="
check "the dev tree"                  visible "$ROOT"
check "python interpreter"            visible "/usr/bin/python3"

echo
echo "=== writability ==="
# mkdir -p because this may run before setup.sh has created var/.
if "$SANDBOX" /bin/sh -c "mkdir -p '$ROOT/var' && touch '$ROOT/var/.sandbox-write-test' && rm -f '$ROOT/var/.sandbox-write-test'" 2>/dev/null; then
  echo "  PASS  dev tree is writable"
else
  echo "  FAIL  dev tree is not writable"; fails=$((fails + 1))
fi
if "$SANDBOX" /bin/sh -c "touch /usr/.sandbox-write-test 2>/dev/null"; then
  echo "  FAIL  /usr is writable -- it must be read-only"; fails=$((fails + 1))
else
  echo "  PASS  /usr is read-only"
fi

echo
if [[ "$fails" -eq 0 ]]; then
  echo "All checks passed. The dev stack cannot read credentials, keys, or production data."
else
  echo "$fails check(s) FAILED. Do not expose this environment until they pass." >&2
fi
exit "$fails"
