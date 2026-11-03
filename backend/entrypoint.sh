#!/bin/sh
set -e

# Apply database migrations before starting the API server.
python manage.py migrate --noinput

exec "$@"
