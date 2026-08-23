"""Post an XRD pattern to LOOP as a new experimental trial.

Self-contained: save this one file and run it. Nothing to install — it uses
only the Python standard library.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    export LOOP_XRD_CSV=pattern.csv
    python upload_xrd_experiment.py

Needs a key with both `data:write` and `files:write`.

The CSV is two columns — 2-theta angle and intensity — with a header row:

    Angle,Intensity
    10.00,120
    10.02,131
"""

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")

# The trial LOOP will create. `raw_data_type` labels the attached file;
# `phase_status` is the outcome you observed, not a prediction.
RECORD = {
    "elements": {"Gd": 2, "Zr": 2, "O": 7},
    "structure_family": "pyrochlore",
    "phase_status": "single_phase",
    "raw_data_type": "xrd",
    "comments": "Uploaded through the documented LOOP API v1 Python example.",
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
        {"step_type": "xrd_measurement"},
    ],
}


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
        detail = _detail(error)
        # LOOP deduplicates raw data by SHA-256, so re-running this script with
        # the same pattern is refused rather than quietly creating a twin.
        if error.code == 409:
            raise SystemExit(f"Already in LOOP: {detail}")
        raise SystemExit(f"LOOP returned {error.code}: {detail}")
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _detail(error):
    """Pull the human-readable message out of LOOP's problem+json body."""
    body = error.read().decode("utf-8", "replace")
    try:
        problem = json.loads(body)
    except ValueError:
        return body[:200]
    # A 422 names the fields that failed; show them rather than the summary.
    if problem.get("errors"):
        return json.dumps(problem["errors"])
    return problem.get("detail", body[:200])


def encode_multipart(fields, files):
    """Encode form fields and file parts as multipart/form-data.

    The standard library has no multipart encoder, so uploading a pattern
    without third-party packages means building the body yourself. Every line
    break in the envelope must be CRLF.
    """
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += b"--%s\r\n" % boundary.encode()
        body += b'Content-Disposition: form-data; name="%s"\r\n\r\n' % name.encode()
        body += b"%s\r\n" % str(value).encode("utf-8")
    for name, (filename, content) in files.items():
        guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body += b"--%s\r\n" % boundary.encode()
        body += (
            b'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (name.encode(), filename.encode())
        )
        body += b"Content-Type: %s\r\n\r\n" % guessed.encode()
        body += content + b"\r\n"
    body += b"--%s--\r\n" % boundary.encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


csv_path = Path(os.environ.get("LOOP_XRD_CSV", "pattern.csv"))
if not csv_path.is_file():
    raise SystemExit(f"No XRD CSV at {csv_path}. Set LOOP_XRD_CSV.")

# 1. Validate first. Nothing is written, so a malformed record costs one cheap
#    request instead of a half-finished upload.
check = call(
    "POST",
    "/records/validate/",
    body=json.dumps({"record_type": "experiment", "record": RECORD}).encode(),
    content_type="application/json",
)
if not check["data"]["accepted"]:
    print(json.dumps(check["data"]["errors"], indent=2))
    raise SystemExit("Fix the record before uploading the pattern.")

# 2. Upload. A multipart request carries the record as a JSON string in the
#    `record` field and the pattern in `csv_file`. This is the one write that
#    needs `files:write` on top of `data:write`.
body, content_type = encode_multipart(
    {"record": json.dumps(RECORD)},
    {"csv_file": (csv_path.name, csv_path.read_bytes())},
)
identity = call("POST", "/experiments/", body=body, content_type=content_type)["data"]
print("Stored trial:")
print(json.dumps(identity, indent=2))

# 3. Read the pattern back. LOOP parses the CSV on upload, so this confirms it
#    understood the file rather than merely accepting it.
metadata = call(
    "GET",
    f"/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/xrd/metadata/",
)
print("Parsed XRD metadata:")
print(json.dumps(metadata["data"]["metadata"], indent=2, default=str))
