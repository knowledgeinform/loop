"""Check a record against LOOP's validation rules without writing it.

Self-contained: save this one file and run it. Nothing to install.

`/records/validate/` runs the same normalization as a real write and stores
nothing. Use it while you are still shaping a payload, and before committing a
batch — it is the cheapest way to find a bad field.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    python validate_record.py

Needs a key with the `data:write` scope, even though nothing is created.
"""

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")

# A synthesis route is an ordered list: each step is one operation, in the order
# it was performed. Unknown keys inside a step are dropped by normalization.
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
    f"{BASE_URL}/records/validate/",
    data=json.dumps({"record_type": "experiment", "record": RECORD}).encode(),
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
        result = json.loads(response.read())["data"]
except urllib.error.HTTPError as error:
    raise SystemExit(f"LOOP returned {error.code}: {error.read().decode()[:200]}")

if not result["accepted"]:
    print("Rejected:")
    print(json.dumps(result["errors"], indent=2))
    raise SystemExit(1)

# `normalized` is exactly what a write would store: canonical element ordering,
# coerced numeric types, and the resolved material AUID.
print("Accepted. LOOP would store:")
print(json.dumps(result["normalized"], indent=2, default=str))
