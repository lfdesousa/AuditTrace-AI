"""Tests for ADR-058 recursive self-audit — POST /assessments fan-out +
event_class reconstruction.

The recorder records the evidence of its own security review as first-class
``event_class="assessment"`` rows through its own front door. These tests
cover the fan-out helper (one header + N children) and the end-to-end
record-then-reconstruct path through the audit API.
"""

from __future__ import annotations

import json

from audittrace.models import (
    AssessmentDeferral,
    AssessmentFinding,
    AssessmentIngestRequest,
    AssessmentQuestion,
)
from audittrace.routes.audit import _build_assessment_rows


def _sample_request() -> AssessmentIngestRequest:
    return AssessmentIngestRequest(
        assessment_id="assess-2026-07-14",
        frameworks=["OWASP", "ASVS"],
        rules_of_engagement="own infra; front-door scoped token; no DoS",
        teardown="destroyed to zero",
        questions=[
            AssessmentQuestion(
                question="Can SSRF steal cloud keys?",
                verdict="pass",
                method="verify IMDSv2 + hop-limit",
            )
        ],
        findings=[
            AssessmentFinding(
                finding_id="F-LOW-1",
                severity="low",
                title="one node IMDS hop-limit 2",
                detail="not exploitable; normalise to 1",
            )
        ],
        deferrals=[AssessmentDeferral(item="model-in-the-loop", reason="needs model")],
    )


class TestBuildAssessmentRows:
    """The fan-out helper — one header row plus one child per item, all
    sharing event_class, owner, assessment_id, and trace_id."""

    def test_fan_out_shape_and_row_types(self) -> None:
        rows = _build_assessment_rows(_sample_request(), "user-1", "trace-abc")
        # header + 1 question + 1 finding + 1 deferral
        assert len(rows) == 4
        row_types = [json.loads(r.error_detail or "{}")["row_type"] for r in rows]
        assert row_types == [
            "assessment_header",
            "assessment_question",
            "assessment_finding",
            "assessment_deferral",
        ]

    def test_common_fields_shared_across_rows(self) -> None:
        rows = _build_assessment_rows(_sample_request(), "user-1", "trace-abc")
        for r in rows:
            assert r.event_class == "assessment"
            assert r.user_id == "user-1"
            assert r.session_id == "assess-2026-07-14"
            assert r.trace_id == "trace-abc"

    def test_header_and_children_carry_legible_and_structured_content(self) -> None:
        rows = _build_assessment_rows(_sample_request(), "user-1", None)
        header, question, finding, deferral = rows
        assert header.question == "assessment_header"
        assert header.answer == "assess-2026-07-14"
        assert json.loads(header.error_detail)["frameworks"] == ["OWASP", "ASVS"]
        assert question.answer == "pass"  # the verdict is the legible line
        assert json.loads(finding.error_detail)["finding_id"] == "F-LOW-1"
        assert deferral.question == "model-in-the-loop"


