"""Public documentation discovery for humans, clients, and LLM agents."""

from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.views.decorators.http import require_GET


def _absolute(request, url_name):
    return request.build_absolute_uri(reverse(url_name))


def _public_text(content, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def llms_txt(request):
    """Serve the llmstxt.org discovery document with deployment-aware URLs."""
    portal = _absolute(request, "developer_portal")
    guide = _absolute(request, "developer_guide")
    markdown = _absolute(request, "api-markdown")
    agent_guide = _absolute(request, "agent-markdown")
    openapi = _absolute(request, "api-v1-openapi-json")
    swagger = _absolute(request, "api-v1-docs")
    redoc = _absolute(request, "api-v1-redoc")
    health = _absolute(request, "api-v1-health")
    docs_url = _absolute(request, "docs-url")
    content = f"""# LOOP

> LOOP is the Learning and Optimization Platform for experimental, literature, and computational materials data, with detailed synthesis routes and AUID-addressable records.

The versioned API base is `{_absolute(request, "api-v1-version").rsplit("version/", 1)[0]}`. Protected operations require an approved LOOP account and a scoped API key. Never place API keys in prompts, source control, or public notebooks.

## Documentation

- [Developer documentation]({portal}): Human guide, first request, materials-data workflows, errors, and rate limits.
- [Integration guide]({guide}): Human-oriented explanation of read, synthesis, XRD-upload, batch-import, and production workflows.
- [API guide in Markdown]({markdown}): Authentication, endpoint map, synthesis schemas, uploads, examples, and rate limits.
- [Agent client guide]({agent_guide}): Safe API-only instructions for external agents; it contains no operator or deployment details.
- [OpenAPI JSON schema]({openapi}): Machine-readable API v1 contract for client and tool generation.
- [Swagger reference]({swagger}): Interactive request and response documentation.
- [Canonical documentation URL]({docs_url}): Plain URI discovery document for the API documentation overview.

## Data interfaces

- [API health]({health}): Public service and API-version health check.
- Experimental records support ordered synthesis steps and optional XRD CSV uploads.
- Literature records support DOI lookup and Crossref metadata.
- Computational records support DFT properties, extended data, and ML prediction payloads.
- Batch imports accept JSON and JSONL with validation-only dry runs.

## Optional

- [ReDoc reference]({redoc}): Alternate rendered OpenAPI reference.
"""
    return _public_text(content, "text/plain; charset=utf-8")


@require_GET
def docs_url(request):
    """Return the canonical human documentation location as a URI list."""
    return _public_text(
        f'{_absolute(request, "developer_portal")}\n',
        "text/uri-list; charset=utf-8",
    )


@require_GET
def api_markdown(request):
    """Expose the maintained API guide without HTML navigation or scripting."""
    guide = Path(settings.BASE_DIR, "docs", "API.md").read_text(encoding="utf-8")
    return _public_text(guide, "text/markdown; charset=utf-8")


@require_GET
def python_sample(request, sample_name):
    """Serve one documented Python sample as a downloadable .py file.

    The developer pages render these inside HTML, which is fine for reading but
    poor for use: copying a client out of a <pre> block is exactly the friction
    that stops someone before their first request. This serves the same file the
    pages render, with the deployment's own base URL already substituted in.
    """
    from catalog.api import code_samples

    api_base_url = _absolute(request, "api-v1-version").rsplit("version/", 1)[0]
    try:
        source = code_samples.render_sample(sample_name, api_base_url)
    except KeyError:
        raise Http404(f"No published sample named {sample_name}.py")
    response = _public_text(source, "text/x-python; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="{sample_name}.py"'
    return response


@require_GET
def agent_markdown(request):
    """Serve the public, API-only copy-paste guide for external agents."""
    api_base_url = _absolute(request, "api-v1-version").rsplit("version/", 1)[0]
    guide = Path(settings.BASE_DIR, "docs", "AGENT.md").read_text(encoding="utf-8")
    replacements = {
        "{{API_BASE_URL}}": api_base_url,
        "{{INTEGRATION_GUIDE_URL}}": _absolute(request, "developer_guide"),
        "{{OPENAPI_URL}}": _absolute(request, "api-v1-openapi-json"),
        "{{API_REFERENCE_URL}}": _absolute(request, "api-v1-docs"),
        "{{HEALTH_URL}}": _absolute(request, "api-v1-health"),
    }
    for placeholder, value in replacements.items():
        guide = guide.replace(placeholder, value)
    return _public_text(guide, "text/markdown; charset=utf-8")
