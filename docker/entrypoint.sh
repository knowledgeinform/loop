#!/bin/sh
set -eu

if [ -n "${SQLITE_PATH:-}" ]; then
  mkdir -p "$(dirname "$SQLITE_PATH")"
fi

echo "Applying database migrations..."
python manage.py migrate --noinput

if [ "${CHEMSCREEN_BOOTSTRAP_ON_START:-0}" = "1" ]; then
  echo "Ensuring Predictions-tab ChemScreen data..."
  python manage.py ensure_prediction_data --minimum 20
fi

echo "Ensuring vector search indexes..."
python manage.py mongo_admin ensure-vector-indexes || echo "Warning: vector index setup failed — search may be degraded"

# Report on the JSON archive, never repair it. An empty archive beside a
# populated database prints an instruction to run `loop_archive export`; it
# does not run it, because the correct response to "these disagree" depends on
# which one is right, and that is an operator's call, not a boot script's.
echo "Checking JSON archive..."
python manage.py loop_archive status --warn-on-drift || echo "Warning: archive status check failed"

if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ]; then
  echo "Compiling SCSS..."
  python manage.py compile_scss --style "${SCSS_STYLE:-expanded}" || echo "SCSS compilation skipped"
fi

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting: $*"
exec "$@"
