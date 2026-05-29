#!/bin/sh
set -eu

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Ensuring vector search indexes..."
python manage.py mongo_admin ensure-vector-indexes || echo "Warning: vector index setup failed — search may be degraded"

if [ "${1:-}" = "python" ] && [ "${2:-}" = "manage.py" ]; then
  echo "Compiling SCSS..."
  python manage.py compile_scss --style "${SCSS_STYLE:-expanded}" || echo "SCSS compilation skipped"

  if [ "${DEV_COLLECTSTATIC:-0}" = "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
  fi

fi

echo "Starting: $*"
exec "$@"
