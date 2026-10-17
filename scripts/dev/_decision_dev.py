"""Shared helpers for the #459 decision-client dev scripts (NOT shipped).

``scripts/dev`` is outside the image (the runtime stage copies only ``src/``)
and outside the coverage source list. Nothing here is imported by ``src/``.

Target and credentials come from the environment, never from the repo:

* ``AUDITTRACE_DECISION_URL``           (default ``http://127.0.0.1:11450``)
* ``AUDITTRACE_DECISION_API_KEY_FILE``  path to a file holding the API key.
  The key is read INSIDE this process and handed to Settings; it is never
  printed, logged or written.
"""

from __future__ import annotations

import os
from pathlib import Path

from audittrace.config import Settings

# Phase-0 recorded sha256 of the tev1-0.8B f16 GGUF.
EXPECTED_F16_DIGEST = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"
PROBE_STATE = (
    "User asked: what did we decide last week about the ACL write path audit rows?"
)
QUESTION_ID = "memory_layer_v1"


def read_api_key() -> str:
    path = os.environ.get("AUDITTRACE_DECISION_API_KEY_FILE", "")
    if not path:
        raise SystemExit("set AUDITTRACE_DECISION_API_KEY_FILE")
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def make_settings(api_key: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        decision_url=os.environ.get(
            "AUDITTRACE_DECISION_URL", "http://127.0.0.1:11450"
        ),
        decision_api_key=api_key,
        decision_model="tev1-0.8B-experimental",
        decision_model_alias=os.environ.get(
            "AUDITTRACE_DECISION_MODEL_ALIAS", "tev1-0.8B-f16"
        ),
        decision_model_digest=EXPECTED_F16_DIGEST,
        decision_timeout_ms=int(
            os.environ.get("AUDITTRACE_DECISION_TIMEOUT_MS", "10000")
        ),
        decision_temperature=1.0,
        decision_backend="vulkan",
        decision_quantisation="f16",
        memory_routing_mode="shadow",
    )
