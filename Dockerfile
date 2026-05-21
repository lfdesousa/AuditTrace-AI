# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# git is required to fetch the audittrace-object-storage shared
# package (ADR-006) from its git+https URL pin. Apt-cache cleanup
# inline so the layer stays small; the runtime stage does not carry
# git (the shared package is installed into /root/.local in this
# stage and copied across).
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Install from the locked requirements file so the image always matches
# pyproject.toml. The previous hard-coded list silently omitted
# opentelemetry-instrumentation-fastapi + opentelemetry-instrumentation-logging,
# which made FastAPI request spans invisible in Langfuse and triggered the
# "No module named 'opentelemetry.instrumentation'" warning at startup.
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Install the audittrace-ai package itself (no deps — already installed
# above). Without this, the runtime image had no package metadata for
# ``importlib.metadata.version("audittrace-ai")`` to resolve, so
# ``server._resolve_version()`` fell through to whichever stale
# ``src/audittrace_ai.egg-info`` happened to be in the developer's
# working tree (caught 2026-05-09: v1.0.13 image self-reported as
# v1.0.11 because of a frozen dev-side egg-info). ADR-055 §4 — install
# from the current pyproject + sources so the metadata always
# matches the chart appVersion at build time.
COPY pyproject.toml ./
COPY src/ ./src/
COPY README.md ./
RUN pip install --user --no-cache-dir --no-deps .

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# curl for healthcheck; tesseract + language packs for tier-B item #1
# (OCR fallback on raster-only PDF pages, ADR-050).
#
# Image-size cost: tesseract-ocr (~30 MB) + four language packs
# (~10 MB each) ≈ 65 MB total. Default languages = English + the
# three CH national languages (de/fr/it). Adding a language is a
# one-line change here; keeping the default minimal preserves
# cold-start time.
#
# pytesseract (the Python binding) lives in requirements.txt; without
# this apt install the Python helper falls through to the
# ``no_text_layer`` graceful-degradation branch — see
# ``_ocr_render_page`` in routes/memory.py.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-deu \
        tesseract-ocr-fra \
        tesseract-ocr-ita && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd --gid 1000 sovereign && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash sovereign

# Copy installed packages from builder
COPY --from=builder /root/.local /home/sovereign/.local

# Set up PATH for user-installed packages
ENV PATH=/home/sovereign/.local/bin:$PATH
ENV PYTHONPATH=/app/src

# Pre-warm ChromaDB's default embedding model (all-MiniLM-L6-v2, ~79 MB
# ONNX). Without this, the FIRST `collection.upsert()` call from any pod
# triggers an in-process download that blocks the FastAPI worker for
# ~26 s — long enough for kubelet's liveness probe to mark the pod
# unhealthy, kill it mid-request, and CrashLoopBackOff. Found in PR A's
# 2026-05-03 live test (semantic POST 200 then GET/PUT/DELETE 503).
#
# Baking the model into the image layer means every pod that boots has
# it on disk already — no startup hit, no in-request hit, no liveness
# false-positive. Trade-off: ~79 MB image size increase.
#
# Run as root to populate /home/sovereign/.cache, then chown to the
# runtime UID so the sovereign user can read the cached model at
# runtime. ChromaDB's DefaultEmbeddingFunction resolves the cache
# location from $HOME, so we set it explicitly.
RUN HOME=/home/sovereign python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; ef = DefaultEmbeddingFunction(); ef(['prewarm'])" && \
    chown -R 1000:1000 /home/sovereign/.cache

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

# NOTE: the tests image was previously stage 3 of this Dockerfile. It
# moved to a dedicated Dockerfile.tests (Chart-A, 2026-05-13) so that
# `docker build .` (no --target) reliably produces the runtime image.
# When tests was the LAST stage, a missing --target silently produced
# an image whose ENTRYPOINT was pytest — the kubelet then ran it once,
# exited 0, and CrashLoopBackOff'd with `Reason: Completed`. See
# `Dockerfile.tests` for the chart helm-test image build.
