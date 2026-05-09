"""ADR-048 ingestion content-control — closed-set constants.

Owns the closed-set enums for the scanner pipeline:

- ``_SCAN_STATUS_CODES`` — values written into ``memory_items.scan_status``
  by the upload route (``pending_scan``) and the verdict consumer
  (``scanning`` / ``scanned_clean`` / ``rejected_malware`` / ``scan_failed``
  / ``scan_unrecoverable``). Closed set; new values require an ADR
  amendment.

- ``_EVENT_CLASS_VALUES`` — values written into ``interactions.event_class``.
  ``"interaction"`` is the existing behaviour (chat/tool calls); ``"security"``
  is added by ADR-048's verdict consumer to distinguish content-control
  audit rows from interaction audit rows so SOC tooling can alert on
  ``rejected_malware`` rows without scanning every interaction row.

The constants are kept here (rather than inline in routes/memory.py) per
the ``memory_pdf`` precedent — domain-specific closed sets live in a
domain-specific module, re-exported from routes/memory.py for the test
pattern (``from audittrace.routes.memory import _SCAN_STATUS_CODES``).

The verdict-consumer path that *writes* these values is shipped in PR-B4
(blocked on PR-A3); this module's purpose in PR-B1 is to lock the
closed-set contract and pin it via tests so PR-B4 can't drift.
"""

from __future__ import annotations

# ── ADR-048 §Failure modes table — six states ──────────────────────────
# pending_scan       → memory-server has PUT to quarantine/, awaiting
#                      content-control to pull from the scan-request
#                      Redis Stream
# scanning           → content-control has claimed the request
# scanned_clean      → verdict.kind == clean; object promoted to
#                      episodic/papers/ and safe to /memory/index
# rejected_malware   → verdict.kind == rejected; quarantine object
#                      deleted; SECURITY audit row emitted
# scan_failed        → verdict.kind == scan_failed; transient scanner
#                      error; entry remains pending for retry
# scan_unrecoverable → max_deliveries exceeded; entry moved to DLQ
_SCAN_STATUS_CODES: frozenset[str] = frozenset(
    {
        "pending_scan",
        "scanning",
        "scanned_clean",
        "rejected_malware",
        "scan_failed",
        "scan_unrecoverable",
    }
)

# ── ADR-048 §Audit trail integration — event_class enum on interactions ──
# "interaction" — the legacy implicit value (chat completions, tool
# calls). Backfilled as the default for migration 012.
# "security"    — content-control verdict propagation (clean and
# rejected outcomes both produce rows; SOC alerting filters on this).
_EVENT_CLASS_VALUES: frozenset[str] = frozenset({"interaction", "security"})

__all__ = ["_SCAN_STATUS_CODES", "_EVENT_CLASS_VALUES"]
