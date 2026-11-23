"""#61 WU-2: ``SemanticService.stored_corpus_rows`` on the services the route
tests do not exercise.

The route reads the STORED corpus-tier rows through this method before a PUT
writes through. The base class must fail closed (a service that cannot say
what is stored is never a write-through); the user-scoped wrapper must hand
the call to the service it wraps; the mock holds one store, so a corpus-tier
document is its corpus row.
"""

from __future__ import annotations

import pytest
from langchain_core.documents import Document

from audittrace.identity import UserContext
from audittrace.services.semantic import (
    MockSemanticService,
    UserScopedSemanticService,
)
from tests.test_memory_tool_handlers import _SpySemanticService

USER = UserContext(
    user_id="00000000-0000-0000-0000-00000000a001",
    username="u",
    agent_type="test",
    scopes=(),
    is_admin=False,
)


def _mock_with(*metadatas: dict[str, object]) -> MockSemanticService:
    service = MockSemanticService()
    service._docs["decisions"] = [
        Document(page_content="text", metadata=m) for m in metadatas
    ]
    return service


async def test_the_base_class_fails_closed() -> None:
    service = _SpySemanticService([])
    with pytest.raises(NotImplementedError, match="_SpySemanticService cannot read"):
        await service.stored_corpus_rows("decisions", "doc-c")


async def test_the_mock_returns_a_corpus_tier_document_as_the_corpus_row() -> None:
    meta: dict[str, object] = {"document_id": "doc-c", "tier": "corpus", "k": "v"}
    service = _mock_with(meta, {"document_id": "other", "tier": "corpus"})
    assert await service.stored_corpus_rows("decisions", "doc-c") == (None, meta)


@pytest.mark.parametrize(
    "stored",
    [
        {"document_id": "doc-c", "tier": "private"},
        {"document_id": "doc-c"},
        {"document_id": "elsewhere", "tier": "corpus"},
    ],
)
async def test_the_mock_has_no_corpus_row_for_anything_else(
    stored: dict[str, object],
) -> None:
    service = _mock_with(stored)
    assert await service.stored_corpus_rows("decisions", "doc-c") == (None, None)
    assert await service.stored_corpus_rows("missing", "doc-c") == (None, None)


async def test_the_user_scoped_wrapper_hands_the_call_to_the_wrapped_service() -> None:
    meta: dict[str, object] = {"document_id": "doc-c", "tier": "corpus"}
    wrapper = UserScopedSemanticService(_mock_with(meta), USER)
    assert await wrapper.stored_corpus_rows("decisions", "doc-c") == (None, meta)
