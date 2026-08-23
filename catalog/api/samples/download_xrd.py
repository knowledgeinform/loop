"""Download the XRD pattern stored on a LOOP trial.

Self-contained: save this one file and run it. Nothing to install.

LOOP keeps the exact bytes you uploaded, so a reanalysis starts from the
original file rather than a re-export. The same trial also exposes the parsed
metadata LOOP derived from it.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    export LOOP_RECIPE_AUID='M:...:R:...'
    export LOOP_TRIAL_ID='1'
    python download_xrd.py

Needs a key with the `data:read` scope.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")


def call(path, raw=False):
    key = os.environ.get("LOOP_API_KEY")
    if not key:
        raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

    request = urllib.request.Request(
        f"{BASE_URL}/{path.lstrip('/')}",
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            # urllib's default User-Agent is rejected by bot protection in front
            # of a LOOP deployment before the request reaches the API.
            "User-Agent": "loop-api-client/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SystemExit(
                "No XRD file on that trial, or it is not visible to your account."
            )
        raise SystemExit(f"LOOP returned {error.code}: {error.read().decode()[:200]}")
    return payload if raw else json.loads(payload)


recipe_auid = os.environ.get("LOOP_RECIPE_AUID")
trial_id = os.environ.get("LOOP_TRIAL_ID")
if not recipe_auid or not trial_id:
    raise SystemExit("Set LOOP_RECIPE_AUID and LOOP_TRIAL_ID.")

trial_path = f"/recipes/{recipe_auid}/trials/{trial_id}"

# This endpoint serves the CSV itself as a file attachment, not a JSON
# envelope, so read the raw response bytes.
pattern = call(f"{trial_path}/xrd/", raw=True)
destination = Path(os.environ.get("LOOP_XRD_OUT", f"{trial_id}.csv"))
destination.write_bytes(pattern)
print(f"Wrote {len(pattern)} bytes to {destination}")

metadata = call(f"{trial_path}/xrd/metadata/")
print(json.dumps(metadata["data"]["metadata"], indent=2, default=str))

# `/xrd/preview/` renders the pattern server-side and returns a base64 data
# URI, which is useful for a notebook or report without reimplementing LOOP's
# plotting.
