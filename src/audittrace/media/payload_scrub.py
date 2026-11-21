"""Export-time scrub rules for media payloads inside any telemetry string (pure).

``logging_config.scrub_text`` is the one chokepoint every span attribute and log
record already passes; it calls these rules, so a payload is dropped wherever it
turns up, in a module that exists today or one added later, without naming sinks.
"""

from __future__ import annotations

import re
from typing import Any

MEDIA_MARK = "[REDACTED:media]"
#: Longer than a hex SHA-512 (128 characters): every digest this service emits
#: survives; a payload does not. A run is dropped only above this length.
BASE64_RUN_MIN = 128
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{" + str(BASE64_RUN_MIN + 1) + r",}={0,2}")
#: ``data:<type>[;params];base64,<payload>``: the payload is dropped, also when the
#: value is a truncated one (the live exposure was a truncated attribute).
_DATA_URL_RE = re.compile(
    r"(data:[A-Za-z0-9.+/-]+(?:;[^;,\"\\\s]+)*;base64,)[A-Za-z0-9+/=]+"
)


def scrub_data_url_payloads(value: str) -> str:
    """Replace the payload after ``base64,`` of every data URL."""
    if "base64," not in value:
        return value
    return _DATA_URL_RE.sub(r"\1" + MEDIA_MARK, value)


def scrub_base64_runs(value: str) -> str:
    """Drop every long base64 run (a payload that has no ``data:`` marker).

    Must run AFTER every secret rule of ``scrub_text``: placed first it replaced
    part of a registered secret and left the secret's head unredacted.
    """
    if len(value) <= BASE64_RUN_MIN:
        return value
    return _BASE64_RUN_RE.sub(MEDIA_MARK, value)


def scrub_strings(value: Any, scrub: Any) -> Any:
    """``scrub`` applied to every string leaf of a JSON-shaped value (a copy)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_strings(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_strings(v, scrub) for v in value]
    return value
