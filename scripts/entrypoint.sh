#!/usr/bin/env bash
# Container entrypoint — run Alembic migrations then start uvicorn
set -euo pipefail

echo "Running database migrations..."
python -m alembic upgrade head

echo "Starting audittrace-ai..."
exec uvicorn audittrace.server:app \
    --host "${AUDITTRACE_HOST:-0.0.0.0}" \
    --port "${AUDITTRACE_PORT:-8765}" \
    --workers "${AUDITTRACE_WORKERS:-1}"
