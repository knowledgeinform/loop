import logging
import os
import sys
import threading

from django.apps import AppConfig
from django.conf import settings


logger = logging.getLogger(__name__)


# Management commands that don't serve HTTP traffic and therefore don't
# benefit from a preloaded embedder. Keeps `manage.py migrate`,
# `makemigrations`, etc. fast and free of a 90 MB model download.
_NON_WEB_COMMANDS = frozenset({
    "makemigrations",
    "migrate",
    "collectstatic",
    "test",
    "createsuperuser",
    "changepassword",
    "shell",
    "dbshell",
    "check",
    "showmigrations",
    "loaddata",
    "dumpdata",
    "compilemessages",
    "makemessages",
    "sendtestemail",
    "diffsettings",
    "mongo_admin",
    "backfill_embeddings",
})


def _should_preload_embeddings() -> bool:
    """True iff this process will serve HTTP and wants a warm embedder."""
    if not getattr(settings, "EMBEDDINGS_PRELOAD", True):
        return False
    # Management commands: skip anything that isn't a web server.
    argv = sys.argv
    if len(argv) >= 2:
        command = argv[1]
        if command in _NON_WEB_COMMANDS:
            return False
    # `runserver`'s file-watcher parent process also calls AppConfig.ready(),
    # but it never handles requests — only the child (RUN_MAIN=true) does.
    # Skip the parent so we don't load the model twice in dev.
    if "runserver" in argv and os.environ.get("RUN_MAIN") != "true":
        return False
    return True


def _preload_embeddings() -> None:
    """Load the sentence-transformers checkpoint and keep the singleton warm."""
    import time

    started = time.monotonic()
    try:
        from . import embeddings as embeddings_mod

        embeddings_mod.get_embedder()
        # One-token encode to force tokenizer initialization too; otherwise the
        # first real query still pays for the tokenizer's lazy warmup.
        embeddings_mod.embed_text("warmup")
    except Exception as exc:
        # Never crash the web boot because the embedder couldn't load:
        # semantic search already gracefully degrades to AUID regex via
        # VectorSearchUnavailable, and we don't want an offline HF hub or
        # a missing dependency to take the whole site down.
        logger.warning("Embedding model preload failed: %s", exc)
        print(f"[catalog] Embedding model preload failed: {exc}", flush=True)
        return
    # Use print() rather than logger.info(): Django's default config filters
    # anything below WARNING, but we want this one-time startup confirmation
    # visible in `docker compose logs` without forcing users to add a LOGGING
    # dict to settings.py.
    from . import embeddings as embeddings_mod
    elapsed = time.monotonic() - started
    print(
        f"[catalog] Preloaded embedding model "
        f"{embeddings_mod.EMBEDDING_MODEL_NAME} in {elapsed:.1f}s; "
        f"semantic search is warm.",
        flush=True,
    )


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'catalog'

    def ready(self):
        from . import signals  # noqa: F401

        signals.connect_document_signals()

        if _should_preload_embeddings():
            # Daemon thread: don't block request handling on model load, and
            # don't keep the process alive past shutdown. First semantic
            # request within ~5s of boot may still pay a few ms of lock wait,
            # but it's orders of magnitude cheaper than the cold load.
            threading.Thread(
                target=_preload_embeddings,
                name="embedding-preload",
                daemon=True,
            ).start()
