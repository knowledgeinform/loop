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
    "import_chaos_data",
    "discretize_synthesis",
})


def _is_web_process() -> bool:
    """True iff this process serves HTTP (not a one-shot management command)."""
    argv = sys.argv
    if len(argv) >= 2 and argv[1] in _NON_WEB_COMMANDS:
        return False
    # `runserver`'s file-watcher parent process also calls AppConfig.ready(),
    # but it never handles requests — only the child (RUN_MAIN=true) does.
    if "runserver" in argv and os.environ.get("RUN_MAIN") != "true":
        return False
    return True


def _should_preload_embeddings() -> bool:
    """True iff this process will serve HTTP and wants a warm embedder."""
    if not getattr(settings, "EMBEDDINGS_PRELOAD", True):
        return False
    return _is_web_process()


def _run_synthesis_worker() -> None:
    """Poll the synthesis-parse queue and discretize routes in the background.

    Runs in every web worker; the atomic claim in
    :func:`catalog.synthesis_worker.process_pending_jobs` ensures a job is never
    processed twice, so no cross-worker lock is needed.
    """
    import time

    from . import synthesis_worker

    poll = float(getattr(settings, "SYNTHESIS_LLM_POLL_SECONDS", 15))
    print("[catalog] Synthesis discretization worker started.", flush=True)
    while True:
        try:
            counts = synthesis_worker.process_pending_jobs(limit=25)
            if any(counts.values()):
                print(f"[catalog] synthesis worker processed {counts}", flush=True)
        except Exception as exc:  # never let the loop die
            logger.warning("synthesis worker loop error: %s", exc)
        time.sleep(poll)


def _run_model_training_worker() -> None:
    """Drain coalesced EFA/DEED retraining requests in the background."""
    import time

    from .model_training import process_pending_training_jobs

    poll = float(getattr(settings, "CHEMSCREEN_RETRAIN_POLL_SECONDS", 15))
    print("[catalog] ChemScreen EFA/DEED retraining worker started.", flush=True)
    while True:
        try:
            counts = process_pending_training_jobs(limit=1)
            if any(counts.values()):
                print(
                    f"[catalog] ChemScreen retraining worker processed {counts}",
                    flush=True,
                )
        except Exception as exc:
            logger.warning("ChemScreen retraining worker loop error: %s", exc)
        time.sleep(poll)


def _run_xrd_analysis_worker() -> None:
    """Poll the XRD analysis queue and execute persisted jobs in the background."""
    import time

    from .xrd_analysis.worker import process_pending_xrd_analysis_jobs, xrd_poll_seconds

    poll = xrd_poll_seconds()
    print("[catalog] XRD analysis worker started.", flush=True)
    while True:
        try:
            counts = process_pending_xrd_analysis_jobs(limit=25)
            if any(counts.values()):
                print(f"[catalog] xrd analysis worker processed {counts}", flush=True)
        except Exception as exc:  # never let the loop die
            logger.warning("xrd analysis worker loop error: %s", exc)
        time.sleep(poll)


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


def _watch_chaos_db() -> None:
    """Poll the CHAOS SQLite database for changes and trigger an incremental import.

    Uses a lock file so that only one gunicorn worker runs the import even when
    multiple workers are configured (each calls AppConfig.ready()).
    """
    import fcntl
    import subprocess
    import time

    db_path = getattr(settings, "CHAOS_DB_PATH", "")
    if not db_path:
        return

    if not os.path.exists(db_path):
        print(
            f"[catalog] CHAOS_DB_PATH={db_path!r} not found; file watcher will not start.",
            flush=True,
        )
        return

    # Acquire an exclusive lock on a sentinel file before entering the poll
    # loop.  The first gunicorn worker to grab the lock becomes the sole watcher;
    # the others exit silently.  The lock is held for the lifetime of the process
    # (released automatically by the OS on exit), so it transfers cleanly on
    # worker restart.
    lock_path = db_path + ".watch.lock"
    try:
        lock_fh = open(lock_path, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another worker already holds the lock — this worker bows out.
        return

    print(f"[catalog] CHAOS file watcher started (polling {db_path} every 3600s).", flush=True)

    last_mtime: float = os.stat(db_path).st_mtime
    import_running = threading.Lock()  # prevents overlapping imports within this process

    while True:
        time.sleep(3600)
        try:
            current_mtime = os.stat(db_path).st_mtime
        except OSError:
            continue

        if current_mtime == last_mtime:
            continue

        last_mtime = current_mtime

        if not import_running.acquire(blocking=False):
            print("[catalog] CHAOS watcher: file changed but previous import still running; skipping.", flush=True)
            continue

        print("[catalog] CHAOS file changed — starting incremental import.", flush=True)
        try:
            manage_py = os.path.join(settings.BASE_DIR, "manage.py")
            proc = subprocess.Popen(
                ["python", manage_py, "import_chaos_data", "--incremental"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                print(f"[import_chaos_data] {line}", end="", flush=True)
            proc.wait()
            if proc.returncode != 0:
                print(f"[catalog] CHAOS import exited with code {proc.returncode}.", flush=True)
            else:
                print("[catalog] CHAOS incremental import complete.", flush=True)
        except Exception as exc:
            print(f"[catalog] CHAOS import failed: {exc}", flush=True)
        finally:
            import_running.release()


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

            threading.Thread(
                target=_watch_chaos_db,
                name="chaos-watcher",
                daemon=True,
            ).start()

        if getattr(settings, "SYNTHESIS_LLM_WORKER", False) and _is_web_process():
            # Daemon thread: discretize batch synthesis routes in the background
            # so uploads never wait on the LLM. Gated by SYNTHESIS_LLM_WORKER.
            threading.Thread(
                target=_run_synthesis_worker,
                name="synthesis-worker",
                daemon=True,
            ).start()

        if getattr(settings, "XRD_ANALYSIS_WORKER", False) and _is_web_process():
            # Daemon thread: execute persisted XRD analysis jobs outside HTTP requests.
            threading.Thread(
                target=_run_xrd_analysis_worker,
                name="xrd-analysis-worker",
                daemon=True,
            ).start()

        if (
            getattr(settings, "CHEMSCREEN_AUTOTRAIN_ENABLED", True)
            and getattr(settings, "CHEMSCREEN_RETRAIN_WORKER", True)
            and _is_web_process()
        ):
            threading.Thread(
                target=_run_model_training_worker,
                name="chemscreen-retraining-worker",
                daemon=True,
            ).start()
