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

from audittrace.logging_config import StructuredFormatter
from audittrace.routes._session_bind import SESSION_ID_RE, header_session_id

LITERAL = r"^[\x21-\x7e]{1,255}$"


class _Req:
    def __init__(self, value: str | None) -> None:
        self.headers: dict[str, str] = {} if value is None else {"x-session-id": value}


#: Attributes every ``LogRecord`` has before anyone adds ``extra=``.
_BARE_RECORD_KEYS = frozenset(
    logging.LogRecord("n", logging.WARNING, "p", 1, "m", None, None).__dict__
)
#: Keys the installed ``TraceContextFilter`` may stamp (LOG-TRACE-ID) ...
_FILTER_STAMPED = frozenset({"trace_id", "span_id", "user_id", "session_id"})
#: ... and keys the capture handler's formatter adds while formatting.
_FORMATTER_STAMPED = frozenset({"message", "asctime"})
_EVENT = "session_id.header_rejected"


def _windows(value: str, size: int = 4) -> set[str]:
    """Every ``size``-character slice of the value's non-marker part."""
    part = value.replace("{{", "").replace("}}", "")
    return {part[i : i + size] for i in range(len(part) - size + 1)}


def assert_rejection_logs_the_length_only(
    caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """The shared F2 / F2-r3 assertion, on the RECORD (``caplog.text`` never
    shows ``extra=`` fields, but ``StructuredFormatter`` ships every one of
    them to Loki):

    * exactly one ``session_id.header_rejected`` record;
    * its ``extra=`` attributes are exactly ``{"header_length"}`` (filter- and
      formatter-stamped ids are listed above and tolerated);
    * ``header_length == len(value)``;
    * no attribute, in its ``str()`` form, holds the value or any 4-character
      slice of it;
    * the real ``StructuredFormatter`` line holds neither the value nor a slice.
    """
    rows = [r for r in caplog.records if r.getMessage() == _EVENT]
    assert len(rows) == 1
    record = rows[0]
    extras = set(record.__dict__) - _BARE_RECORD_KEYS
    assert extras - _FILTER_STAMPED - _FORMATTER_STAMPED == {"header_length"}, extras
    assert getattr(record, "header_length") == len(value)
    needles = {value} | _windows(value)
    for attr, attr_value in record.__dict__.items():
        rendered = str(attr_value)
        leaked = [n for n in needles if n in rendered]
        assert not leaked, (attr, leaked)
    line = StructuredFormatter().format(record)
    assert not [n for n in needles if n in line], line


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
    """Neuters: log the value, or any slice of it, under any ``extra=`` key on
    the shared warning (for example ``header_value``) -> RED."""
    secret = "tok-" + "z" * 300
    with caplog.at_level(logging.WARNING, logger="audittrace.routes._session_bind"):
        assert header_session_id(_Req(secret)) is None
    assert_rejection_logs_the_length_only(caplog, secret)


@pytest.mark.parametrize(
    "value",
    ["{{" + "z" * 40, "z" * 40 + "}}", "{{" + "z" * 40 + "}}"],
    ids=["open-only", "close-only", "both"],
)
def test_a_template_rejection_logs_the_length_only_never_the_value(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """F2 (A3 log property). A value that PASSES the shape regex but carries a
    template marker is rejected with exactly ONE ``session_id.header_rejected``
    warning that carries ``header_length`` and nothing of the value.

    Neuters: give the template check its own branch that logs the raw value
    (in the message or under any ``extra=`` key, prefix included) -> RED; make
    the template branch reject silently -> RED."""
    assert SESSION_ID_RE.fullmatch(value) is not None
    with caplog.at_level(logging.WARNING, logger="audittrace.routes._session_bind"):
        assert header_session_id(_Req(value)) is None
    assert_rejection_logs_the_length_only(caplog, value)
