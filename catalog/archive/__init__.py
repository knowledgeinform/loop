"""The LOOP JSON archive: the durable, inspectable source of truth.

MongoDB is an index. This package is the record.

Every write that reaches Mongo lands on disk first, as UTF-8 JSON under
``settings.ARCHIVE_ROOT``, alongside the original uploaded bytes. If Mongo is
lost, corrupted, or version-stranded, ``manage.py loop_archive rebuild``
reconstructs it from these files. Nothing here needs Mongo, this codebase, or
Python to be readable in ten years — it is sorted, indented JSON in a directory
tree named after the AUIDs it describes.

Layout::

    ARCHIVE_ROOT/
      ARCHIVE.md                  human-readable format description
      schema_version.json
      records/
        materials/M-<hash>/material.json
        materials/M-<hash>/recipes/R-<short>/recipe.json
        materials/M-<hash>/recipes/R-<short>/trials/<trial_id>/trial.json
        materials/M-<hash>/recipes/R-<short>/literature/<lit_id>.json
        raw-files/<sha256>.json
        users/<username>/{affiliations,precursors/,protocols/}
        xrd-analyses/<analysis_id>/{job.json,reviews/<id>.json}
        ...
      blobs/sha256/<ab>/<cd>/<sha256><ext>
      journal/<YYYY>/<MM>/<YYYY-MM-DD>.jsonl
      .state/

Two representations are maintained, per the design:

*Current state* (``records/``) is what the catalog looks like right now — one
file per record, overwritten in place. This is the fast path for a rebuild and
the thing a human browses.

*The journal* (``journal/``) is append-only and inlines the full body of every
create, update, and delete. It can reconstruct any point in time, and
``loop_archive replay`` rebuilds from it alone to prove it is self-sufficient.

Module map:

- :mod:`catalog.archive.context` — who is writing, and from where
- :mod:`catalog.archive.registry` — which documents are archived, and where they land
- :mod:`catalog.archive.writer` — the single choke point for archive writes
- :mod:`catalog.archive.hooks` — MongoEngine signal wiring
- :mod:`catalog.archive.rebuild` — export, verify, rebuild, replay
"""

from __future__ import annotations

ARCHIVE_SCHEMA_VERSION = 1

__all__ = ["ARCHIVE_SCHEMA_VERSION"]
