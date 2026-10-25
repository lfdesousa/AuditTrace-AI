"""Closed ``error_code`` vocabulary and the ``result`` mapping (#459 WU-459-1).

WU-459-2 adds the two shadow-only codes ``skipped`` (queue overflow or open
breaker: no network call) and ``cancelled`` (the decision was cut off by
shutdown), the breaker code set and the state-source vocabulary.
"""

from __future__ import annotations

# The ONLY error codes a DecisionResult may carry (tests assert equality
# against this set, so a new code cannot be added silently).
ERROR_CODES: frozenset[str] = frozenset(
    {
        "disabled",
        "connect_error",
        "timeout",
        "http_status",
        "malformed_response",
        "missing_probabilities",
        "option_not_single_token",
        "template_token_unresolved",
        "no_allowed_token_in_top",
        "model_identity_mismatch",
        "invalid_input",
        "internal_error",
        "skipped",
        "cancelled",
    }
)

RESULTS: frozenset[str] = frozenset({"ok", "timeout", "error", "unavailable"})

# The ONLY codes that count towards opening the shadow breaker (Addendum 1
# S5): transport and endpoint faults. Every other code, including
# ``invalid_input``, ``disabled``, ``skipped`` and ``cancelled``, never trips.
BREAKER_CODES: frozenset[str] = frozenset({"connect_error", "timeout", "http_status"})

# Where the shadow task took the decision state from (Addendum 1 S8): the
# caller-supplied ``context_query``, the last user message, or nothing.
STATE_SOURCES: frozenset[str] = frozenset({"context_query", "last_user_message", "empty"})  # fmt: skip

_UNAVAILABLE = frozenset({"disabled", "connect_error", "skipped", "cancelled"})


def result_for(error_code: str) -> str:
    """Map an error code to the ``result`` enum.

    ``unavailable`` <- disabled, connect_error, skipped, cancelled;
    ``timeout`` <- timeout; ``error`` <- everything else.
    """
    if error_code in _UNAVAILABLE:
        return "unavailable"
    if error_code == "timeout":
        return "timeout"
    return "error"


class DecisionError(Exception):
    """Internal control-flow: a step failed with a closed-vocabulary code.

    Never leaves :meth:`LlamaCppDecisionClient.decide`. The message is the
    code only: it must never carry response bodies or user text.
    """

    def __init__(self, error_code: str) -> None:
        if error_code not in ERROR_CODES:
            raise ValueError(f"unknown error_code {error_code!r}")
        super().__init__(error_code)
        self.error_code = error_code
