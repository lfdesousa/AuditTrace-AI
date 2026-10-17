# AuditTrace LibreChat BFF — a dedicated sidecar image (ADR-042 §5 Option A),
# built and deployed SEPARATELY from the main audittrace-server image. See
# bff/README.md for the placement rationale.
#
# Python 3.12 pin mirrors the pin sites tracked for the main image
# (project_python_version_pin_sites) — a NEW site to keep in sync on any
# future bump, tracked here rather than silently drifting.
FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 bff && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash bff

# System-wide install (root, build time) — simpler than the main image's
# builder/runtime split since this image has no compiled-from-source
# dependency (no git-fetched shared package, no PDF/OCR toolchain).
COPY bff/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bff/ ./bff/

ENV PYTHONPATH=/app

USER bff

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8766/health || exit 1

CMD ["uvicorn", "bff.app:app", "--host", "0.0.0.0", "--port", "8766"]
