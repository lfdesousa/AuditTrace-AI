# OpenAI-compatible LLM stub — deterministic canned responses for
# wiring/integration tests (no GPU, no inference). See server.py.
#
# Deps are installed at BUILD time (not container boot) so the pod
# starts in <2 s instead of paying a ~10 s pip-install every restart.
FROM python:3.12-slim

# Versions pinned to match the retired kind fixture for parity (see
# requirements.in). DEP-LOCK-AT (ADR-065): installed from the HASHED lock beside
# this Dockerfile — transitives included, wheels only. The build context is
# images/llm-stub/ (server.py is COPYed from it), so requirements.txt is too.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt

WORKDIR /app
COPY server.py /app/server.py

# Single listen port; the chart's three llm Services (chat/embed/
# summarizer) port-remap to this.
EXPOSE 8080

# Non-root for baseline hardening (parity with chart securityContext).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin stub
USER 10001

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
