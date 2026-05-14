"""B5 — cross-repo contract drift guard
(``feedback_no_more_drifts``).

Pins every memory-server-side producer/consumer to the vendored
``docs/reference/content-control/contracts/v1.yaml``. A schema
change in memory-server WITHOUT a paired update to the contract
file fails this test, surfacing the drift at PR-review time
instead of at deploy time (which is how B4b's CI run #1 caught
the original nested-vs-flat ScanRequest mismatch — that whole
class is exactly what this guard exists to prevent next time).

Three invariants:

* **ScanRequest producer ↔ schema** — every key in
  ``ScanRequestEnvelope.as_amqp_payload`` output appears under
  the schema's ``properties`` map AND every ``required`` field is
  present in the payload. Catches: producer adds a new field
  without updating the contract.

* **Verdict consumer ↔ schema** — every field
  ``ScanVerdictConsumer._apply_verdict`` reads from the payload
  is documented under the Verdict schema's ``properties``.
  Catches: consumer starts depending on a field cc doesn't
  promise to emit.

* **Audit consumer ↔ schema** — same shape for
  ``ScanAuditConsumer._persist_audit``. The nested ``object``
  block is the one stream that uses non-flat shape; the schema
  documents that asymmetry explicitly.

Out of scope for THIS file (and not asserted here):

* The cross-repo file sync — that ``contracts/v1.yaml`` in this
  repo matches the canonical in the audittrace-content-control
  repo byte-for-byte. That's the cc-side B5 PR-A12 territory
  (`tests/test_content_control_contract.py` in the cc repo would
  symmetrically pin the cc-side producer/consumer code against
  ITS canonical, and a separate sync script keeps both files
  aligned).

* HTTP surfaces — `POST /v1/scan` etc. live on the cc side and
  this repo never talks to them directly (the AMQP stream is the
  only memory-server↔cc path).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = (
    REPO_ROOT / "docs" / "reference" / "content-control" / "contracts" / "v1.yaml"
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _schema(contract: dict[str, Any], name: str) -> dict[str, Any]:
    schemas = contract["components"]["schemas"]
    assert name in schemas, f"missing schema {name!r} in {CONTRACT_PATH.name}"
    return schemas[name]


# ─────────────────────────────────────────────────────────────────────
# 1. ScanRequest — memory-server producer ↔ schema
# ─────────────────────────────────────────────────────────────────────


class TestScanRequestProducerMatchesContract:
    def _sample_payload(self) -> dict[str, Any]:
        from audittrace.services.scan_request_publisher import (
            ScanRequestEnvelope,
        )

        env = ScanRequestEnvelope(
            scan_id="00000000-0000-0000-0000-000000000001",
            user_id="alice",
            trace_id="0" * 32,
            object_uri="s3://memory-shared/quarantine/alice/scan/x.pdf",
            object_sha256="0" * 64,
            size_bytes=42,
            claimed_content_type="application/pdf",
            traceparent="00-" + "0" * 32 + "-" + "0" * 16 + "-01",
        )
        return env.as_amqp_payload()

    def test_payload_contains_every_required_field(
        self, contract: dict[str, Any]
    ) -> None:
        payload = self._sample_payload()
        schema = _schema(contract, "ScanRequestStreamEntry")
        missing = [k for k in schema["required"] if k not in payload]
        assert not missing, (
            "Drift: memory-server's ScanRequest publisher does NOT "
            "emit fields that the contract marks `required`. cc-side "
            "will NACK these messages to DLQ. Fix the publisher OR "
            f"update the contract. Missing: {missing}"
        )

    def test_no_undocumented_payload_keys(self, contract: dict[str, Any]) -> None:
        payload = self._sample_payload()
        schema = _schema(contract, "ScanRequestStreamEntry")
        documented = set(schema["properties"].keys())
        extra = sorted(set(payload.keys()) - documented)
        assert not extra, (
            "Drift: memory-server's ScanRequest publisher emits fields "
            "that the contract doesn't document. The cc-side consumer "
            "will silently drop them — which is fine until someone "
            "starts relying on them without updating the contract. "
            "Add to `properties` map OR remove from publisher. "
            f"Undocumented: {extra}"
        )

    def test_no_nested_object_key_regression(self) -> None:
        """The original B4b CI failure was a nested
        ``object.{uri,sha256,size_bytes}`` shape that cc-v0.0.7's
        flat-key parser rejected. Pin the regression."""
        payload = self._sample_payload()
        assert "object" not in payload, (
            "Drift regression: the nested `object` key resurfaced in "
            "ScanRequest payload. cc-v0.0.7 expects flat top-level "
            "`object_uri` / `object_sha256` / `object_size_bytes`. "
            "See git blame on scan_request_publisher.py for history."
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Verdict — memory-server consumer ↔ schema
# ─────────────────────────────────────────────────────────────────────


class TestVerdictConsumerMatchesContract:
    # Fields the consumer (`scan_verdict_consumer.py::_apply_verdict`)
    # reads from the payload. Update this set when the consumer
    # starts reading additional fields — and ALSO update the
    # contract's properties map in the same PR.
    _CONSUMER_READS: set[str] = {"scan_id", "kind"}

    def test_every_consumer_read_is_documented(self, contract: dict[str, Any]) -> None:
        schema = _schema(contract, "VerdictStreamEntry")
        documented = set(schema["properties"].keys())
        undocumented = sorted(self._CONSUMER_READS - documented)
        assert not undocumented, (
            "Drift: memory-server's Verdict consumer reads payload "
            "keys that the contract doesn't document. Means "
            "cc-side isn't required to emit them — at best they're "
            "silently absent, at worst the next cc image drops them "
            "entirely. Add to the contract's VerdictStreamEntry "
            f"`properties` map. Undocumented: {undocumented}"
        )

    def test_consumer_reads_match_actual_source(self) -> None:
        """Belt-and-braces: re-scan ``_apply_verdict`` source for
        ``payload[...]`` / ``payload.get(...)`` calls. If the
        consumer grew a new ``payload["foo"]`` read but
        ``_CONSUMER_READS`` wasn't updated, fail loudly so the
        next test (above) is given the chance to flag the
        contract gap."""
        from audittrace.services import scan_verdict_consumer

        src = Path(scan_verdict_consumer.__file__).read_text(encoding="utf-8")
        actual = _extract_payload_reads(src)
        # Drop the regression suite's own helper names if any leak in.
        actual = {k for k in actual if not k.startswith("_")}
        mismatch = actual.symmetric_difference(self._CONSUMER_READS)
        assert not mismatch, (
            "_CONSUMER_READS is out of sync with the actual "
            "`payload[...]` reads in scan_verdict_consumer._apply_verdict. "
            "Reconcile (and review the contract test above)."
            f" Diff: {mismatch}"
        )


# ─────────────────────────────────────────────────────────────────────
# 3. Audit — memory-server consumer ↔ schema
# ─────────────────────────────────────────────────────────────────────


class TestAuditConsumerMatchesContract:
    # Top-level keys the consumer reads from the payload.
    # `object` is the nested block — handled separately below.
    _CONSUMER_READS_TOPLEVEL: set[str] = {
        "scan_id",
        "verdict",
        "scanner_name",
        "scanner_version",
        "signature_db_hash",
        "threat_name",
        "threat_family",
        "confidence",
        "user_id",
        "trace_id",
        "object",
    }
    _CONSUMER_READS_NESTED_OBJECT: set[str] = {"uri", "sha256"}

    def test_every_consumer_read_is_documented(self, contract: dict[str, Any]) -> None:
        schema = _schema(contract, "AuditStreamEntry")
        documented = set(schema["properties"].keys())
        undocumented = sorted(self._CONSUMER_READS_TOPLEVEL - documented)
        assert not undocumented, (
            "Drift: memory-server's Audit consumer reads top-level "
            "payload keys the contract doesn't document. "
            f"Undocumented: {undocumented}"
        )

    def test_nested_object_block_documents_consumer_fields(
        self, contract: dict[str, Any]
    ) -> None:
        schema = _schema(contract, "AuditStreamEntry")
        obj_props = schema["properties"].get("object", {}).get("properties", {})
        documented = set(obj_props.keys())
        undocumented = sorted(self._CONSUMER_READS_NESTED_OBJECT - documented)
        assert not undocumented, (
            "Drift: memory-server's Audit consumer reads nested "
            "`object.{...}` keys the contract's nested `object` "
            "block doesn't document. "
            f"Undocumented: {undocumented}"
        )


# ─────────────────────────────────────────────────────────────────────
# Source-scan helper
# ─────────────────────────────────────────────────────────────────────


_PAYLOAD_READ_RE = re.compile(
    r"""payload\s*
        (?:
            \[\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\]      # payload["foo"]
            |
            \.\s*get\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"  # payload.get("foo"
        )
    """,
    re.VERBOSE,
)


def _extract_payload_reads(source: str) -> set[str]:
    """Return every key name accessed via ``payload[...]`` or
    ``payload.get(...)`` in the given Python source. Used by the
    consumer-side drift tests to detect when new reads were
    introduced without updating the corresponding ``_CONSUMER_READS``
    allowlist."""
    out: set[str] = set()
    for m in _PAYLOAD_READ_RE.finditer(source):
        # Match alternation: one of group 1 (bracket form) or
        # group 2 (.get form) will be populated.
        out.add(m.group(1) or m.group(2))
    return out
