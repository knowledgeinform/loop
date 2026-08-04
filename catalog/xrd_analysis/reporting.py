from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from .schemas import to_jsonable


def dumps_canonical_json(value: Any) -> str:
    """Serialize schema payloads into deterministic, JSON-safe text."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))


def sha256_digest(value: Any) -> str:
    """Return a deterministic SHA-256 digest of the canonical JSON payload."""
    return hashlib.sha256(dumps_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 canonical JSON bytes for hashing or atomic writes."""
    return dumps_canonical_json(value).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    """Return the SHA-256 hex digest for already-serialized bytes."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest for a filesystem artifact."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def detected_package_versions(*packages: str) -> dict[str, str | None]:
    """Resolve installed package versions without failing when a package is absent."""
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def python_version_string() -> str:
    """Stable Python runtime version string for manifests."""
    return sys.version.split()[0]


__all__ = [
    "canonical_json_bytes",
    "detected_package_versions",
    "dumps_canonical_json",
    "python_version_string",
    "sha256_bytes",
    "sha256_digest",
    "sha256_file",
    "to_jsonable",
]
