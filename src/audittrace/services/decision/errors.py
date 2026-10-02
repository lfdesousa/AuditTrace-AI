"""Closed ``error_code`` vocabulary and the ``result`` mapping (#459 WU-459-1)."""

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
    }
)

RESULTS: frozenset[str] = frozenset({"ok", "timeout", "error", "unavailable"})

_UNAVAILABLE = frozenset({"disabled", "connect_error"})


def result_for(error_code: str) -> str:
    """Map an error code to the ``result`` enum.

    ``unavailable`` <- disabled, connect_error; ``timeout`` <- timeout;
    ``error`` <- everything else.
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
