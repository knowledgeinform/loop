"""Ambient attribution for archive writes: who did this, and from where.

Every journal entry records an ``actor`` and a ``source``. The write happens
deep inside a MongoEngine signal handler, which has no access to the request,
the API key, or the management command that triggered it — so the attribution
travels out-of-band in a :class:`contextvars.ContextVar`.

``ContextVar`` rather than thread-local because it behaves correctly under
gunicorn's threaded workers *and* under the background daemon threads in
:mod:`catalog.apps`, and because each thread starts with the default rather
than inheriting a stale value from whoever used the thread last.

Set it at the outermost boundary you control:

- HTTP requests — :class:`loop.middleware.ArchiveContextMiddleware`
- management commands — :func:`archive_context` around ``handle()``
- background workers — :func:`archive_context` around the job body

Anything that slips through is attributed to ``("system", "unknown")``, which
is honest rather than wrong.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, NamedTuple


class ArchiveActor(NamedTuple):
    """Attribution for one archive write."""

    actor: str
    source: str


DEFAULT_ACTOR = ArchiveActor(actor="system", source="unknown")

_actor_var: contextvars.ContextVar[ArchiveActor] = contextvars.ContextVar(
    "loop_archive_actor", default=DEFAULT_ACTOR
)


def current_actor() -> ArchiveActor:
    """Return the attribution in effect for the current context."""
    return _actor_var.get()


def set_actor(actor: str, source: str) -> contextvars.Token:
    """Set attribution and return the token needed to restore the previous value."""
    return _actor_var.set(ArchiveActor(actor=actor or "system", source=source or "unknown"))


def reset_actor(token: contextvars.Token) -> None:
    """Restore the attribution captured before a :func:`set_actor` call."""
    _actor_var.reset(token)


@contextmanager
def archive_context(actor: str, source: str) -> Iterator[ArchiveActor]:
    """Scope archive attribution to a block.

    >>> with archive_context("pboctor", "web"):
    ...     recipe.save()          # journal records actor=pboctor source=web
    """
    token = set_actor(actor, source)
    try:
        yield current_actor()
    finally:
        reset_actor(token)


def actor_from_user(user, *, source: str) -> ArchiveActor:
    """Derive attribution from a Django user, tolerating anonymous callers."""
    username = ""
    if user is not None and getattr(user, "is_authenticated", False):
        username = getattr(user, "username", "") or ""
    return ArchiveActor(actor=username or "anonymous", source=source)


__all__ = [
    "ArchiveActor",
    "DEFAULT_ACTOR",
    "actor_from_user",
    "archive_context",
    "current_actor",
    "reset_actor",
    "set_actor",
]
