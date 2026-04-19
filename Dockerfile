# Stage 1: Build dependencies
FROM python:3.14-slim AS builder

WORKDIR /build

# Install from the locked requirements file so the image always matches
# pyproject.toml. The previous hard-coded list silently omitted
# opentelemetry-instrumentation-fastapi + opentelemetry-instrumentation-logging,
# which made FastAPI request spans invisible in Langfuse and triggered the
# "No module named 'opentelemetry.instrumentation'" warning at startup.
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime image
FROM python:3.14-slim AS runtime

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
