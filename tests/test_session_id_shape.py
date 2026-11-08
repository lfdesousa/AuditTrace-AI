"""LC-10a R-Q5: the accepted shape of a client-supplied ``X-Session-Id``.

The reject set is the LibreChat fork's (its AC-23 pins the same literal):
printable ASCII without space, 1-255 characters, applied with
``re.fullmatch`` (``$`` would accept a trailing newline).

Neuters (each alone): ``fullmatch`` -> ``match`` turns the trailing-LF cell
RED; ``{1,255}`` -> ``{1,256}`` turns the 256 cell RED; drop the length-only
log (log the value) turns the value-not-logged cell RED; ``strip(" ")`` -> no strip
turns the padded cell RED.
"""

from __future__ import annotations

import logging

import pytest

from audittrace.routes._session_bind import SESSION_ID_RE, header_session_id

LITERAL = r"^[\x21-\x7e]{1,255}$"


class _Req:
    def __init__(self, value: str | None) -> None:
        self.headers: dict[str, str] = {} if value is None else {"x-session-id": value}


def test_the_pattern_is_the_pinned_literal() -> None:
    assert SESSION_ID_RE.pattern == LITERAL


@pytest.mark.parametrize(
    "value",
    ["abc\n", "x" * 256, "a\x00b", "a b", "\x7f", "café", "a\r"],
    ids=["trailing-lf", "256", "nul", "space", "del", "non-ascii", "trailing-cr"],
)
def test_regex_rejects(value: str) -> None:
    assert SESSION_ID_RE.fullmatch(value) is None
    assert header_session_id(_Req(value)) is None


PLACEHOLDER = "{{LIBRECHAT_BODY_CONVERSATIONID}}"


@pytest.mark.parametrize(
    "value",
    [PLACEHOLDER, "a{{b", "x}}y", "{{", "}}"],
    ids=["literal", "open-inside", "close-inside", "open", "close"],
)
def test_an_unresolved_template_placeholder_is_rejected(value: str) -> None:
    """A3 (operator ruling 2026-11-08, option 1). The placeholder is valid
    printable ASCII, so the pinned regex alone accepts it; the template clause
    rejects it like any other malformed value (absent -> None). The regex
    literal stays the fork's.

    Neuter: drop the template clause -> every cell RED."""
    assert SESSION_ID_RE.fullmatch(value) is not None
    assert header_session_id(_Req(value)) is None


@pytest.mark.parametrize("value", ["s1", "a" * 255, "!", "~", "run-42_x.y:z/1"])
def test_regex_accepts(value: str) -> None:
    assert SESSION_ID_RE.fullmatch(value) is not None
    assert header_session_id(_Req(value)) == value


def test_space_trimmed_form_is_accepted_per_the_strip_rule() -> None:
    assert header_session_id(_Req(" s1 ")) == "s1"
    assert header_session_id(_Req("  s1")) == "s1"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_or_blank_is_none_without_warning(
    value: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert header_session_id(_Req(value)) is None
    assert not caplog.records


def test_rejection_logs_the_length_only_never_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "tok-" + "z" * 300
    with caplog.at_level(logging.WARNING, logger="audittrace.routes._session_bind"):
        assert header_session_id(_Req(secret)) is None
    rows = [r for r in caplog.records if r.getMessage() == "session_id.header_rejected"]
    assert len(rows) == 1
    assert rows[0].header_length == len(secret)  # type: ignore[attr-defined]
    assert secret not in caplog.text and "zzzz" not in caplog.text
