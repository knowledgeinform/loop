"""The smallest script that posts an XRD pattern to LOOP.

Self-contained: save this one file and run it. Nothing to install. Use it to
prove the round trip works from your machine, then move to
`upload_xrd_experiment.py`, which validates the record first, records the full
synthesis route, and reads the stored pattern back.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    export LOOP_XRD_CSV=pattern.csv
    python upload_xrd_minimal.py

Needs a key with both `data:write` and `files:write`.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")

RECORD = {
    "elements": {"Gd": 2, "Zr": 2, "O": 7},
    "structure_family": "pyrochlore",
    "phase_status": "single_phase",
    "raw_data_type": "xrd",
}

csv_path = Path(os.environ.get("LOOP_XRD_CSV", "pattern.csv"))
key = os.environ.get("LOOP_API_KEY")
if not key:
    raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

# The standard library has no multipart encoder, so build the body by hand.
# Every line break in the envelope must be CRLF.
boundary = uuid.uuid4().hex
body = bytearray()
body += b"--%s\r\n" % boundary.encode()
body += b'Content-Disposition: form-data; name="record"\r\n\r\n'
body += b"%s\r\n" % json.dumps(RECORD).encode()
body += b"--%s\r\n" % boundary.encode()
body += (
    b'Content-Disposition: form-data; name="csv_file"; filename="%s"\r\n'
    % csv_path.name.encode()
)
body += b"Content-Type: text/csv\r\n\r\n"
body += csv_path.read_bytes() + b"\r\n"
body += b"--%s--\r\n" % boundary.encode()

request = urllib.request.Request(
    f"{BASE_URL}/experiments/",
    data=bytes(body),
    headers={
        "X-API-Key": key,
        "Accept": "application/json",
        # urllib's default User-Agent is rejected by bot protection in front of
        # a LOOP deployment before the request reaches the API.
        "User-Agent": "loop-api-client/1.0",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        created = json.loads(response.read())
except urllib.error.HTTPError as error:
    raise SystemExit(f"LOOP returned {error.code}: {error.read().decode()[:200]}")

# material_auid, recipe_auid, trial_id — the coordinates of the stored trial.
print(json.dumps(created["data"], indent=2))
