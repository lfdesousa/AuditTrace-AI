"""Domain-specific tests for the Tool-Favorites domain AS MIGRATED onto
``services/console_store`` (WU-A reference domain).

The pre-existing ``test_console_tool_favorites_service.py`` /
``_routes.py`` suites are the behaviour-equivalence oracle and are
untouched. This file covers only what the migration itself introduced:
the descriptor shape, the "only supplied fields change" contract now
expressed through the wrapper's patch construction (not a custom merge),
and the cap-invariant-breach warning on ``list``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.services.console_store import MockConsoleStore
from audittrace.services.console_tool_favorites import (
    MAX_TOOL_FAVORITES,
    MockConsoleToolFavoritesService,
    PostgresConsoleToolFavoritesService,
    ToolFavoritesDomain,
    _StoreBackedToolFavoritesService,
)


@pytest_asyncio.fixture
async def pg_service() -> PostgresConsoleToolFavoritesService:
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return PostgresConsoleToolFavoritesService(
        session_factory=factory.get_session_factory()
    )


class TestDescriptor:
    def test_shape(self) -> None:
        domain = ToolFavoritesDomain()
        assert domain.key_columns == ("item_type", "item_id")
        assert domain.value_columns == ("tenant_id", "metadata_json")
        assert domain.cap() == MAX_TOOL_FAVORITES
        assert domain.order_columns()[0] == "created_at_ms"
        assert domain.has_session_id() is False
        assert domain.defaults({"item_type": "tool", "item_id": "x"}) == {
            "tenant_id": None,
            "metadata_json": {},
        }


class TestOnlySuppliedFieldsChange:
    @pytest.mark.parametrize("impl", ["mock", "sqlite"])
    async def test_re_add_without_tenant_or_metadata_keeps_both(
        self,
        impl: str,
        pg_service: PostgresConsoleToolFavoritesService,
        user_context: Any,
    ) -> None:
        service: Any = (
            MockConsoleToolFavoritesService() if impl == "mock" else pg_service
        )
        await service.add_tool_favorite(
            user_context,
            "tool",
            "web-search",
            tenant_id="acme",
            metadata={"pinned": True},
        )
        await service.remove_tool_favorite(user_context, "tool", "web-search")
        re_added = await service.add_tool_favorite(user_context, "tool", "web-search")
        assert re_added["tenant_id"] == "acme", "re-add without tenant_id dropped it"
        assert re_added["metadata"] == {"pinned": True}
        assert re_added["deleted_at_ms"] is None

        updated = await service.add_tool_favorite(
            user_context, "tool", "web-search", metadata={"pinned": False}
        )
        assert updated["tenant_id"] == "acme"
        assert updated["metadata"] == {"pinned": False}


class _UncappedFavorites(ToolFavoritesDomain):
    name = "console_tool_favorites_uncapped"

    def cap(self) -> int | None:
        return None


class TestCapInvariantBreachIsLoud:
    async def test_list_warns_instead_of_silently_truncating(
        self, user_context: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        store: MockConsoleStore[dict[str, Any]] = MockConsoleStore(_UncappedFavorites())
        service = _StoreBackedToolFavoritesService(store)
        for i in range(MAX_TOOL_FAVORITES + 1):
            await service.add_tool_favorite(user_context, "tool", f"item-{i:03d}")
        with caplog.at_level(
            logging.WARNING, logger="audittrace.services.console_tool_favorites"
        ):
            items = await service.list_tool_favorites(user_context)
        assert len(items) == MAX_TOOL_FAVORITES
        assert any("cap invariant breached" in r.getMessage() for r in caplog.records)
