"""Runnable client samples rendered in public documentation.

The files in ``samples/`` are the single source of truth: the developer pages
render them, ``docs/API.md`` mirrors them, and
``catalog/tests/test_api_python_samples.py`` executes them against a live
server. A sample that stops working fails a test rather than quietly shipping
broken copy-paste code.
"""

from functools import lru_cache
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"

# The samples default to this base URL so they read sensibly as standalone
# files. When a page renders one, the deployment's own base URL is substituted
# in, so copied code points at the LOOP the reader is actually looking at.
PLACEHOLDER_BASE_URL = "https://loop.example.edu/api/v1"

#: Sample name -> one-line description used as the caption in documentation.
PYTHON_SAMPLES = {
    "read_materials": "List materials matching a composition and structure family",
    "paginate_materials": "Walk every page of a filtered list",
    "validate_record": "Check a record without writing it",
    "create_experiment": "Record an experimental trial and its synthesis route",
    "upload_xrd_minimal": "The smallest script that posts an XRD pattern",
    "upload_xrd_experiment": "Attach an XRD pattern to a new experimental trial",
    "download_xrd": "Download a stored pattern and its parsed metadata",
    "batch_import": "Import many records, dry run first",
}


@lru_cache(maxsize=None)
def sample_source(name):
    """Return the raw source of one sample script."""
    if name not in PYTHON_SAMPLES:
        raise KeyError(f"Unknown sample: {name}")
    return (SAMPLES_DIR / f"{name}.py").read_text(encoding="utf-8")


def render_sample(name, api_base_url=None):
    """Return a sample with the placeholder base URL pointed at this deployment."""
    source = sample_source(name)
    if api_base_url:
        source = source.replace(PLACEHOLDER_BASE_URL, api_base_url.rstrip("/"))
    return source


def rendered_samples(api_base_url=None):
    """Return every sample keyed by name, ready for a template context."""
    return {name: render_sample(name, api_base_url) for name in PYTHON_SAMPLES}
