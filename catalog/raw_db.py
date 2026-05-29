"""
Flat "Raw" MongoDB database for file-level backup.

This is the portable, intentionally dumb half of the LOOP data model. One
collection (``raw_files``) in a separate database (default: ``loop_raw``)
holds a row for every raw file that has been ingested (XRD, SEM, etc.) plus
enough metadata to reconstruct provenance if the primary ``loop`` database is
ever lost or moved.

Every row is keyed by ``file_hash`` (a SHA-256 of the file contents), so
repeated uploads of the same file idempotently upsert without duplication.

The collection lives under the ``"raw"`` MongoEngine alias. That alias is
registered in :mod:`loop.settings` alongside the default connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from mongoengine import (
    DateTimeField,
    DictField,
    Document,
    IntField,
    ListField,
    StringField,
)


RAW_DB_ALIAS = "raw"


def _utc_now() -> datetime:
    """Aware UTC timestamp; mirrors :func:`catalog.documents._utc_now`."""
    return datetime.now(timezone.utc)


class RawFile(Document):
    """One document per ingested raw file.

    The primary ``loop`` database may reference this document by ``file_hash``
    (see :class:`catalog.documents.EmbeddedTrial.file_hash`), but no foreign
    key integrity is enforced at the database layer — the raw DB is a
    portable, self-contained manifest.
    """

    id = StringField(primary_key=True)  # file_hash (sha256 hex)

    original_filename = StringField()
    stored_path = StringField()
    content_type = StringField()
    size_bytes = IntField()

    uploaded_at = DateTimeField(default=_utc_now)
    uploaded_by = StringField()

    # Provenance backlinks into the main catalog. Kept as plain strings so
    # the raw DB stays independently useful without a schema join.
    material_auid = StringField()
    recipe_auid = StringField()
    trial_id = StringField()
    doi = StringField()

    elements = DictField()
    structure_family = StringField()
    notes = StringField()
    tags = ListField(StringField())

    meta = {
        "db_alias": RAW_DB_ALIAS,
        "collection": "raw_files",
        "indexes": [
            "material_auid",
            "recipe_auid",
            "trial_id",
            "doi",
            "uploaded_by",
            "-uploaded_at",
        ],
        "ordering": ["-uploaded_at"],
        "strict": False,
    }

    @property
    def file_hash(self) -> str:
        return self.id


def record_raw_file(
    *,
    file_hash: str,
    material_auid: Optional[str] = None,
    recipe_auid: Optional[str] = None,
    trial_id: Optional[str] = None,
    doi: Optional[str] = None,
    original_filename: Optional[str] = None,
    stored_path: Optional[str] = None,
    content_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    uploaded_by: Optional[str] = None,
    elements: Optional[Dict[str, Any]] = None,
    structure_family: Optional[str] = None,
    notes: Optional[str] = None,
    tags: Optional[list] = None,
) -> None:
    """Idempotently upsert a ``raw_files`` row keyed by ``file_hash``.

    Fields left as ``None`` are not written (so the call is additive when a
    later upload of the same hash brings richer metadata).
    """
    if not file_hash:
        raise ValueError("file_hash is required to record a raw file")

    set_fields: Dict[str, Any] = {}
    if material_auid is not None:
        set_fields["set__material_auid"] = material_auid
    if recipe_auid is not None:
        set_fields["set__recipe_auid"] = recipe_auid
    if trial_id is not None:
        set_fields["set__trial_id"] = trial_id
    if doi is not None:
        set_fields["set__doi"] = doi
    if original_filename is not None:
        set_fields["set__original_filename"] = original_filename
    if stored_path is not None:
        set_fields["set__stored_path"] = stored_path
    if content_type is not None:
        set_fields["set__content_type"] = content_type
    if size_bytes is not None:
        set_fields["set__size_bytes"] = size_bytes
    if uploaded_by is not None:
        set_fields["set__uploaded_by"] = uploaded_by
    if elements is not None:
        set_fields["set__elements"] = elements
    if structure_family is not None:
        set_fields["set__structure_family"] = structure_family
    if notes is not None:
        set_fields["set__notes"] = notes
    if tags is not None:
        set_fields["set__tags"] = list(tags)

    RawFile.objects(id=file_hash).update_one(
        set_on_insert__uploaded_at=_utc_now(),
        upsert=True,
        **set_fields,
    )


__all__ = ["RAW_DB_ALIAS", "RawFile", "record_raw_file"]
