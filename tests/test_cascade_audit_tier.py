"""The cascade delete audit row carries the row-level tier (#472 x #473).

``audit_source_folds`` hands each fold's tier to ``emit_write_audit`` as the
row-level ``tier`` and never restates it in ``detail_extra`` (the audit store
refuses a ``detail_extra`` that restates a row field). Removing the conversion
makes ``emit_write_audit`` raise ``TypeError`` (missing ``tier``), or, if the
tier were left in the detail, the restated-field refusal fires.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from audittrace.identity import UserContext
from audittrace.routes import memory_write_audit
from audittrace.routes.memory_delete_cascade import FoldRow, audit_source_folds


def _user() -> UserContext:
    return UserContext(user_id="u-1", username="u-1", agent_type="test", scopes=())


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_emit(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(memory_write_audit, "emit_memory_audit_event", fake_emit)
    return calls


async def test_cascade_row_tier_is_the_source_tier_and_not_in_detail(
    captured: list[dict[str, Any]],
) -> None:
    folds: list[FoldRow] = [
        (
            "decisions",
            "c0",
            {
                "hard": False,
                "cascade_from": "episodic/a.md",
                "chunks": 2,
                "tier": "private",
            },
        ),
        (
            "skills",
            "c1",
            {
                "hard": False,
                "cascade_from": "procedural/b.md",
                "chunks": 1,
                "tier": "corpus",
            },
        ),
    ]
    before = copy.deepcopy(folds)

    await audit_source_folds(_user(), folds)

    assert [c["tier"] for c in captured] == ["private", "corpus"]
    for call in captured:
        assert "tier" not in call["detail_extra"]
        assert set(call["detail_extra"]) == {"hard", "cascade_from", "chunks"}
    assert folds == before  # the caller's rows are not mutated
