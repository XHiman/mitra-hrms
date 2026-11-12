#!/usr/bin/env bash
# Render start command:  bash render_start.sh
# 1. create/upgrade the database schema (safe to repeat; keeps existing data)
# 2. create the first administrator only if the database has no users (never loads demo data into a used DB)
# 3. run the app with Gunicorn: 1 worker (SQLite has a single writer) x 4 threads, on Render's $PORT
set -euo pipefail

export FLASK_APP=run
python -m flask --app run render-init

exec gunicorn run:app \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile -
