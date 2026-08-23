from __future__ import annotations

import importlib.metadata
import sys

# The canonical-JSON and hashing primitives moved to :mod:`catalog.canonical` so
# the archive writer and this pipeline share one implementation. They are
# re-exported here unchanged: persisted ``analysis_id`` and ``result_hash``
# values are derived from ``dumps_canonical_json``, so its output is frozen.
from catalog.canonical import (  # noqa: F401
    canonical_json_bytes,
    dumps_canonical_json,
    sha256_bytes,
    sha256_digest,
    sha256_file,
    to_jsonable,
)


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
