"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 gate condition C2.

Naming the writer on the memory server is the switch of the refused-turn route,
and the chart names it only under STRICT mutual TLS. The BFF's own switch
(``AUDITTRACE_BFF_REFUSAL_RECORD_ENABLED``) must be rendered under the SAME
condition, else under PERMISSIVE every refused turn would be posted to a route
that does not exist (404, no audit row).

Neuter (C2): render the BFF's switch on ``istio.enabled`` alone -> the
PERMISSIVE cell.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_chart_refused_turn_and_session_end import RELEASE, _env, _render

BFF_SWITCH = "AUDITTRACE_BFF_REFUSAL_RECORD_ENABLED"
WRITER = "AUDITTRACE_REFUSAL_INGEST_WRITER"


def _both(docs: list[dict[str, Any]]) -> tuple[bool, bool]:
    return (
        BFF_SWITCH in _env(docs, f"{RELEASE}-librechat-bff"),
        WRITER in _env(docs, f"{RELEASE}-memory-server"),
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("STRICT", (True, True)), ("PERMISSIVE", (False, False))],
)
def test_the_sender_and_the_writer_are_on_together_or_off_together(
    mode: str, expected: tuple[bool, bool]
) -> None:
    docs = _render("--set", "console.enabled=true", "--set", f"istio.mtls.mode={mode}")

    assert _both(docs) == expected
