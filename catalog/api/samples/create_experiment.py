"""Record an experimental trial and its synthesis route in LOOP.

Self-contained: save this one file and run it. Nothing to install.

This is the JSON-only write: the synthesis route without an attached data file.
To send an XRD pattern with it, use `upload_xrd_experiment.py` instead.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    python create_experiment.py

Needs a key with the `data:write` scope.
"""

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")

# The same record `validate_record.py` checks. Validating first is cheap; this
# script writes, so run that one while you are still shaping the payload.
RECORD = {
    "elements": {"Ho": 2, "Ti": 2, "O": 7},
    "structure_family": "pyrochlore",
    "phase_status": "single_phase",
    "comments": "Created through the documented LOOP API v1 example.",
    "synthesis_steps": [
        {
            "step_type": "ball_milling",
            "milling_time_hours": 8,
            "milling_rpm": 250,
            "ball_powder_ratio": "10:1",
            "atmosphere": "air",
        },
        {
            "step_type": "heat_treatment",
            "max_temp_c": 1500,
            "hold_time_hours": 6,
            "atmosphere": "air",
        },
    ],
}

key = os.environ.get("LOOP_API_KEY")
if not key:
    raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

request = urllib.request.Request(
    f"{BASE_URL}/experiments/",
    data=json.dumps(RECORD).encode(),
    headers={
        "X-API-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        # urllib's default User-Agent is rejected by bot protection in front of
        # a LOOP deployment before the request reaches the API.
        "User-Agent": "loop-api-client/1.0",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        identity = json.loads(response.read())["data"]
except urllib.error.HTTPError as error:
    body = error.read().decode("utf-8", "replace")
    # An identical trial on the same recipe is refused rather than duplicated.
    if error.code == 409:
        raise SystemExit(f"An identical trial already exists: {body[:200]}")
    raise SystemExit(f"LOOP returned {error.code}: {body[:200]}")

# LOOP derives the material and recipe AUIDs from the content: the same
# composition and route always resolve to the same identifiers, and a second
# trial on that route is added to the existing recipe.
print(json.dumps(identity, indent=2))