class TestAssessmentEndpoint:
    """Record through the front door, then reconstruct the whole assessment
    with a single event_class + session_id query."""

    def test_record_then_reconstruct(self, client) -> None:
        body = {
            "assessment_id": "assess-x",
            "frameworks": ["OWASP"],
            "rules_of_engagement": "front-door scoped token",
            "questions": [{"question": "SSRF?", "verdict": "pass"}],
            "findings": [
                {"finding_id": "F1", "severity": "low", "title": "hop-limit 2"}
            ],
            "deferrals": [{"item": "model-in-loop"}],
        }
        r = client.post("/assessments", json=body)
        assert r.status_code == 200
        out = r.json()
        assert out["rows_written"] == 4
        assert out["event_class"] == "assessment"

        # Reconstruct: one query returns the whole self-review.
        r2 = client.get("/interactions?event_class=assessment&session_id=assess-x")
        assert r2.status_code == 200
        rows = r2.json()["interactions"]
        assert len(rows) == 4
        assert all(row["event_class"] == "assessment" for row in rows)
        assert all(row["session_id"] == "assess-x" for row in rows)
        # WS-A1: the DB-server-assigned created_at surfaces through the API.
        assert all(row["created_at"] for row in rows)
        # WS-A3: each row carries a content hash that verifies end-to-end.
        from types import SimpleNamespace

        from audittrace.integrity import verify_content_hash

        for row in rows:
            assert row["content_hash"]
            assert verify_content_hash(SimpleNamespace(**row))

    def test_event_class_filter_excludes_non_assessment_rows(self, client) -> None:
        # A plain interaction (default event_class) and an assessment.
        client.post(
            "/assessments",
            json={"assessment_id": "only-this", "questions": [], "findings": []},
        )
        r = client.get("/interactions?event_class=assessment")
        assert r.status_code == 200
        rows = r.json()["interactions"]
        assert len(rows) >= 1
        assert all(row["event_class"] == "assessment" for row in rows)


class _FakeStore:
    """Records put_object calls without touching a real object-store backend."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str | None]] = []

    def put_object(
        self,
        bucket: str,
        key: str,
        data: object,
        length: int,
        content_type: str | None = None,
    ) -> None:
        self.calls.append((bucket, key, length, content_type))


class TestAssessmentArtefact:
    """ADR-058 WS-A4 — the raw payload is stored in object storage and
    referenced by hash from the header row. Best-effort: rows land even with
    no store configured (the other endpoint tests cover that path)."""

    def test_store_helper_puts_and_returns_key_and_sha(self) -> None:
        from audittrace.routes.audit import _store_assessment_artefact

        fake = _FakeStore()
        req = _sample_request()
        key, sha = _store_assessment_artefact(fake, "bucket-x", req)
        assert len(sha) == 64
        assert key == f"assessments/{req.assessment_id}/{sha}.json"
        assert fake.calls[0][0] == "bucket-x"
        assert fake.calls[0][1] == key

    def test_endpoint_stores_artefact_when_store_present(self, client) -> None:
        import audittrace.dependencies as deps

        fake = _FakeStore()
        deps.container._instances["object_storage"] = fake
        try:
            r = client.post(
                "/assessments",
                json={"assessment_id": "a-art", "questions": [], "findings": []},
            )
            out = r.json()
            assert r.status_code == 200
            assert out["artefact_key"]
            assert fake.calls  # put_object was invoked
            assert fake.calls[0][1].startswith("assessments/a-art/")

            rows = client.get(
                "/interactions?event_class=assessment&session_id=a-art"
            ).json()["interactions"]
            header = next(row for row in rows if row["question"] == "assessment_header")
            detail = json.loads(header["error_detail"])
            assert detail["artefact_key"] == out["artefact_key"]
            assert detail["artefact_sha256"]
        finally:
            deps.container._instances.pop("object_storage", None)

    def test_endpoint_constructs_store_on_cache_miss(self, client, monkeypatch) -> None:
        """WS-A4 regression (found in the 2026-07-14 live run): the
        object-storage provider is registered LAZILY, so an assessment that is
        the first storage consumer since pod start hits an empty cache. The
        endpoint must CONSTRUCT the provider (mirroring memory.py's
        ``_get_minio_client`` fallback), not silently skip the artefact."""
        import audittrace.dependencies as deps

        fake = _FakeStore()
        # Ensure a genuine cache miss, then make construction return the fake.
        deps.container._instances.pop("object_storage", None)
        monkeypatch.setattr(
            deps, "_create_object_storage_provider", lambda settings: fake
        )
        r = client.post(
            "/assessments",
            json={"assessment_id": "a-lazy", "questions": [], "findings": []},
        )
        out = r.json()
        assert r.status_code == 200
        assert out["artefact_key"], "cache-miss path must still capture the artefact"
        assert fake.calls  # the constructed provider received the put_object
