"""AC-18 seal check for the WU-2c live-E2E evidence folder.

Run BEFORE ``SHA256SUMS`` is finalised. Fails (exit 1) if any file under
the folder contains either

* a three-segment base64url string of >= 20 chars per segment (a JWT; a
  32-hex ``trace_id`` cannot match), or
* a ``refresh_token`` / ``accessToken`` / ``device_code`` assignment
  (``key =`` or ``key :``) — an opaque secret the JWT regex cannot see
  (the ``device_code`` is bearer-equivalent for its lifetime).

Evidence prose must therefore say "refresh-token key absent", never
``refresh_token: absent``. Only file NAMES are reported, never matched
text, so the check cannot itself leak a secret into a log.

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
SECRET_KEY_PATTERN = re.compile(r"(refresh_token|accessToken|device_code)\s*[=:]")


def scan(root: Path) -> list[Path]:
    """Files under ``root`` that match either pattern (sorted). An
    unreadable file is reported as a hit — the check fails closed."""
    hits: list[Path] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            hits.append(path)
            continue
        if JWT_PATTERN.search(text) or SECRET_KEY_PATTERN.search(text):
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
