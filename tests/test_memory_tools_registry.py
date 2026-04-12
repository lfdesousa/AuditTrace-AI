"""Tests for the memory-tool registry primitives (ADR-025 §Decision.3).

The registry is the single source of truth for which memory tools exist,
what scopes they require, and what their OpenAI-spec tool definition
looks like. Everything downstream — `tools_visible_to`, the tool-call
loop, the audit row, the Langfuse trace — reads from this table.

Tests cover:

  - Decorator-based registration at import time
  - Duplicate-name registration raises
  - OpenAI-spec tool definition shape produced by `tools_visible_to`
  - Scope filtering: non-admin only sees tools matching their scopes;
    admin bypasses the filter entirely (consistent with Phase 2
    sentinel behaviour)
  - TOML config overlay: disable, retune scope, rename, override
    description
  - Config overlay cannot introduce a new handler (unknown name skipped
    with a WARNING)
  - Registry is resettable between tests (fixture isolation)
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sovereign_memory.identity import sentinel_user_context
from sovereign_memory.tools import (
    MEMORY_TOOL_REGISTRY,
    MemoryTool,
    load_config_overrides,
    register_memory_tool,
    reset_registry_for_tests,
    tools_visible_to,
)

# ───────────────────────────── Fixtures ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts with an empty registry and ends leaving it empty.

    This is deliberately autouse so the test order can never leak state.
    Module-level decorator registration in production code is a one-time
    import-time side effect; these tests exercise the primitive by hand.
    """
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


async def _noop_handler(user_context, args):  # noqa: ARG001
    """Handler stand-in for tests that only care about registration."""
    return {"matches": [], "total": 0, "truncated": False}


# ─────────────────────────── Decorator registration ─────────────────────────


class TestRegistration:
    def test_decorator_registers_tool_by_name(self):
        @register_memory_tool(
            name="recall_decisions",
            description="Recall past architectural decisions.",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            required_scope="memory:episodic:read",
        )
        async def recall_decisions(user_context, args):
            return {"matches": [], "total": 0, "truncated": False}

        assert "recall_decisions" in MEMORY_TOOL_REGISTRY
        tool = MEMORY_TOOL_REGISTRY["recall_decisions"]
        assert isinstance(tool, MemoryTool)
        assert tool.name == "recall_decisions"
        assert tool.required_scope == "memory:episodic:read"
        assert tool.enabled is True

    def test_decorator_preserves_function(self):
        """The decorator returns the function unchanged so it can still be
        called directly by tests or other code paths."""

        @register_memory_tool(
            name="recall_skills",
            description="Recall a skill.",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:procedural:read",
        )
        async def recall_skills(user_context, args):
            return {"called": True}

        # Direct call still works
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            recall_skills(sentinel_user_context(), {})
        )
        assert result == {"called": True}

    def test_duplicate_name_raises(self):
        @register_memory_tool(
            name="recall_decisions",
            description="first",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:read",
        )
        async def first(user_context, args):
            return {}

        with pytest.raises(ValueError, match="already registered"):

            @register_memory_tool(
                name="recall_decisions",
                description="second",
                parameters_schema={"type": "object", "properties": {}},
                required_scope="memory:episodic:read",
            )
            async def second(user_context, args):
                return {}


# ───────────────────────── tools_visible_to scope gate ──────────────────────


class TestScopeFilter:
    @pytest.fixture
    def _four_tools(self):
        @register_memory_tool(
            name="recall_decisions",
            description="Recall ADRs.",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            required_scope="memory:episodic:read",
        )
        async def recall_decisions(user_context, args):
            return {}

        @register_memory_tool(
            name="recall_skills",
            description="Recall skills.",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            required_scope="memory:procedural:read",
        )
        async def recall_skills(user_context, args):
            return {}

        @register_memory_tool(
            name="recall_recent_sessions",
            description="Recall sessions.",
            parameters_schema={
                "type": "object",
                "properties": {"project": {"type": "string"}},
            },
            required_scope="memory:conversational:read-own",
        )
        async def recall_recent_sessions(user_context, args):
            return {}

        @register_memory_tool(
            name="recall_semantic",
            description="Semantic search.",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            required_scope="memory:semantic:read",
        )
        async def recall_semantic(user_context, args):
            return {}

    def test_admin_sees_every_tool(self, _four_tools):
        """The sentinel fixture is admin-by-construction (Phase 2 behaviour);
        admins bypass the scope filter entirely."""
        admin = sentinel_user_context()
        assert admin.is_admin is True
        visible = tools_visible_to(admin)
        names = {t["function"]["name"] for t in visible}
        assert names == {
            "recall_decisions",
            "recall_skills",
            "recall_recent_sessions",
            "recall_semantic",
        }

    def test_non_admin_sees_only_scoped_tools(self, _four_tools):
        """A plain user with only two scopes sees only those two tools."""
        user = replace(
            sentinel_user_context(),
            is_admin=False,
            scopes=("memory:episodic:read", "memory:semantic:read"),
        )
        visible = tools_visible_to(user)
        names = {t["function"]["name"] for t in visible}
        assert names == {"recall_decisions", "recall_semantic"}

    def test_non_admin_with_no_memory_scopes_sees_nothing(self, _four_tools):
        user = replace(
            sentinel_user_context(),
            is_admin=False,
            scopes=("unrelated:scope",),
        )
        assert tools_visible_to(user) == []

    def test_openai_spec_shape(self, _four_tools):
        """The visible-to result matches OpenAI's tools array shape so the
        proxy can forward it verbatim to llama.cpp alongside OpenCode's own
        tools."""
        admin = sentinel_user_context()
        visible = tools_visible_to(admin)
        sample = next(t for t in visible if t["function"]["name"] == "recall_decisions")
        assert sample["type"] == "function"
        assert sample["function"]["name"] == "recall_decisions"
        assert sample["function"]["description"] == "Recall ADRs."
        assert sample["function"]["parameters"] == {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }


