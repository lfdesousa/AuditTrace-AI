"""``DecisionResult``: the immutable outcome of one ``decide()`` (#459 WU-459-1).

Carries every provenance fact the audit row needs, so
:func:`~audittrace.services.decision.provenance.build_pending_tool_call` is a
pure function of this object. It never carries state, question or option
TEXT, only ids, hashes, enums and numbers.

Two temperatures exist on a row and must not be confused: the top-level
``temperature`` is the CLIENT softmax temperature (always > 0);
``sampler_params["temperature"]`` is what was sent to the runtime (0). A
reconstruction divides the raw logprobs by the CLIENT one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of one decision call. ``result`` is ok|timeout|error|unavailable."""

    result: str
    error_code: str | None
    started_at: datetime
    latency_ms: int
    # --- outcome (None unless result == "ok") ---
    choice_index: int | None = None
    confidence: float | None = None
    entropy: float | None = None
    distribution: tuple[float, ...] | None = None
    # id -> logprob, exactly as returned by the runtime.
    raw_top_logprobs: dict[int, float] | None = None
    allowed_ids: tuple[int, ...] | None = None
    # --- reconstruction inputs / provenance ---
    template_id: str | None = None
    template_sha256: str | None = None
    upstream_chat_template_sha256: str | None = None
    question_id: str | None = None
    question_sha256: str | None = None
    options_sha256: str | None = None
    input_sha256: str | None = None
    decision_model: str = ""
    decision_model_digest_configured: str = ""
    server_model_file: str | None = None
    server_model_alias: str | None = None
    runtime: str = "llama.cpp"
    runtime_version: str | None = None
    backend: str = ""
    quantisation: str = ""
    temperature: float | None = None
    sampler_params: dict[str, Any] | None = field(default=None)
    n_probs: int | None = None
