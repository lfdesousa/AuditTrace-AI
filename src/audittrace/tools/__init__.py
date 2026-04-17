"""Memory-tool registry primitives (ADR-025 §Decision.3).

This module is the single source of truth for which memory tools exist,
what scopes they require, and what their OpenAI-spec tool definition
looks like. Everything downstream — ``tools_visible_to``, the tool-call
loop, the ``ToolCall`` audit row, the Langfuse trace — reads from this
table.

Contract:

- Tools register at import time via the ``@register_memory_tool``
  decorator. The decorator populates the module-level
  ``MEMORY_TOOL_REGISTRY`` dict keyed on the *registration name*
  (the Python developer's name, not the LLM-visible name).
- ``tools_visible_to(user_context)`` returns the OpenAI-spec
  ``tools`` array filtered by scope. Admins bypass the filter
  entirely (consistent with Phase 2 sentinel behaviour). Disabled
  tools are never included.
- ``load_config_overrides(path)`` applies an optional TOML overlay
  to the decorator-built registry. Overlay can: disable a tool,
  override its ``required_scope``, override its ``description``,
  override its LLM-visible ``name``. Overlay **cannot** add new
  handlers — handlers come from code only.
- ``get_tool_by_name(name)`` resolves an LLM-facing tool name to
  the underlying ``MemoryTool`` instance. Used by the tool-call
  loop to dispatch. Returns ``None`` for unknown or disabled tools.

The registry is resettable via ``reset_registry_for_tests`` so test
fixtures can start from a clean slate without module-reload gymnastics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from audittrace.identity import UserContext

logger = logging.getLogger(__name__)


# ────────────────────────────── MemoryTool ──────────────────────────────────


ToolHandler = Callable[["UserContext", dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class MemoryTool:
    """Immutable description of one memory tool.

    The frozen dataclass shape means config overrides build *new*
    instances via :func:`dataclasses.replace` rather than mutating
    live state; the registry rebinds the registry-key slot to the
    new instance.
    """

    name: str
    """LLM-visible tool name (e.g. ``recall_decisions``). A config
    overlay may override this per deployment without changing the
    registry key."""

    description: str
    """Natural-language description shown to the LLM in the OpenAI
    ``tools`` array. Drives tool selection quality — favour verb-
    oriented phrasing (``Recall ADRs...`` beats ``Search episodic
    layer...``). See ADR-025 §Decision.1."""

    parameters_schema: dict[str, Any]
    """JSON-Schema for the tool arguments. Becomes the
    ``function.parameters`` field of the OpenAI tool definition."""

    required_scope: str
    """OAuth2 scope required to see and invoke this tool.
    ``UserContext.is_admin`` bypasses the filter. See Phase 2
    semantics (ADR-026 §15)."""

    handler: ToolHandler = field(repr=False)
    """Async callable ``(user_context, args) -> dict``. The handler
    is invoked by the tool-call loop; the registry never calls it
    directly. Excluded from ``repr`` to keep log lines tidy."""

    enabled: bool = True
    """Config overlay can disable a tool without removing its
    handler from code. A disabled tool never appears in
    :func:`tools_visible_to` and :func:`get_tool_by_name` returns
    ``None`` for it."""


# ─────────────────────────── Module-level store ─────────────────────────────
# Two maps are kept in sync by every mutator:
#
#   _BY_REGISTRATION_KEY : registration name → MemoryTool
#   _BY_VISIBLE_NAME     : current LLM-visible name → registration name
#
# The separation matters because a config overlay can rename the
# LLM-visible name while the registration key (the Python developer's
# chosen identifier) stays stable for internal references.

MEMORY_TOOL_REGISTRY: dict[str, MemoryTool] = {}
_BY_VISIBLE_NAME: dict[str, str] = {}


# ─────────────────────── Decorator-based registration ──────────────────────


def register_memory_tool(
    *,
    name: str,
    description: str,
    parameters_schema: dict[str, Any],
    required_scope: str,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator that registers a memory tool at import time.

    Usage::

        @register_memory_tool(
            name="recall_decisions",
            description="Recall past architectural decisions.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
            required_scope="memory:episodic:read",
        )
        async def recall_decisions(user_context, args):
            ...

    Raises ``ValueError`` on duplicate registration keys so a typo
    cannot silently shadow an existing tool.
    """

    def _decorate(handler: ToolHandler) -> ToolHandler:
        if name in MEMORY_TOOL_REGISTRY:
            raise ValueError(f"memory tool {name!r} is already registered")
        tool = MemoryTool(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            required_scope=required_scope,
            handler=handler,
            enabled=True,
        )
        MEMORY_TOOL_REGISTRY[name] = tool
        _BY_VISIBLE_NAME[name] = name
        logger.debug("Registered memory tool %r (scope=%s)", name, required_scope)
        return handler

    return _decorate


