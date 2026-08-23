"""MongoEngine signal wiring that makes every write archive-first.

Why signals rather than wrapping the write paths
------------------------------------------------
There are roughly a hundred ``.save()`` and ``.delete()`` call sites across
``views.py``, ``api/``, ``services/``, and the background workers. Wrapping
each one would be a hundred chances to miss one, and every future feature
would be a hundred-and-first. Hooking the document layer covers all of them at
once, including code not yet written.

Why ``pre_save_post_validation``
--------------------------------
It fires after ``validate()`` and before the write to Mongo
(``mongoengine/document.py``: the signal is sent, and only then is
``doc = self.to_mongo()`` computed and handed to the driver). So raising from
this hook means Mongo is never touched — which is exactly the archive-first
guarantee. Mongoengine explicitly supports mutating the document from this
hook, which is what lets us assign an ``ObjectId`` below.

``pre_save`` would be too early: the document has not been validated, so we
could archive a record that Mongo then rejects.

Deletes
-------
``pre_delete`` covers ``doc.delete()`` and, less obviously,
``Recipe.objects(material_auid=...).delete()``: MongoEngine's ``QuerySet.delete``
checks whether any delete signal has receivers and, if so, loops the documents
individually instead of issuing one bulk ``delete_many``. So the cascading
wipe in ``views.delete_material`` produces one archive tombstone per recipe.

What this does *not* cover
--------------------------
``QuerySet.update()`` and ``update_one()`` fire no signals at all. Those call
sites — ``raw_db.record_raw_file``, ``documents.upsert_user_affiliations``, the
superuser JSON editor, the AFLOW bulk import — call the writer explicitly.
They are enumerated in the module docstring of :mod:`catalog.archive.writer`
and covered by ``loop_archive verify``.
"""

from __future__ import annotations

import logging

from bson import ObjectId
from mongoengine import signals as me_signals

from . import registry, writer

logger = logging.getLogger(__name__)

_connected = False


def archive_document(document) -> None:
    """Write one document to the archive. The hook body, callable directly.

    Exposed for the few code paths that write to Mongo without going through
    ``Document.save()`` — notably the superuser JSON editor, which issues a raw
    ``replace_one``. Those call this first, then write, preserving the
    archive-first ordering.
    """
    if not writer.is_enabled():
        return
    archive_kind = registry.kind_for_document(document)
    if archive_kind is None:
        return

    # Auto-id documents have no primary key until the driver assigns one on
    # insert, but the archive path and the journal key need it *now*. Assigning
    # it here is safe: `created` was already computed before this hook, and
    # `_save_create` honors an explicit `_id`.
    if archive_kind.id_is_objectid and document.pk is None:
        document.pk = ObjectId()

    if archive_kind is registry.RECIPE:
        writer.write_recipe(document)
        return

    payload = registry.document_payload(document, drop=archive_kind.drop_fields)
    writer.write_payload(archive_kind.name, payload)


def archive_document_deletion(document) -> None:
    """Tombstone one document in the archive. The delete hook body."""
    if not writer.is_enabled():
        return
    archive_kind = registry.kind_for_document(document)
    if archive_kind is None:
        return

    if archive_kind is registry.RECIPE:
        writer.delete_recipe(document)
        return

    payload = registry.document_payload(document, drop=archive_kind.drop_fields)
    writer.delete_payload(archive_kind.name, payload)


def _archive_save(sender, document, **kwargs):
    """Write the record to the archive before Mongo sees it."""
    archive_document(document)


def _archive_delete(sender, document, **kwargs):
    """Tombstone the record in the archive before Mongo drops it."""
    archive_document_deletion(document)


def connect_archive_signals() -> None:
    """Attach archive hooks to every archived document class. Idempotent."""
    global _connected
    if _connected:
        return
    for archive_kind in registry.HOOKED_KINDS:
        try:
            document_class = archive_kind.document_class()
        except Exception:
            logger.exception(
                "archive: could not resolve %s for kind %r",
                archive_kind.document_path,
                archive_kind.name,
            )
            continue
        me_signals.pre_save_post_validation.connect(_archive_save, sender=document_class)
        me_signals.pre_delete.connect(_archive_delete, sender=document_class)
    _connected = True


__all__ = [
    "archive_document",
    "archive_document_deletion",
    "connect_archive_signals",
]
