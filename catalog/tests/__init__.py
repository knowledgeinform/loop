"""Catalog test package (keeps ``catalog/`` uncluttered).

Django loads ``catalog.tests`` as a package; submodules are imported so
discovery picks up all ``test_*.py`` modules consistently.
"""

from . import test_aggregation_browse
from . import test_aggregation_visible
from . import test_archive_rebuild
from . import test_archive_writer
from . import test_http_smoke
from . import test_search
from . import test_views_helpers

__all__ = [
    "test_aggregation_browse",
    "test_aggregation_visible",
    "test_archive_rebuild",
    "test_archive_writer",
    "test_http_smoke",
    "test_search",
    "test_views_helpers",
]
