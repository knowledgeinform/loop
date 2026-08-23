"""Walk every page of a filtered LOOP material list.

Self-contained: save this one file and run it. Nothing to install.

List endpoints return at most 100 rows. `meta.has_more` — not the length of
`data` — tells you whether another page exists, so this loop terminates
correctly even when the last page happens to be full.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    python paginate_materials.py

Needs a key with the `data:read` scope.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")
PAGE_SIZE = 100


def call(method, path, params=None, body=None, content_type=None, raw=False):
    """Call the LOOP API and return the decoded JSON body."""
    key = os.environ.get("LOOP_API_KEY")
    if not key:
        raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

    url = f"{BASE_URL}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "X-API-Key": key,
        "Accept": "application/json",
        # urllib's default User-Agent is rejected by bot protection in front of
        # a LOOP deployment before the request reaches the API.
        "User-Agent": "loop-api-client/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"LOOP returned {error.code}: {_detail(error)}")
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _detail(error):
    body = error.read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("detail", body[:200])
    except ValueError:
        return body[:200]


def iter_materials(**filters):
    """Yield every visible material matching `filters`, one page at a time."""
    offset = 0
    while True:
        response = call(
            "GET",
            "/materials/",
            params={"limit": PAGE_SIZE, "offset": offset, **filters},
        )
        for material in response["data"]:
            yield material
        # `total` counts matches before the page window is applied, so compare
        # it with what you collected to tell a complete set from a truncated one.
        if not response["meta"]["has_more"]:
            return
        offset += PAGE_SIZE


seen = 0
for material in iter_materials(structure_family="pyrochlore"):
    print(material["material_auid"], material["structure_family"])
    seen += 1
print(f"Walked {seen} materials")
