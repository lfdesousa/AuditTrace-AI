#!/usr/bin/env bash
# Container entrypoint — run Alembic migrations then start uvicorn
set -euo pipefail

echo "Running database migrations..."
python -m alembic upgrade head

echo "Starting audittrace-ai..."
exec uvicorn sovereign_memory.server:app \
    --host "${SOVEREIGN_HOST:-0.0.0.0}" \
    --port "${SOVEREIGN_PORT:-8765}" \
    --workers "${SOVEREIGN_WORKERS:-1}"
