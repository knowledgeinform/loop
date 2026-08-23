"""List LOOP materials matching a composition and structure family.

Self-contained: save this one file and run it. Nothing to install — it uses
only the Python standard library.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    python read_materials.py

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
        # urllib sends "Python-urllib/3.x" by default, which bot protection in
        # front of a LOOP deployment rejects before the request reaches the API.
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
    """Pull the human-readable message out of LOOP's problem+json body."""
    body = error.read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("detail", body[:200])
    except ValueError:
        return body[:200]


# `elements` matches materials containing all of the listed symbols. A filter
# LOOP cannot honor is rejected with a 400 rather than silently ignored, so a
# typo never returns an unfiltered population.
response = call(
    "GET",
    "/materials/",
    params={"elements": "Ho,Ti,O", "structure_family": "pyrochlore", "limit": 5},
)

meta = response["meta"]
print(f"Showing {meta['returned']} of {meta['total']} visible materials")
print(f"Filters LOOP applied: {meta['filters_applied']}")

for material in response["data"]:
    # AUIDs are content-derived: the same composition and structure family
    # always resolve to the same material_auid.
    print(material["material_auid"], material["structure_family"])
