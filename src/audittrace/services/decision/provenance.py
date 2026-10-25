"""Audit-row builder for a decision (#459 WU-459-1).

Turns a :class:`DecisionResult` into a ``PendingToolCall`` that the existing
flush path can persist (the first caller is WU-459-2; nothing flushes in this
slice).

A decision row is identified by ALL THREE module constants below. Migration
021 does not constrain ``tool_calls.provenance`` (plain nullable
``String(16)``), and the public audit API does not expose ``provenance``, so
through the API a decision row is identified by ``tool_name`` +
``granted_scope`` together. ``granted_scope`` is non-null and the empty string
means "refused" (``db/models.py``), hence the dedicated non-empty value.

``args`` is a canonical JSON object (sorted keys, strict: no NaN/Infinity)
with EXACTLY the keys in :data:`ARGS_KEYS`; failure rows carry every key, with
``null`` where a value is unknown. No state, question or option text is ever
placed in ``args``, ``result_summary`` or ``error``: hashes only. ``args`` is
stored untruncated (``Text`` column; the flush path copies it verbatim).

Identity and clock (Addendum 1 S6): a decision row's id is ``tool_calls.id``
(client-side UUID); its clock anchor is ``tool_calls.started_at``, naive local
time from ``datetime.now()`` in the client; ``tool_calls`` has no
``created_at``.
"""

from __future__ import annotations

import json
from typing import Any

from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.services.decision.errors import STATE_SOURCES
from audittrace.services.decision.result import DecisionResult

TOOL_NAME = "system_one_route"
PROVENANCE = "decision"
GRANTED_SCOPE = "system:decision"

ROW_MODES = frozenset({"shadow", "acting"})

ARGS_KEYS: tuple[str, ...] = (
    "template_id",
    "template_sha256",
    "upstream_chat_template_sha256",
    "question_id",
    "question_sha256",
    "options_sha256",
    "input_sha256",
    "allowed_ids",
    "decision_model",
    "decision_model_digest_configured",
    "server_model_file",
    "server_model_alias",
    "runtime",
    "runtime_version",
    "backend",
    "quantisation",
    "temperature",
    "sampler_params",
    "n_probs",
    "raw_top_logprobs",
    "distribution",
    "choice_index",
    "confidence",
    "entropy",
    "latency_ms",
    "mode",
    "acted",
    "result",
    "error_code",
    "state_max_chars",
    "state_source",
    "artefact_record_sha256",
)


def build_args(
    result: DecisionResult,
    *,
    mode: str,
    acted: bool,
    state_max_chars: int | None = None,
    state_source: str | None = None,
    artefact_record_sha256: str | None = None,
) -> dict[str, Any]:
    """The args mapping (every key of :data:`ARGS_KEYS`, nothing else).

    The three shadow keys (WU-459-2) are copied verbatim; ``None`` means
    "unknown here" (the router fills them). A ``state_source`` outside
    :data:`STATE_SOURCES` is refused: the vocabulary is closed and the P2
    report partitions by it (Addendum 1 S8).
    """
    if state_source is not None and state_source not in STATE_SOURCES:
        raise ValueError(
            f"state_source must be one of {sorted(STATE_SOURCES)}, got {state_source!r}"
        )
    raw = result.raw_top_logprobs
    return {
        "template_id": result.template_id,
        "template_sha256": result.template_sha256,
        "upstream_chat_template_sha256": result.upstream_chat_template_sha256,
        "question_id": result.question_id,
        "question_sha256": result.question_sha256,
        "options_sha256": result.options_sha256,
        "input_sha256": result.input_sha256,
        "allowed_ids": None if result.allowed_ids is None else list(result.allowed_ids),
        "decision_model": result.decision_model,
        "decision_model_digest_configured": result.decision_model_digest_configured,
        "server_model_file": result.server_model_file,
        "server_model_alias": result.server_model_alias,
        "runtime": result.runtime,
        "runtime_version": result.runtime_version,
        "backend": result.backend,
        "quantisation": result.quantisation,
        "temperature": result.temperature,
        "sampler_params": (
            None if result.sampler_params is None else dict(result.sampler_params)
        ),
        "n_probs": result.n_probs,
        # JSON object keys are strings: token ids are stringified here.
        "raw_top_logprobs": None
        if raw is None
        else {str(k): v for k, v in raw.items()},
        "distribution": None
        if result.distribution is None
        else list(result.distribution),
        "choice_index": result.choice_index,
        "confidence": result.confidence,
        "entropy": result.entropy,
        "latency_ms": result.latency_ms,
        "mode": mode,
        "acted": acted,
        "result": result.result,
        "error_code": result.error_code,
        "state_max_chars": state_max_chars,
        "state_source": state_source,
        "artefact_record_sha256": artefact_record_sha256,
    }


def build_pending_tool_call(
    result: DecisionResult,
    *,
    user_id: str,
    agent_type: str,
    mode: str,
    acted: bool = False,
    state_max_chars: int | None = None,
    state_source: str | None = None,
    artefact_record_sha256: str | None = None,
) -> PendingToolCall:
    """Build the audit record for ``result``.

    ``mode`` must be ``shadow`` or ``acting``; ``acted=True`` is refused for a
    ``shadow`` row (a shadow decision never changes what the model sees, and
    the audit trail must not claim otherwise).
    """
    if mode not in ROW_MODES:
        raise ValueError(f"mode must be one of {sorted(ROW_MODES)}, got {mode!r}")
    if acted and mode != "acting":
        raise ValueError("acted=True is only valid for an 'acting' row")
    args = build_args(
        result,
        mode=mode,
        acted=acted,
        state_max_chars=state_max_chars,
        state_source=state_source,
        artefact_record_sha256=artefact_record_sha256,
    )
    summary = f"result={result.result}"
    if result.choice_index is not None:
        summary += f" choice_index={result.choice_index}"
    return PendingToolCall(
        tool_name=TOOL_NAME,
        user_id=user_id,
        agent_type=agent_type,
        args=json.dumps(
            args,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
        result_summary=summary,
        error=result.error_code,
        started_at=result.started_at,
        duration_ms=result.latency_ms,
        granted_scope=GRANTED_SCOPE,
        provenance=PROVENANCE,
    )
