# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install from the locked requirements file so the image always matches
# pyproject.toml. The previous hard-coded list silently omitted
# opentelemetry-instrumentation-fastapi + opentelemetry-instrumentation-logging,
# which made FastAPI request spans invisible in Langfuse and triggered the
# "No module named 'opentelemetry.instrumentation'" warning at startup.
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# curl needed for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd --gid 1000 sovereign && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash sovereign

# Copy installed packages from builder
COPY --from=builder /root/.local /home/sovereign/.local

# Set up PATH for user-installed packages
ENV PATH=/home/sovereign/.local/bin:$PATH
ENV PYTHONPATH=/app/src

# Copy application source and scripts
COPY src/ ./src/
COPY alembic.ini ./
COPY scripts/entrypoint.sh ./scripts/

# Switch to non-root user
USER sovereign

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

# Entrypoint runs migrations then starts uvicorn
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# Stage 3: Tests image
# Inherits from runtime, adds dev deps + tests/. Used by the chart's
# `helm.sh/hook: test` Pod (templates/tests/test-rls.yaml). Runs the
# RLS integration tests against the in-cluster Postgres via the real
# Vault Agent + Istio mTLS path. ADR-043 §"test integration".
FROM runtime AS tests

USER root
RUN pip install --no-cache-dir \
    "pytest>=7.4.0" \
    "pytest-asyncio>=0.23.0" \
    "pytest-mock>=3.12.0" \
    "psycopg2-binary"

COPY tests/ /app/tests/
COPY pyproject.toml /app/

USER sovereign
WORKDIR /app

# Default: run the RLS integration suite. Override via Pod args for
# other test files.
ENTRYPOINT ["pytest"]
CMD ["tests/test_rls_isolation.py", "-v", "--no-cov"]
