"""AC-18 seal check for the WU-2c live-E2E evidence folders (Addendum E).

Run BEFORE ``SHA256SUMS`` is finalised, and on every review evidence
folder. Fails (exit 1) when any file under the folder contains a secret in
ANY of its forms — the earlier version matched ONE spelling of each key
(``key=value`` with no quote between key and separator) and was blind to
the most common real form, Keycloak's JSON (``"device_code": "<opaque>"``).

A finding is any of:

1. **A secret key with a non-empty value** (key names matched
   case-insensitively: ``access_token``, ``refresh_token``, ``id_token``,
   ``device_code``, ``client_secret``, ``accessToken``, ``refreshToken``,
   ``password``) in JSON (ANY RFC 8259 whitespace - space, tab, LF, CR -
   around the ``:``, string or non-string values, and JSON ESCAPED inside
   a string: ``\\"k\\":\\"v\\"``), YAML (``k: v``), env/ini (``k=v``,
   ``export k=v``) or URL/form (``k=v&``) shape.
2. **A bearer credential:** an ``Authorization:`` header carrying
   ``Bearer``/``Basic`` and a value, or any ``Bearer <16+ chars>`` token;
   the scheme match is case-INSENSITIVE (``bearer x...``).
3. **A JWT-shaped string** (three base64url segments of >= 20 chars; a
   32-hex ``trace_id`` cannot match).
4. **An unreadable or binary file** (fail closed).

A BARE mention of a key name with no value is NOT a finding (the
read-back wording "refresh-token key absent" is how evidence says it);
neither are an empty value, a ``trace_id`` or a ``user_code``. Only file
NAMES are reported, never matched text, so the scan cannot leak a secret
into a log.

**NOT claimed** (outside Addendum E's closed key list and JWT rule; known
limits, not defects): a custom ``token:`` header or ``X-Auth-Token``, a
hyphenated ``access-token`` key, a JWT wrapped across several lines, an
encoded/split secret. A JSON ``null`` value for a listed key (for example
``"client_secret": null``) IS flagged (fail closed; a false positive that
may stay).

Lives under ``scripts/deploy/`` so the per-file coverage floor covers it.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

JWT_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")

SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "device_code",
    "client_secret",
    "accessToken",
    "refreshToken",
    "password",
)
_KEYS = r"(?:(?:" + "|".join(SECRET_KEYS) + r")\b)"
# A value character: not a quote, backslash, whitespace or delimiter - so an
# empty value (``""``, ``\"\"``, ``k=``, ``k:`` at end of line) is not a
# finding and a bare key name is not either.
_VALUE = r"""[^\s"',}&\]\\]"""
# QUOTED key (JSON, also JSON-escaped inside a string: ``\"k\":\"v\"``):
# ANY RFC 8259 whitespace (space, tab, LF, CR) around the colon.
_QUOTED = _KEYS + r"""(?:\\?["'])\s*[:=]\s*(?:\\?["'])?""" + _VALUE
# BARE key (YAML, env/ini, ``export``, URL/form): same-line whitespace only,
# so ``password:`` followed by prose on the next line is not a finding.
_BARE = _KEYS + r"""[ \t]*[:=][ \t]*["']?""" + _VALUE
SECRET_KEY_PATTERN = re.compile(f"(?:{_QUOTED})|(?:{_BARE})", re.IGNORECASE)
AUTH_HEADER_PATTERN = re.compile(
    r"authorization[ \t]*:[ \t]*(?:bearer|basic)[ \t]+\S", re.IGNORECASE
)
BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE
)


def _secret_shaped(text: str) -> bool:
    return bool(
        JWT_PATTERN.search(text)
        or SECRET_KEY_PATTERN.search(text)
        or AUTH_HEADER_PATTERN.search(text)
        or BEARER_TOKEN_PATTERN.search(text)
    )


def scan(root: Path) -> list[Path]:
    """Files under ``root`` that match any form (sorted). An unreadable or
    binary file is reported as a hit — the check fails closed."""
    hits: list[Path] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            data = path.read_bytes()
        except OSError:
            hits.append(path)
            continue
        if b"\x00" in data or _secret_shaped(data.decode("utf-8", errors="replace")):
            hits.append(path)
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args(argv)
    if not args.evidence_dir.is_dir():
        sys.stderr.write("not a directory (fail closed)\n")
        return 2
    hits = scan(args.evidence_dir)
    for path in hits:
        sys.stdout.write(f"SECRET-SHAPED CONTENT: {path}\n")
    if hits:
        return 1
    sys.stdout.write("AC-18 scan: clean (no token-shaped or secret-key content)\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
