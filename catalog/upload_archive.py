"""
Filesystem archive for every successful upload.

For each completed upload a timestamped folder is written under
``settings.RAW_UPLOADS_ROOT``:

    {type}-{YYYY_MM_DD}-{HH:MM:SS.mmm}-{username}/
        {trial_id}.csv          (trials with a CSV only)
        metadata.jsonl          (one JSON line of provenance)

Folder naming uses milliseconds to make simultaneous uploads from the same
user unique without a database round-trip.

The archive call is *non-fatal*: if writing fails for any reason (disk full,
permissions, etc.) a warning is logged and the upload succeeds normally.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def _archive_root() -> Optional[Path]:
    root = getattr(settings, "RAW_UPLOADS_ROOT", None)
    if not root:
        return None
    return Path(root)


def _folder_name(upload_type: str, username: str, ts: datetime) -> str:
    date_str = ts.strftime("%Y_%m_%d")
    time_str = ts.strftime("%H:%M:%S") + f".{ts.microsecond // 1000:03d}"
    safe_user = username.replace("/", "_").replace("\\", "_")
    return f"{upload_type}-{date_str}-{time_str}-{safe_user}"


def archive_upload(
    *,
    upload_type: str,
    username: str,
    timestamp: datetime,
    metadata: Dict[str, Any],
    media_src_path: Optional[str] = None,
    media_dest_filename: Optional[str] = None,
) -> None:
    """Write one archive folder for a completed upload.

    Parameters
    ----------
    upload_type:
        One of ``"trial"``, ``"literature"``, ``"computational"``.
    username:
        The uploading user's username.
    timestamp:
        The upload timestamp (timezone-aware or naive — used for folder name only).
    metadata:
        Dict of provenance fields; written as a single JSON line to ``metadata.jsonl``.
    media_src_path:
        Absolute path of the already-written media file to copy in (optional).
    media_dest_filename:
        Filename for the copy inside the archive folder (e.g. ``"trial_id.csv"``).
        Defaults to the basename of ``media_src_path``.
    """
    root = _archive_root()
    if root is None:
        return

    try:
        folder_name = _folder_name(upload_type, username, timestamp)
        folder = root / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        if media_src_path:
            src = Path(media_src_path)
            if src.is_file():
                dest_name = media_dest_filename or src.name
                shutil.copy2(src, folder / dest_name)
            else:
                logger.warning("upload_archive: media file not found at %s", media_src_path)

        meta_line = json.dumps(metadata, default=str) + "\n"
        (folder / "metadata.jsonl").write_text(meta_line, encoding="utf-8")

    except Exception:
        logger.warning("upload_archive: failed to write archive for %s/%s", upload_type, username, exc_info=True)
