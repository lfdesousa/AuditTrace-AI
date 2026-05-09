"""ADR-048 PR-B3 sub-package — PDF upload 202 flow.

Splits the new quarantine + scan-request producer concerns out of
``routes/memory.py`` so that file does not regrow the LOC bloat
that motivated the recent ``memory_pdf/`` sub-package extraction.

Structure:

* ``manifest.py``   — DB CRUD on ``memory_items`` (insert pending,
                       fetch by scan_id, update scan_status).
* ``quarantine.py`` — MinIO PUT into the quarantine prefix +
                       SHA-256 + content-type sniffing helpers.
* ``router.py``     — POST /memory/upload PDF branch + GET
                       /memory/upload/status?scan_id=...

The POST /upload endpoint in ``routes/memory.py`` keeps owning the
content-type dispatch — it calls into this package only when the
upload is a PDF; markdown/other goes through the legacy direct
path unchanged (Luis's call 2026-05-10).
"""

from __future__ import annotations

from audittrace.routes.memory_upload.router import router

__all__ = ["router"]
