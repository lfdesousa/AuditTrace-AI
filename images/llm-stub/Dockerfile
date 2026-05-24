# OpenAI-compatible LLM stub — deterministic canned responses for
# wiring/integration tests (no GPU, no inference). See server.py.
#
# Deps are installed at BUILD time (not container boot) so the pod
# starts in <2 s instead of paying a ~10 s pip-install every restart.
FROM python:3.12-slim

# Versions pinned to match the retired kind fixture for parity.
RUN pip install --no-cache-dir 'fastapi==0.115.0' 'uvicorn[standard]==0.32.0'

WORKDIR /app
COPY server.py /app/server.py

# Single listen port; the chart's three llm Services (chat/embed/
# summarizer) port-remap to this.
EXPOSE 8080

# Non-root for baseline hardening (parity with chart securityContext).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin stub
USER 10001

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
