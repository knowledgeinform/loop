#!/usr/bin/env bash
#
# Run a command confined to the dev tree, using bubblewrap.
#
#   deploy/dev/sandbox.sh <command> [args...]
#
# WHY THIS EXISTS
#
# The dev site is internet-facing and runs the branch under test, so it is the
# least trustworthy code on the host. Unconfined it runs as the invoking user,
# and a remote-code-execution bug in Django would hand the attacker:
#
#   ~/.cloudflared/cert.pem   the Cloudflare ACCOUNT credential -- lets an
#                             attacker create tunnels and DNS records across
#                             the whole zone, not merely hijack this one
#   ~/.cloudflared/*.json     this tunnel's credentials
#   ~/.ssh                    keys and known_hosts
#   /srv/loop/data/LOOP       production's Mongo files and user table, which
#                             are world-readable (drwxr-xr-x) on this host
#
# The dev database is a copy, so losing it costs little. The credentials are
# what turn "someone broke the dev box" into "someone owns the account". This
# sandbox makes all four invisible to the confined process.
#
# WHAT IT DOES AND DOES NOT PROTECT
#
#   Blocked : the filesystem outside ~/loop-dev -- credentials, keys, prod data,
#             other users' homes. Verified by deploy/dev/verify-sandbox.sh.
#   Not blocked : the network. The process keeps normal networking because the
#             three dev services talk to each other over loopback. It can still
#             reach prod's Mongo on the Docker bridge, read-only, which is data
#             dev already holds a copy of. Blocking that needs firewall rules
#             and therefore root.
#
# Namespaces are unprivileged (kernel.unprivileged_userns_clone=1), so none of
# this needs sudo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[[ $# -ge 1 ]] || { echo "usage: sandbox.sh <command> [args...]" >&2; exit 2; }
command -v bwrap >/dev/null || { echo "bwrap not found" >&2; exit 1; }

exec bwrap \
  --ro-bind /usr /usr \
  --ro-bind /etc /etc \
  --ro-bind /lib /lib \
  --ro-bind-try /lib64 /lib64 \
  --ro-bind-try /bin /bin \
  --ro-bind-try /sbin /sbin \
  --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
  `# MongoDB 8's tcmalloc counts CPUs via /sys and aborts on a failed CHECK if it` \
  `# is absent. Read-only kernel topology, no credentials, so binding it costs` \
  `# nothing that the confinement cares about.` \
  --ro-bind-try /sys /sys \
  --bind "$ROOT" "$ROOT" \
  --tmpfs /tmp \
  --proc /proc \
  --dev /dev \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  `# No --die-with-parent: these are daemons. serve.sh exits as soon as it has` \
  `# launched them, and --die-with-parent would take the service down with it.` \
  --new-session \
  --setenv HOME "$ROOT" \
  --chdir "$ROOT" \
  "$@"
