"""Tests for ADR-058 WS-A3 content-integrity hashing (``integrity.py``)."""

from __future__ import annotations

from audittrace.integrity import content_hash, verify_content_hash


class _Row:
    """Minimal attribute bag standing in for an ORM audit row."""

    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


_BASE: dict[str, object] = {
    "project": "self-audit",
    "source": "security-assessment",
    "question": "q",
    "answer": "a",
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "timestamp": "2026-07-14T10:00:00",
    "session_id": "s1",
    "model": None,
    "user_id": "u1",
    "status": "success",
    "failure_class": None,
    "error_detail": None,
    "duration_ms": None,
    "trace_id": "t1",
    "event_class": "assessment",
}


class TestContentHash:
    def test_key_order_independent(self) -> None:
        h1 = content_hash(dict(_BASE))
        reordered = {k: _BASE[k] for k in reversed(list(_BASE))}
        assert content_hash(reordered) == h1

    def test_is_sha256_hex(self) -> None:
        h = content_hash(dict(_BASE))
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_content_change_changes_the_hash(self) -> None:
        h1 = content_hash(dict(_BASE))
        assert content_hash(dict(_BASE, answer="TAMPERED")) != h1


class TestVerifyContentHash:
    def test_matching_row_verifies(self) -> None:
        row = _Row(content_hash=content_hash(dict(_BASE)), **_BASE)
        assert verify_content_hash(row) is True

    def test_tampered_row_fails(self) -> None:
        row = _Row(content_hash=content_hash(dict(_BASE)), **_BASE)
        row.answer = "TAMPERED"  # mutate after the hash was written
        assert verify_content_hash(row) is False

    def test_row_without_hash_fails(self) -> None:
        assert verify_content_hash(_Row(content_hash=None, **_BASE)) is False