# ─────────────────────────── Config overlay (TOML) ──────────────────────────


def load_config_overrides(path: Path) -> None:
    """Apply the optional TOML overlay to the decorator-built registry.

    Schema (all keys optional per tool)::

        [tools.<registration_key>]
        enabled        = true | false
        required_scope = "tenant:adr:read"
        description    = "tenant-specific description"
        name           = "recall_past_decisions"  # LLM-visible alias

    Unknown registration keys are logged at WARNING and skipped.
    Missing file is a no-op so deployments without a config file are
    supported out of the box.
    """
    if not path.exists():
        logger.debug("tools config %s not present; registry unchanged", path)
        return

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        logger.warning("failed to parse tools config %s: %s", path, exc)
        return

    tool_overrides = (data.get("tools") or {}) if isinstance(data, dict) else {}
    for reg_key, overrides in tool_overrides.items():
        if reg_key not in MEMORY_TOOL_REGISTRY:
            logger.warning(
                "tools config references unknown tool %r — skipping. "
                "Handlers must be registered in code; the overlay cannot "
                "introduce new tools.",
                reg_key,
            )
            continue
        if not isinstance(overrides, dict):
            logger.warning("tools config entry %r is not a table", reg_key)
            continue
        current = MEMORY_TOOL_REGISTRY[reg_key]
        updates: dict[str, Any] = {}
        if "enabled" in overrides:
            updates["enabled"] = bool(overrides["enabled"])
        if "required_scope" in overrides:
            updates["required_scope"] = str(overrides["required_scope"])
        if "description" in overrides:
            updates["description"] = str(overrides["description"])
        if "name" in overrides:
            new_visible = str(overrides["name"])
            updates["name"] = new_visible

        new_tool = replace(current, **updates)
        MEMORY_TOOL_REGISTRY[reg_key] = new_tool

        # Rebuild the visible-name index entry for this tool.
        # Remove every stale entry pointing at this reg_key first so a
        # rename does not leave the old alias resolvable.
        for vname in [v for v, k in _BY_VISIBLE_NAME.items() if k == reg_key]:
            _BY_VISIBLE_NAME.pop(vname, None)
        _BY_VISIBLE_NAME[new_tool.name] = reg_key

    logger.info("tools config overlay applied from %s", path)


# ──────────────────────── Lookup + scope filtering ──────────────────────────


def tools_visible_to(user_context: UserContext) -> list[dict[str, Any]]:
    """Return the OpenAI-spec ``tools`` array scoped to the caller.

    - Admins see every enabled tool regardless of ``required_scope``
      (matches Phase 2 sentinel semantics so existing tests and the
      ``AUDITTRACE_AUTH_REQUIRED=false`` bypass keep working).
    - Non-admins see only tools whose ``required_scope`` is present
      in their ``UserContext.scopes`` tuple.
    - Disabled tools are never included.

    The shape is exactly what the chat proxy can forward to
    ``llama-server`` alongside OpenCode's own tools, so no mapping
    step is required at the call site.
    """
    visible: list[dict[str, Any]] = []
    for tool in MEMORY_TOOL_REGISTRY.values():
        if not tool.enabled:
            continue
        if not user_context.is_admin and tool.required_scope not in user_context.scopes:
            continue
        visible.append(_to_openai_spec(tool))
    return visible


