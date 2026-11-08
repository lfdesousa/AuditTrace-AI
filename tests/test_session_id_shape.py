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

import base64
import logging
import random
import string

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
#: Keys the capture handler's formatter adds while formatting. NOTHING else is
#: tolerated: caplog's handler carries no ``TraceContextFilter``, so a
#: ``trace_id`` / ``session_id`` / ``user_id`` extra on the event can only come
#: from the code under test (an encoded value hidden there would ship to Loki).
_FORMATTER_STAMPED = frozenset({"message", "asctime"})
_EVENT = "session_id.header_rejected"

#: Varied characters on purpose: a 4-character slice check is meaningless on a
#: single repeated character.
_TEMPLATE_CORE = "Q7mx2Rk9Lp4Wv8Tn3Bz6"
_SHAPE_VALUE = "".join(
    random.Random(20261108).choices(string.ascii_letters + string.digits, k=256)
)


def _forbidden_forms(value: str) -> set[str]:
    """Every form of ``value`` that must appear nowhere in the log output:

    * the value itself;
    * each 4-character slice of its non-marker part;
    * its hex encoding (lower and upper case);
    * its base64 encoding, standard and urlsafe, with and without padding.

    Known limit: other transforms (reversal, a different cipher) are not
    covered; the property cannot be proven against every transform.
    """
    part = value.replace("{{", "").replace("}}", "")
    raw = value.encode()
    forms = {value} | {part[i : i + 4] for i in range(len(part) - 3)}
    hexed = raw.hex()
    forms |= {hexed, hexed.upper()}
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = encoded.decode()
        forms |= {text, text.rstrip("=")}
    return forms


def assert_rejection_logs_the_length_only(
    caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """The shared F2 assertion. Capture EVERY record from EVERY logger at DEBUG
    (the caller sets the level) and check on the records, not on
    ``caplog.text``, which never shows ``extra=`` fields while
    ``StructuredFormatter`` ships every one of them to Loki:

    * every captured record, every attribute's ``str()`` and its real
      ``StructuredFormatter`` line hold none of ``_forbidden_forms(value)``
      (so a second warning, a second logger or an encoded extra is caught);
    * exactly one ``session_id.header_rejected`` record, whose ``extra=`` keys
      (record keys minus a bare ``LogRecord``'s, minus the formatter's
      ``message`` / ``asctime``) are exactly ``{"header_length"}``;
    * ``header_length == len(value)``.
    """
    forbidden = _forbidden_forms(value)
    formatter = StructuredFormatter()
    for record in caplog.records:
        line = formatter.format(record)
        for attr, attr_value in record.__dict__.items():
            rendered = str(attr_value)
            hit = [f for f in forbidden if f in rendered]
            assert not hit, (record.name, attr, hit)
        hit = [f for f in forbidden if f in line]
        assert not hit, (record.name, line)
    rows = [r for r in caplog.records if r.getMessage() == _EVENT]
    assert len(rows) == 1
    extras = set(rows[0].__dict__) - _BARE_RECORD_KEYS - _FORMATTER_STAMPED
    assert extras == {"header_length"}, extras
    assert getattr(rows[0], "header_length") == len(value)


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
    """A 256-character value of varied characters fails the shape check.

    Neuters: the value, a slice, its hex or base64 under ANY extra key
    (``session_id`` and ``user_id`` included), a second warning or a second
    logger carrying it -> RED."""
    caplog.set_level(logging.DEBUG)
    assert SESSION_ID_RE.fullmatch(_SHAPE_VALUE) is None
    assert header_session_id(_Req(_SHAPE_VALUE)) is None
    assert_rejection_logs_the_length_only(caplog, _SHAPE_VALUE)


@pytest.mark.parametrize(
    "value",
    [
        "{{" + _TEMPLATE_CORE,
        _TEMPLATE_CORE + "}}",
        "{{" + _TEMPLATE_CORE + "}}",
    ],
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
    caplog.set_level(logging.DEBUG)
    assert SESSION_ID_RE.fullmatch(value) is not None
    assert header_session_id(_Req(value)) is None
    assert_rejection_logs_the_length_only(caplog, value)
