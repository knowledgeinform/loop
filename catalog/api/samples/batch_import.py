"""Import many records at once, dry run first.

Self-contained: save this one file and run it. Nothing to install.

`/imports/` takes up to 100 records and reports each row independently, so one
bad row does not sink the batch. Always dry-run first: it runs the same
validation and writes nothing.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    export LOOP_IMPORT_FILE=experiments.jsonl
    python batch_import.py

Needs a key with the `imports:write` scope.

The file is JSONL — one JSON record per line, in the same shape a single
`/experiments/` write accepts.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")
RECORD_TYPE = "experiment"


def post_import(records, dry_run):
    key = os.environ.get("LOOP_API_KEY")
    if not key:
        raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

    request = urllib.request.Request(
        f"{BASE_URL}/imports/",
        data=json.dumps(
            {"record_type": RECORD_TYPE, "records": records, "dry_run": dry_run}
        ).encode(),
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            # urllib's default User-Agent is rejected by bot protection in front
            # of a LOOP deployment before the request reaches the API.
            "User-Agent": "loop-api-client/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        # An entirely invalid batch is a 422 carrying the per-row errors.
        raise SystemExit(f"LOOP refused the import ({error.code}): {body[:400]}")


def report(response):
    """Print the per-row outcome; `rejected` rows carry their field errors."""
    meta = response["meta"]
    print(
        f"{meta['total']} rows: {meta['created']} created, "
        f"{meta['validated']} validated, {meta['failed']} failed"
    )
    for row in response["data"]:
        if row["status"] == "rejected":
            print(f"  row {row['index']}: {json.dumps(row['errors'])}")
    return meta["failed"]


path = Path(os.environ.get("LOOP_IMPORT_FILE", "experiments.jsonl"))
if not path.is_file():
    raise SystemExit(f"No import file at {path}. Set LOOP_IMPORT_FILE.")

records = []
for line_number, line in enumerate(path.read_text().splitlines(), start=1):
    if not line.strip():
        continue
    try:
        records.append(json.loads(line))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path}:{line_number} is not valid JSON: {error}")

# 1. Dry run. Nothing is written, whatever the rows contain.
print("Dry run:")
if report(post_import(records, dry_run=True)):
    raise SystemExit("Fix the rejected rows before importing.")

# 2. Commit. A partly-successful import answers 207 rather than pretending the
#    whole batch succeeded, so check the rows again here.
print("Import:")
report(post_import(records, dry_run=False))
