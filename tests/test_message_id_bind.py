"""CONSOLE-AUDIT-EVIDENCE WU-A -- ``header_message_id`` (G2 grid, AC12, A4).

``X-AuditTrace-Message-Id`` is read once, by ``routes/_message_id_bind.header_message_id``
(a sibling of ``header_session_id``, one concept per file). The shared predicate is
``^(?!\\.{1,2}$)[A-Za-z0-9._:-]{1,255}$`` applied with ``re.fullmatch``; the template-marker
check runs FIRST (A6 A2), then the shape; the WARNING carries the LENGTH and a reason code,
never the value (A1 A4).

Neuters (each alone): ``fullmatch`` -> ``match`` turns the ``abc\\n`` cell RED; drop the
template check turns the template cell's reason code RED; log the value turns
``test_the_warning_carries_length_and_reason_only`` RED; drop the dot lookahead turns the
``.`` / ``..`` cells RED; ``{1,255}`` -> ``{1,256}`` turns the 256 cell RED.
"""

from __future__ import annotations

import base64
import logging
import random
import string
from typing import Any

import pytest

from audittrace.logging_config import StructuredFormatter
from audittrace.routes._message_id_bind import (
    MESSAGE_ID_RE,
    bound_message_id,
    current_message_id,
    header_message_id,
    message_id_rejection,
    stream_with_bound_message_id,
)

LITERAL = r"^(?!\.{1,2}$)[A-Za-z0-9._:-]{1,255}$"
UUID = "b7b3f4e0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
_EVENT = "message_id.header_rejected"


class _Req:
    def __init__(self, value: str | None) -> None:
        self.headers: dict[str, str] = (
            {} if value is None else {"x-audittrace-message-id": value}
        )


ACCEPTED = [UUID, "...", "a..b", "x" * 255, "a:b_c-d.e", "A"]
# value -> reason code (the order is template, dot_only, too_long, invalid_charset)
REJECTED = {
    ".": "dot_only",
    "..": "dot_only",
    "../x": "invalid_charset",
    "a/b": "invalid_charset",
    "abc\n": "invalid_charset",
    "a\nb": "invalid_charset",
    "a b": "invalid_charset",
    "café": "invalid_charset",
    "x" * 256: "too_long",
    "{{LIBRECHAT_BODY_MESSAGEID}}": "template",
    "a{{b": "template",
    # A2: the template check runs BEFORE the length check
    "{{" + "x" * 300: "template",
    "b}}": "template",
}


def test_the_pattern_is_the_pinned_literal() -> None:
    assert MESSAGE_ID_RE.pattern == LITERAL


@pytest.mark.parametrize("value", ACCEPTED)
def test_accepted(value: str) -> None:
    assert header_message_id(_Req(value)) == value
    assert message_id_rejection(value) is None


@pytest.mark.parametrize("value", sorted(REJECTED), ids=lambda v: repr(v)[:30])
def test_rejected_is_none_with_a_reason_code(value: str) -> None:
    assert header_message_id(_Req(value)) is None
    assert message_id_rejection(value) == REJECTED[value]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_or_blank_is_none_and_silent(
    value: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert header_message_id(_Req(value)) is None
    assert [r for r in caplog.records if r.getMessage() == _EVENT] == []


def test_surrounding_spaces_are_trimmed() -> None:
    assert header_message_id(_Req(f"  {UUID} ")) == UUID


def test_a_trailing_newline_is_rejected_by_fullmatch_not_match() -> None:
    import re

    assert re.match(LITERAL, "abc\n") is not None  # what a `match` neuter would accept
    assert MESSAGE_ID_RE.fullmatch("abc\n") is None


def _forbidden(value: str) -> set[str]:
    part = value.replace("{{", "").replace("}}", "")
    raw = value.encode()
    forms = {value} | {part[i : i + 4] for i in range(len(part) - 3)}
    forms |= {raw.hex(), raw.hex().upper()}
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = encoded.decode()
        forms |= {text, text.rstrip("=")}
    return forms


_VARIED = "".join(random.Random(20261123).choices(string.ascii_letters, k=300))
_WARN_CASES = {
    "invalid_charset": "Qm7x/Rk9Lp4Wv8Tn3Bz6",
    "too_long": _VARIED,
    "dot_only": "..",
    "template": "{{Q7mx2Rk9Lp4Wv8Tn3Bz6}}",
}


@pytest.mark.parametrize("reason", sorted(_WARN_CASES))
def test_the_warning_carries_length_and_reason_only(
    reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Every record of every logger, every attribute and the real structured line hold
    none of the value's forms; exactly one event with exactly ``{header_length, reason}``."""
    value = _WARN_CASES[reason]
    bare = frozenset(
        logging.LogRecord("n", logging.WARNING, "p", 1, "m", None, None).__dict__
    )
    with caplog.at_level(logging.DEBUG):
        assert header_message_id(_Req(value)) is None
    formatter = StructuredFormatter()
    forbidden = _forbidden(value) if len(value) <= 40 else {value, value[:40]}
    for record in caplog.records:
        line = formatter.format(record)
        for attr, attr_value in record.__dict__.items():
            assert not [f for f in forbidden if f in str(attr_value)], (
                record.name,
                attr,
            )
        assert not [f for f in forbidden if f in line], (record.name, line)
    rows = [r for r in caplog.records if r.getMessage() == _EVENT]
    assert len(rows) == 1
    extras = set(rows[0].__dict__) - bare - {"message", "asctime"}
    assert extras == {"header_length", "reason"}, extras
    assert getattr(rows[0], "header_length") == len(value)
    assert getattr(rows[0], "reason") == reason


class TestBinding:
    def test_unbound_is_none(self) -> None:
        assert current_message_id() is None

    def test_bound_inside_the_block_and_restored_after(self) -> None:
        with bound_message_id("m-1"):
            assert current_message_id() == "m-1"
            with bound_message_id(None):
                assert current_message_id() is None
            assert current_message_id() == "m-1"
        assert current_message_id() is None

    def test_restored_when_the_block_raises(self) -> None:
        with pytest.raises(RuntimeError):
            with bound_message_id("m-2"):
                raise RuntimeError("boom")
        assert current_message_id() is None

    async def test_a_stream_rebinds_while_it_runs_and_restores_after(self) -> None:
        seen: list[Any] = []

        async def body() -> Any:
            seen.append(current_message_id())
            yield b"a"
            seen.append(current_message_id())
            yield b"b"

        chunks = [c async for c in stream_with_bound_message_id("m-3", body())]
        assert chunks == [b"a", b"b"]
        assert seen == ["m-3", "m-3"]
        assert current_message_id() is None