def get_tool_by_name(name: str) -> MemoryTool | None:
    """Resolve an LLM-facing tool name to the registered ``MemoryTool``.

    Returns ``None`` for unknown, renamed-away, or disabled tools so
    the tool-call loop can treat "tool not found" as a graceful error
    path rather than a crash. Callers should still check scope against
    the resolved tool — this function is purely a name resolver.
    """
    reg_key = _BY_VISIBLE_NAME.get(name)
    if reg_key is None:
        return None
    tool = MEMORY_TOOL_REGISTRY.get(reg_key)
    if tool is None or not tool.enabled:
        return None
    return tool


def _to_openai_spec(tool: MemoryTool) -> dict[str, Any]:
    """Render a ``MemoryTool`` as an OpenAI tools-array entry."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_schema,
        },
    }


# ───────────────────────────── Test support ─────────────────────────────────


def reset_registry_for_tests() -> None:
    """Drop every registered tool and visible-name alias.

    Used by test fixtures to guarantee isolation between tests that
    exercise decorator registration. Production code should never
    call this — tools register at import time and stay registered
    for the process lifetime.
    """
    MEMORY_TOOL_REGISTRY.clear()
    _BY_VISIBLE_NAME.clear()


# ─────────────────── Cache-aware tool invocation helper ────────────────────
# ADR-025 §Decision.8. The tool-call loop dispatches through this function
# so caching, error handling, and the cache-hit signal live in exactly one
# place. Phase 2 provides the handler-facing contract; Phase 4 wires the
# tool-call loop into chat.py to consume it.


def _canonical_cache_id(session_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Build the deterministic cache id for a tool invocation.

    Dict key ordering is irrelevant — the args dict is serialised with
    ``sort_keys=True`` so ``{"q": "x", "k": 4}`` and ``{"k": 4, "q": "x"}``
    produce the same cache id. Session isolation is preserved by
    including ``session_id`` in the hash input.
    """
    raw = f"{session_id}|{tool_name}|{json.dumps(args, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def invoke_tool(
    user_context: UserContext,
    tool: MemoryTool,
    args: dict[str, Any],
    session_id: str,
) -> tuple[dict[str, Any], bool]:
    """Execute a memory tool with Redis-backed result caching.

    Returns a ``(result, was_cache_hit)`` tuple. The boolean tells the
    caller (the tool-call loop) whether to write a ``ToolCall`` audit
    row — cache hits skip the row because the real execution already
    landed when the cache was populated (ADR-025 §Decision.8).

    Error semantics:

    - **Handler raises:** caught here, logged with stacktrace, and
      returned to the LLM as ``{"error": "TypeName: message"}``. The
      cache is not populated on the exception path, so the next call
      re-attempts the handler.
    - **Cache layer fails:** a Redis outage is invisible to the caller;
      ``ToolResultCache`` already degrades to "always miss".
    - **Invalid args:** the handler itself is responsible for validating
      its arguments. Handlers conventionally return an ``{"error": ...}``
      dict for known bad inputs rather than raising.

    Note this helper does not check the scope gate — by the time the
    tool-call loop calls here, the tool was already filtered through
    ``tools_visible_to(user_context)`` at advertisement time. Scope
    enforcement is at the edge; this is the dispatch.
    """
    # Import locally to avoid a module-level cycle with cache → config.
    from audittrace.tools.cache import get_tool_result_cache

    cache = get_tool_result_cache()
    cache_id = _canonical_cache_id(session_id, tool.name, args)

    cached = cache.get(cache_id)
    if cached is not None:
        logger.debug("tool result cache HIT tool=%s session=%s", tool.name, session_id)
        return cached, True

    try:
        result = await tool.handler(user_context, args)
    except Exception as exc:
        logger.exception("memory tool handler %r raised", tool.name)
        return (
            {"error": f"{type(exc).__name__}: {exc}"},
            False,
        )

    # Only cache success shapes. Error results look like {"error": "..."}
    # and must not be replayed — the next call might succeed against a
    # recovered layer.
    if "error" not in result:
        cache.put(cache_id, result)
    return result, False
