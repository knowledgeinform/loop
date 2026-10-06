"""Usage record for LOOP: one JSON line per request.

Why: the S4E Laboratory reports how its tools are used (who, from which
institution and country, which pages, searches and API calls) in proposals and
reports to funders. Before 2026-09 LOOP kept no record of reads at all: its
archive journal records changes only, gunicorn writes no access log, and
Apache sees the account of no one. Decided by Corey Oses, 2026-09-29: record
every request with the account and the visitor's address, keep the record for
the life of the project plus five years, xz-compressed.

The lines go to USAGE_DIR/loop/<YYYY-MM-DD>.jsonl (UTC), in the same format as
the CHAOS API gate and CHAOS-Agent (web/usage.ts in entropy4energy/chaosgpt),
so one report script reads them all. A nightly job on the server compresses
finished days. USAGE_DIR empty: nothing is written. A failed write never
affects the response.

Never recorded: request bodies (passwords, uploads) and cookies. Recorded
with the value masked: the token in activation and password-reset links, and
query parameters whose name mentions a token, key, password, secret or
signature.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from django.conf import settings

TOOL = "loop"

# A query parameter is treated as secret when its name contains token,
# password, passwd, secret or signature, or is key / api_key / apikey or ends
# in _key or -key ("keyword" and "monkey" are not).
_SECRET_PARAM = re.compile(
    r"(?i)((?:^|[?&])(?:[^=&#?]*(?:token|password|passwd|secret|signature)[^=&#?]*|(?:[^=&#?]*[_-])?key|apikey)=)[^&#]*"
)
_SECRET_PATH = re.compile(r"(/accounts/(?:reset|activate)/[^/]+/)[^/]+")


def usage_dir() -> str:
    return getattr(settings, "USAGE_DIR", "") or ""


def client_ip(request) -> str:
    meta = request.META
    forwarded = meta.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return (meta.get("HTTP_CF_CONNECTING_IP") or forwarded or meta.get("HTTP_X_REAL_IP") or meta.get("REMOTE_ADDR") or "")[:64]


def clean_path(path: str) -> str:
    return _SECRET_PATH.sub(r"\1***", path)


def clean_query(query: str) -> str:
    return _SECRET_PARAM.sub(r"\1***", query)


def write(fields: dict, now: datetime | None = None) -> None:
    root = usage_dir()
    if not root:
        return
    now = now or datetime.now(timezone.utc)
    line = {"t": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "tool": TOOL}
    line.update({k: v for k, v in fields.items() if v is not None})
    try:
        folder = os.path.join(root, TOOL)
        os.makedirs(folder, exist_ok=True)
        fd = os.open(
            os.path.join(folder, now.strftime("%Y-%m-%d") + ".jsonl"),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o640,
        )
        try:
            os.write(fd, (json.dumps(line, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"[usage] loop: not recorded: {exc}", file=sys.stderr)


def record_request(request, response, started: float) -> None:
    user = getattr(request, "user", None)
    signed_in = bool(user is not None and getattr(user, "is_authenticated", False))
    # An API key: DRF puts the APIKey on request.auth (catalog.api.
    # authentication); the older session-JSON endpoints resolve keys in
    # ApiTokenAuthMiddleware, which leaves the prefix on the request.
    key = getattr(getattr(request, "auth", None), "prefix", None) or getattr(request, "usage_key_prefix", None)
    via = "key" if key else ("session" if signed_in else "none")
    match = getattr(request, "resolver_match", None)
    size = None
    if not getattr(response, "streaming", False):
        try:
            size = len(response.content)
        except Exception:
            size = None
    write(
        {
            "account": user.get_username() if signed_in else None,
            "user_id": user.pk if signed_in else None,
            "via": via,
            "key": key,
            "method": request.method,
            "path": clean_path(request.path),
            "q": clean_query(request.META.get("QUERY_STRING", "")) or None,
            "view": match.view_name if match else None,
            "status": response.status_code,
            "bytes": size,
            "ms": round((time.monotonic() - started) * 1000),
            "ip": client_ip(request),
            "ua": request.META.get("HTTP_USER_AGENT", "")[:400] or None,
            "referer": clean_query(clean_path(request.META.get("HTTP_REFERER", "")[:400])) or None,
            # The CHAOS gate's and kiosk's own sign-in checks (they call
            # /api/v1/access/ or /api/v1/me/ with the visitor's cookie or key);
            # reports leave these out.
            "internal": True if request.META.get("HTTP_USER_AGENT", "") == "chaos-kiosk access check" else None,
        }
    )
