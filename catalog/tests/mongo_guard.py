"""Shared skip-guard for tests that need a live MongoDB."""

import os


def mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db

        get_db().command("ping")
        return True
    except Exception:
        return False
