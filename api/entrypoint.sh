#!/bin/bash
set -e

# Database steps: migrate, load bots, and create the superuser if
# DJANGO_SUPERUSER_PASSWORD is set. They run under a database named lock so that
# instances starting at the same moment take turns instead of applying the same
# migration concurrently. Exits non-zero if the lock is not free within
# MIGRATE_LOCK_TIMEOUT seconds (default 600). See migrate_locked.py.
echo "Applying database migrations and loading bots..."
python manage.py migrate_locked

# Collect static files (per-instance filesystem work, needs no lock)
echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
# Finally, run the command passed in the Dockerfile CMD or via `docker run`
exec "$@"
