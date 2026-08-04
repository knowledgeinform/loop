# API Keys — Design

Date: 2026-07-14
Branch: ui-overhaul

## Goal

Let researchers call the LOOP JSON API programmatically (scripts, notebooks) by
authenticating with a per-user API key, in addition to the existing browser
session. Keys grant the owner's **full** access (read + write), scoped to their
affiliation visibility.

## Decisions (from brainstorming)

- **Full access** keys: a valid key acts as the user for both GET reads and the
  mutating user-library endpoints (precursors/protocols CRUD).
- Keys are **keyed to the Django user** — all approval/affiliation/visibility
  rules carry over unchanged; a key never grants more than the user has.
- Header schemes accepted (whatever's easiest for a Python client):
  `Authorization: Bearer <key>`, `Authorization: Token <key>`, or `X-API-Key: <key>`.

## Architecture

### `catalog/documents.py` — `UserApiKey`
Mongo document, mirrors `UserProtocol`/`UserAffiliation`:
```
user_id       IntField(required)   # Django user id
username      StringField          # denormalized for display
name          StringField          # user label, e.g. "notebook laptop"
key_prefix    StringField          # first ~10 chars, shown to identify a key
key_hash      StringField(unique)  # SHA-256 hex of full token — secret never stored
created_at    DateTimeField
last_used_at  DateTimeField
```
Helpers (same module):
- `generate_api_key(user, name) -> (UserApiKey, plaintext)` — token = `loop_` +
  `secrets.token_urlsafe(32)`; store only its SHA-256 hash; return plaintext once.
- `resolve_api_key(raw) -> UserApiKey | None` — SHA-256 the input, look up by hash.
- `_hash_api_key(raw) -> str`.

Token secret is shown **once** at creation and only hashed at rest (SHA-256 is
appropriate — the token is 256-bit random, nothing to brute-force). Revoke =
delete the document.

### `loop/middleware.py` — `ApiTokenAuthMiddleware`
Registered after `AuthenticationMiddleware`, before `ApprovedGateMiddleware`.
- Acts only on `api/` paths.
- If a key header is present: `resolve_api_key`; on hit, load the Django `User`
  (must be active) → set `request.user`, set `request._dont_enforce_csrf_checks
  = True` (token POSTs need no CSRF cookie), bump `last_used_at` (throttled to
  ≤ once / 60s). On miss/invalid → `401 JSON {"error": "invalid API key"}`.
- The resolved user still passes through `ApprovedGateMiddleware` (same
  group/staff rule), so a token grants exactly the user's own access.
- **Cleaner errors:** for the JSON API surface (`api/` except `api/docs`), an
  unauthenticated request returns `401 JSON` instead of the HTML login redirect.
  `api/docs` (HTML) keeps the normal login redirect.

CSRF note: `CsrfViewMiddleware.process_view` runs after this middleware's request
phase, so setting `_dont_enforce_csrf_checks` here is honored even though CSRF is
listed earlier in `MIDDLEWARE`.

### Key management (browser-only, session + CSRF)
Under `account/api-keys/…` (NOT `api/…`, so keys can't mint keys and the routes
never appear in the console):
- `account/api-keys/` (GET) → `api_keys_manage_page`: list keys (label, prefix,
  created, last used) + Revoke; "Generate key" shows the plaintext once.
- `account/api-keys/create/` (POST) → `api_keys_create`.
- `account/api-keys/<key_id>/revoke/` (POST) → `api_keys_revoke`.
Linked from `/account/` next to the precursors/protocols buttons.

### Docs page
Add an **Authentication** section at the top of `/api/docs/`: the two ways to
authenticate (browser session; or a key header, with a curl/Python example) and a
link to `/account/api-keys/`. The live console keeps using the browser session.

## Testing
- Key generate → hash stored, plaintext not; `resolve_api_key` round-trips; revoke.
- Middleware: valid key → view runs as the user with their visibility; bad/revoked
  key → 401 JSON; token POST to a mutating endpoint succeeds without CSRF token.
- Unauthenticated JSON endpoint → 401 JSON; `api/docs` → login redirect.

## Out of scope
No OAuth, no scopes/permissions beyond the user's own access, no rate limiting
beyond the `last_used_at` write throttle.
