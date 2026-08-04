# Interactive API Console — Design

Date: 2026-07-14
Branch: ui-overhaul

## Goal

Add a Swagger-style API documentation page at `/api/docs/` that lists every
`/api/` endpoint, documents it, and offers a live "Try it → Execute" console for
the safe, non-mutating GET endpoints. Add a nav link `API` immediately to the
right of *Add Data*.

## Decisions (from brainstorming)

- **Interactive console**, not a static page or an OpenAPI/Swagger-UI bundle.
- **Auto-derived** endpoint catalog: discovered by walking `catalog.urls.urlpatterns`,
  so new `/api/` routes appear automatically. No hand-maintained endpoint list.
- **Document all, execute safe reads only.** Live Execute is enabled only for
  non-mutating GETs; mutating POST/DELETE endpoints are documented but their
  Execute is disabled with a "read-only in console — mutates data" note.
- No new Python/JS dependencies. Pure Django template + vanilla JS + SCSS in the
  existing "Institutional Archive" design system.

## Architecture

### `catalog/api_docs.py` (new)
Single source of truth for the catalog, built by introspection:

- `discover_api_endpoints()` walks `urlpatterns`, keeps routes whose pattern
  starts with `api/`.
- For each route it derives: display path, url name, HTTP method, path params
  (parsed from `<converter:name>` tokens), and description (view docstring).
- **Method / mutates inference:** url name or path containing `delete` → `DELETE`;
  containing `create`/`update` → `POST`; otherwise `GET`. `executable = method == GET and not mutating`.
- **Optional annotations:** an `ANNOTATIONS` dict keyed by url name supplies the
  extra detail that cannot be introspected (query params, example response) for
  the executable GET endpoints. Endpoints without an annotation still render from
  the derived data. This keeps the page automatic while allowing rich "Try it".

### `catalog/views.py`
`@login_required def api_docs(request)` → renders `catalog/api_docs.html` with the
grouped catalog.

### `catalog/urls.py`
`path("api/docs/", views.api_docs, name="api_docs")`.

### `catalog/templates/catalog/api_docs.html` (new)
- Page head + intro.
- Endpoints grouped by section, each a collapsible `<details>` disclosure with a
  method badge, monospace path, description, params table, example response.
- Executable rows include a "Try it" panel (inputs per param → Execute →
  live `fetch()` rendering status + pretty JSON). Mutating rows show a disabled
  Execute with a note.

### `catalog/static/js/api_console.js` (new)
Vanilla JS: reads param inputs, builds the query string, `fetch()` with
`credentials: 'same-origin'` (+ `X-CSRFToken` from cookie for parity), renders the
response. Wired only to executable rows.

### `catalog/static/css/_api-docs.scss` (new) + `main.scss` import
Design-system styling: navy for headings/active only (One Navy Rule); method
badges pair color with text; paths/JSON in monospace (Call-Number Rule); flat
surfaces with 1px hairlines (Line-Not-Shadow Rule). Recompiled via
`manage.py compile_scss`.

### `catalog/templates/base_generic.html`
Add `<li>API</li>` right after the Add Data `<li>`; active when path starts with
`/api/docs`.

## Testing
Django test: `/api/docs/` returns 200 for a logged-in user; response contains each
discovered endpoint path; mutating endpoints render as non-executable.

## Out of scope
No REST framework, no OpenAPI schema generation, no execute on mutating endpoints.
