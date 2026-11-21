"""Export-time scrub rules for media payloads inside any telemetry string (pure).

``logging_config.scrub_text`` is the one chokepoint every span attribute and log
record already passes; it calls these rules, so a payload is dropped wherever it
turns up, in a module that exists today or one added later, without naming sinks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from audittrace.media.json_walk import map_strings

MEDIA_MARK = "[REDACTED:media]"
#: Longer than a hex SHA-512 (128 characters): every digest this service emits
#: survives; a payload does not. A run is dropped only above this length.
BASE64_RUN_MIN = 128
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{" + str(BASE64_RUN_MIN + 1) + r",}={0,2}")
#: ``data:<type>[;params];base64,<payload>``: the payload is dropped, also when the
#: value is a truncated one (the live exposure was a truncated attribute).
#:
#: This is NOT a regular expression: the equivalent pattern backtracks quadratically on
#: caller text (a code-scanning finding: ``base64,`` followed by ``data:+;data:+...``
#: took ~20 s for 112 KB). The scan below anchors on the literal ``;base64,`` and
#: validates BACKWARDS, and every character is visited a bounded number of times (see
#: :func:`scrub_data_url_payloads`).
_MARKER = ";base64,"
_TYPE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.+/-"
)
_PAYLOAD_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_NOT_PARAM_CHARS = frozenset(';,"\\')


def _is_token_char(char: str) -> bool:
    """A character a ``;``-separated parameter may hold (not ``; , " \\`` or whitespace)."""
    return char not in _NOT_PARAM_CHARS and not char.isspace()


def _data_start(token: str) -> int:
    """Offset in ``token`` of the ``data:`` that opens ``data:<type>``, or ``-1``.

    The token must END with ``data:`` + one or more type characters. The type holds no
    ``:``, so the only candidate is the token's LAST ``:``.
    """
    colon = token.rfind(":")
    if colon < 4 or token[colon - 4 : colon + 1] != "data:":
        return -1
    rest = token[colon + 1 :]
    if not rest or not all(char in _TYPE_CHARS for char in rest):
        return -1
    return colon - 4


def _opening(value: str, low: int, marker: int) -> int:
    """Start of the leftmost ``data:<type>(;<param>)*`` that ends at ``marker``, or ``-1``.

    Walks back from ``marker`` over token characters and ``;`` only, never past ``low``
    (the end of the previous replacement). Tokens are the ``;``-separated pieces; the
    first one carries ``data:<type>`` as a suffix, every later one is a non-empty param.
    """
    begin = marker
    while begin > low and (value[begin - 1] == ";" or _is_token_char(value[begin - 1])):
        begin -= 1
    tokens = value[begin:marker].split(";")
    last_empty = max(
        (k for k, token in enumerate(tokens) if k and not token), default=0
    )
    offset = sum(len(token) + 1 for token in tokens[:last_empty])
    for token in tokens[last_empty:]:
        found = _data_start(token)
        if found >= 0:
            return begin + offset + found
        offset += len(token) + 1
    return -1


def scrub_data_url_payloads(value: str) -> str:
    """Replace the payload after ``base64,`` of every data URL.

    Complexity (linear in ``len(value)``): ``str.find`` locates each literal
    ``;base64,`` once, left to right. The backward walk from a marker stops at the first
    character that cannot be part of a ``data:`` URL head; every marker ends with ``,``,
    which is not such a character, so the walks of two markers never overlap, and the
    token split, the last-``:`` test and the payload scan read each character of their
    own span a bounded number of times. No nested or overlapping quantifier, no
    backtracking: the output equals the former pattern's on every input
    (``tests/test_media_unit.py`` compares both on a few hundred thousand strings).
    """
    if "base64," not in value:
        return value
    pieces: list[str] = []
    copied = 0
    search = 0
    size = len(value)
    while (marker := value.find(_MARKER, search)) >= 0:
        search = marker + 1
        payload = marker + len(_MARKER)
        end = payload
        while end < size and value[end] in _PAYLOAD_CHARS:
            end += 1
        if end == payload or _opening(value, copied, marker) < 0:
            continue
        pieces.append(value[copied:payload])
        pieces.append(MEDIA_MARK)
        copied = search = end
    pieces.append(value[copied:])
    return "".join(pieces)


def scrub_base64_runs(value: str) -> str:
    """Drop every long base64 run (a payload that has no ``data:`` marker).

    Must run AFTER every secret rule of ``scrub_text``: placed first it replaced
    part of a registered secret and left the secret's head unredacted.
    """
    if len(value) <= BASE64_RUN_MIN:
        return value
    return _BASE64_RUN_RE.sub(MEDIA_MARK, value)


def scrub_strings(value: Any, scrub: Callable[[str], str]) -> Any:
    """``scrub`` applied to every string leaf of a JSON-shaped value (a copy, iterative)."""
    return map_strings(value, scrub)