# ──────────────────────── TOML config overlay ───────────────────────────────


class TestConfigOverrides:
    def _register_base(self):
        @register_memory_tool(
            name="recall_decisions",
            description="base description",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:read",
        )
        async def h(user_context, args):
            return {}

    def test_disable_tool_via_config(self, tmp_path: Path):
        self._register_base()
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
enabled = false
"""
        )
        load_config_overrides(cfg)
        assert MEMORY_TOOL_REGISTRY["recall_decisions"].enabled is False
        # Disabled tools do not appear in visible-to, even for admins
        assert tools_visible_to(sentinel_user_context()) == []

    def test_retune_required_scope(self, tmp_path: Path):
        self._register_base()
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
required_scope = "tenant:adr:read"
"""
        )
        load_config_overrides(cfg)
        assert (
            MEMORY_TOOL_REGISTRY["recall_decisions"].required_scope == "tenant:adr:read"
        )

    def test_override_description(self, tmp_path: Path):
        self._register_base()
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
description = "tenant-specific description"
"""
        )
        load_config_overrides(cfg)
        assert (
            MEMORY_TOOL_REGISTRY["recall_decisions"].description
            == "tenant-specific description"
        )

    def test_rename_tool(self, tmp_path: Path):
        """The LLM-visible name can be overridden (rare, for deployment
        aliasing). The registry key stays stable so internal code keeps
        working."""
        self._register_base()
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
name = "recall_past_decisions"
"""
        )
        load_config_overrides(cfg)
        assert MEMORY_TOOL_REGISTRY["recall_decisions"].name == "recall_past_decisions"
        visible = tools_visible_to(sentinel_user_context())
        assert visible[0]["function"]["name"] == "recall_past_decisions"

    def test_unknown_tool_in_config_is_skipped_with_warning(
        self, tmp_path: Path, monkeypatch
    ):
        """Config overlay cannot register new handlers. An entry for a tool
        that isn't in the decorator-populated registry is ignored with a
        WARNING log so the operator sees it.

        We monkey-patch the module logger directly rather than relying on
        caplog — pytest's caplog propagation is flaky in this suite (see
        test_context_builder.py:178 for the same workaround), and what we
        actually care about is that the warning was emitted + that the
        base tool is untouched.
        """
        self._register_base()
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.not_a_real_tool]
enabled = true
description = "ghost"
"""
        )
        warnings: list[str] = []
        from sovereign_memory import tools as tools_mod

        def _capture(msg, *args, **kwargs):
            try:
                warnings.append(msg % args if args else msg)
            except Exception:
                warnings.append(str(msg))

        monkeypatch.setattr(tools_mod.logger, "warning", _capture)
        load_config_overrides(cfg)
        assert any("not_a_real_tool" in w for w in warnings), warnings
        # And the base tool is still there, untouched.
        assert "recall_decisions" in MEMORY_TOOL_REGISTRY
        assert "not_a_real_tool" not in MEMORY_TOOL_REGISTRY

    def test_missing_config_file_is_noop(self, tmp_path: Path):
        self._register_base()
        missing = tmp_path / "does_not_exist.toml"
        load_config_overrides(missing)  # should not raise
        # Base registration untouched
        tool = MEMORY_TOOL_REGISTRY["recall_decisions"]
        assert tool.description == "base description"
        assert tool.enabled is True


# ───────────────────── Invoke-helper sanity (no cache yet) ──────────────────
# The registry exposes `get_tool(name)` so the tool-call loop can look a
# tool up by its advertised (possibly overridden) name. This is the thinnest
# possible lookup for Phase 1; Phase 2 adds the cache-aware invoke helper.


class TestLookup:
    def test_get_tool_by_registry_key(self):
        @register_memory_tool(
            name="recall_decisions",
            description="base",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:read",
        )
        async def h(user_context, args):
            return {"ok": True}

        from sovereign_memory.tools import get_tool_by_name

        tool = get_tool_by_name("recall_decisions")
        assert tool is not None
        assert tool.name == "recall_decisions"

    def test_get_tool_by_overridden_name(self, tmp_path: Path):
        """After a rename override the tool-call loop needs to find the
        tool by the NEW name (the LLM calls it by that name)."""

        @register_memory_tool(
            name="recall_decisions",
            description="base",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:read",
        )
        async def h(user_context, args):
            return {"ok": True}

        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
name = "recall_past_decisions"
"""
        )
        load_config_overrides(cfg)

        from sovereign_memory.tools import get_tool_by_name

        tool = get_tool_by_name("recall_past_decisions")
        assert tool is not None
        assert tool.name == "recall_past_decisions"
        # Old name no longer resolves
        assert get_tool_by_name("recall_decisions") is None

    def test_get_tool_disabled_returns_none(self, tmp_path: Path):
        """Disabled tools must not be resolvable — a disabled tool cannot be
        invoked even if the LLM somehow tries (stale tools advertised earlier
        in a conversation)."""

        @register_memory_tool(
            name="recall_decisions",
            description="base",
            parameters_schema={"type": "object", "properties": {}},
            required_scope="memory:episodic:read",
        )
        async def h(user_context, args):
            return {"ok": True}

        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            """
[tools.recall_decisions]
enabled = false
"""
        )
        load_config_overrides(cfg)

        from sovereign_memory.tools import get_tool_by_name

        assert get_tool_by_name("recall_decisions") is None
