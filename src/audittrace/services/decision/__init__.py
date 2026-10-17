"""Decision-model client core (#459 WU-459-1).

A small llama.cpp "decision model" is asked ONE closed multiple-choice
question per call; the answer is read as a distribution over the option
letters (raw logprobs, renormalised client-side at a configured temperature)
and recorded as a self-contained, reconstructible audit row.

This package has NO caller in ``routes/`` in this slice: it is the client
core only (config, template, distribution maths, llama.cpp adapter,
provenance-row builder). Default off.
"""

from audittrace.services.decision.client import LlamaCppDecisionClient
from audittrace.services.decision.errors import ERROR_CODES
from audittrace.services.decision.provenance import build_pending_tool_call
from audittrace.services.decision.result import DecisionResult

__all__ = [
    "ERROR_CODES",
    "DecisionResult",
    "LlamaCppDecisionClient",
    "build_pending_tool_call",
]
