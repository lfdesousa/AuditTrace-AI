"""MCP entry-interface — ADR-063 Phase 1 (read/recall tools) + Phase 2
Track B (the audited tool-broker gateway) + Phase 2 Track A (write/
curation tools).

**Additive.** Mounts a standard MCP transport (streamable-HTTP,
JSON-RPC 2.0 over a single ``POST /mcp``) on a NEW path. Nothing about
``/v1/chat/completions`` changes — this router is registered alongside
it in ``server.py``, never inside it
(``feedback_openai_schema_inviolate``).

**Per-call contract (ADR-063 §Change.3, extended by Phase 2 Track B).**
Every ``tools/call``:

1. **Authorize.** Identity is resolved by ``require_user`` — the SAME
   dependency ``routes/chat.py`` uses. It validates the Keycloak bearer
   JWT (or returns the bypass sentinel when
   ``AUDITTRACE_AUTH_REQUIRED=false``), and binds the
   ``app.current_user_id`` RLS ContextVar before this handler ever
   runs. The per-tool (own-tool) or per-downstream-server (brokered)
   scope check happens inside ``services.mcp_bridge.call_read_tool`` /
   ``services.mcp_broker.broker_tool_call`` respectively — deny means
   no execution/forward and no data, mirroring the chat tool loop's
   defensive re-check.
2. **Execute** (own tool) or **forward** (brokered tool, to the
   operator-configured downstream MCP server) under the caller's
   isolated RLS context (bound in step 1).
3. **Record.** Own-tool calls write ONE tamper-evident audit event;
   brokered calls write TWO (request + result, ADR-063 Phase 2 Track B
   §Decision.2) — both via the SAME ``_persist_interaction`` /
   ``_flush_pending_tool_calls`` path the chat tool loop uses
   (ADR-037/058). One new transport/dispatcher over the same record,
   never a second recorder.
4. **Return.**

**Which dispatcher a ``tools/call`` reaches.** ``name`` starting with
``broker:`` (``services.mcp_broker.parse_namespaced_tool_name``
recognises it) routes to ``mcp_broker.broker_tool_call``; a name
registered in the MCP-only write/curation registry
(``services.mcp_write_bridge.get_write_tool_by_name``) routes to
``mcp_write_bridge.call_write_tool``; every other name routes to the
Phase 1 ``mcp_bridge.call_read_tool`` path, byte-identical to before
Track A/B existed.

**Track A — write/curation tools.** ``write_decision``/``write_skill``
are admitted into ``tools/list`` and dispatchable ONLY for a caller
holding the tool's own ``memory:{decisions,skills}:write`` scope — no
``is_admin`` bypass (see ``services.mcp_write_bridge``'s module
docstring). They live in a registry structurally separate from the
Phase 1 read tools' ``MEMORY_TOOL_REGISTRY``, so neither
``/v1/chat/completions`` nor Phase 1's own manifest/dispatch is touched
by their existence.

**Auth scope of this endpoint.** ``require_user`` gates EVERY JSON-RPC
method on this route, including ``initialize`` and ``tools/list`` —
not just ``tools/call``. The spec text is call-focused, but nothing
about MCP discovery should be anonymous on a system whose whole point
is provable authorization; this is the fail-closed reading of an
otherwise-unspecified point (builder's note, not a spec deviation on
any falsifiable acceptance criterion).

**``tools/list`` merges two sources (Phase 2 Track B).** The Phase 1
read/recall tools (unchanged, unnamespaced) plus every ENABLED
operator-configured downstream server's tools, discovered live and
namespaced ``broker:<server>:<tool>`` — one audited surface, per the
spec's "a client sees one audited surface". A downstream server that is
unreachable at manifest-build time is skipped for that call (logged),
never fails the whole ``tools/list`` response.

**Transport notes.** This is a stateless, non-streaming implementation
of streamable-HTTP: every JSON-RPC request gets exactly one
``application/json`` response body. The MCP spec explicitly permits a
single JSON response instead of an SSE upgrade when the server has no
server-initiated messages to send, which neither Phase's synchronous
tool calls ever need. No session id / SSE upgrade is implemented — a
future phase can add it without changing this contract. Notifications
(JSON-RPC requests with no ``id``, e.g. ``notifications/initialized``)
get HTTP 202 with an empty body, per JSON-RPC 2.0 semantics (no
response to a notification).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import mcp.types as mcp_types
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from audittrace.auth import require_user
from audittrace.identity import UserContext
from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.routes.chat import (
    _current_trace_id_hex,
    _flush_pending_tool_calls,
    _persist_interaction,
    _resolve_project,
    _resolve_session_id,
)
from audittrace.services.mcp_bridge import call_read_tool, list_read_tools, to_mcp_tool
from audittrace.services.mcp_broker import (
    broker_tool_call,
    list_downstream_tools,
    parse_namespaced_tool_name,
)
from audittrace.services.mcp_write_bridge import (
    call_write_tool,
    get_write_tool_by_name,
    list_write_tools,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_JSONRPC_VERSION = "2.0"
_SERVER_NAME = "audittrace-mcp"
_MCP_SOURCE = "mcp"


def _server_version() -> str:
    """Package version for MCP ``serverInfo``.

    Deliberately duplicated (not imported) from
    ``server._resolve_version`` — ``server.py`` imports this router
    module at startup, before its own ``_resolve_version`` name is
    bound, so importing it here would be a circular import.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("audittrace-ai")
    except (PackageNotFoundError, ImportError):  # pragma: no cover - dev path
        return "unknown"


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(request_id: Any) -> JSONResponse:
    result = mcp_types.InitializeResult(
        protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
        capabilities=mcp_types.ServerCapabilities(
            tools=mcp_types.ToolsCapability(listChanged=False)
        ),
        serverInfo=mcp_types.Implementation(
            name=_SERVER_NAME, version=_server_version()
        ),
        instructions=(
            "AuditTrace-AI MCP entry-interface, Phase 1 (ADR-063). Exposes "
            "the read/recall memory tools only. Every tools/call is "
            "authorized per-tool against the caller's Keycloak scopes and "
            "recorded as a tamper-evident audit event."
        ),
    )
    return JSONResponse(
        _jsonrpc_result(request_id, result.model_dump(exclude_none=True, by_alias=True))
    )


async def _handle_tools_list(
    request_id: Any, user_context: UserContext
) -> JSONResponse:
    tools = [to_mcp_tool(t) for t in list_read_tools(user_context)]
    tools.extend(to_mcp_tool(t) for t in list_write_tools(user_context))
    tools.extend(await list_downstream_tools(user_context))
    result = mcp_types.ListToolsResult(tools=tools)
    return JSONResponse(
        _jsonrpc_result(request_id, result.model_dump(exclude_none=True, by_alias=True))
    )


async def _dispatch_own_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    user_context: UserContext,
    session_id: str,
) -> tuple[
    mcp_types.CallToolResult, list[PendingToolCall], str, str | None, str | None
]:
    """Phase 1 own-tool dispatch — byte-identical to pre-Track-B behaviour."""
    outcome = await call_read_tool(
        user_context=user_context,
        session_id=session_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    return (
        outcome.result,
        [outcome.pending],
        outcome.status,
        outcome.failure_class,
        outcome.error_detail,
    )


async def _dispatch_write_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    user_context: UserContext,
) -> tuple[
    mcp_types.CallToolResult, list[PendingToolCall], str, str | None, str | None
]:
    """Phase 2 Track A write/curation dispatch — exactly ONE
    ``PendingToolCall`` record per call (deny, error, or success), same
    "one call, one row" invariant as the Phase 1 own-tool path."""
    outcome = await call_write_tool(
        user_context=user_context, tool_name=tool_name, arguments=arguments
    )
    return (
        outcome.result,
        [outcome.pending],
        outcome.status,
        outcome.failure_class,
        outcome.error_detail,
    )


async def _dispatch_broker_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    user_context: UserContext,
) -> tuple[
    mcp_types.CallToolResult, list[PendingToolCall], str, str | None, str | None
]:
    """Phase 2 Track B brokered dispatch — up to TWO ``PendingToolCall``
    records (request + result); a denial produces exactly one."""
    outcome = await broker_tool_call(
        user_context=user_context,
        tool_name=tool_name,
        arguments=arguments,
    )
    pending = [outcome.request_pending]
    if outcome.result_pending is not None:
        pending.append(outcome.result_pending)
    return (
        outcome.result,
        pending,
        outcome.status,
        outcome.failure_class,
        outcome.error_detail,
    )


async def _handle_tools_call(
    *,
    request: Request,
    body: dict[str, Any],
    request_id: Any,
    params: dict[str, Any],
    user_context: UserContext,
) -> JSONResponse:
    tool_name = params.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        return JSONResponse(
            _jsonrpc_error(request_id, -32602, "Invalid params: 'name' is required")
        )
    raw_arguments = params.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    started_perf = time.perf_counter()
    project = _resolve_project(request, body)
    session_id = _resolve_session_id(
        request, _MCP_SOURCE, tool_name, user_context.user_id
    )

    # Dispatch routing (ADR-063 Phase 2 Track A + Track B): a
    # ``broker:<server>:<tool>`` name goes to the broker; a name
    # registered in the MCP-only write/curation registry goes to Track
    # A's write dispatch; every other name keeps the exact Phase 1
    # own-tool path.
    if parse_namespaced_tool_name(tool_name) is not None:
        (
            result,
            pending,
            status,
            failure_class,
            error_detail,
        ) = await _dispatch_broker_tool(
            tool_name=tool_name, arguments=arguments, user_context=user_context
        )
    elif get_write_tool_by_name(tool_name) is not None:
        (
            result,
            pending,
            status,
            failure_class,
            error_detail,
        ) = await _dispatch_write_tool(
            tool_name=tool_name, arguments=arguments, user_context=user_context
        )
    else:
        result, pending, status, failure_class, error_detail = await _dispatch_own_tool(
            tool_name=tool_name,
            arguments=arguments,
            user_context=user_context,
            session_id=session_id,
        )

    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    trace_id = _current_trace_id_hex()

    # One InteractionRecord (one trace) per call regardless of how many
    # ToolCall rows the dispatcher produced — the broker's request+result
    # pair shares this single parent, matching the spec's "one trace
    # spans caller→gateway→downstream→result".
    answer_source = pending[-1].result_summary or ""
    interaction_id = await _persist_interaction(
        project=project,
        source=_MCP_SOURCE,
        question=json.dumps({"tool": tool_name, "arguments": arguments})[:2000],
        answer=answer_source[:2000],
        prompt_tokens=0,
        completion_tokens=0,
        session_id=session_id,
        model=None,
        user_id=user_context.user_id,
        status=status,
        failure_class=failure_class,
        error_detail=error_detail,
        duration_ms=duration_ms,
        trace_id=trace_id,
    )
    await _flush_pending_tool_calls(pending, interaction_id)

    return JSONResponse(
        _jsonrpc_result(request_id, result.model_dump(exclude_none=True, by_alias=True))
    )


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    user_context: UserContext = Depends(require_user),
) -> Response:
    """Single streamable-HTTP entry point for the MCP JSON-RPC methods
    Phase 1 supports: ``initialize``, ``notifications/initialized``,
    ``tools/list``, ``tools/call``, ``ping``.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error"), status_code=400
        )

    if not isinstance(body, dict):
        return JSONResponse(
            _jsonrpc_error(None, -32600, "Invalid Request"), status_code=400
        )

    method = body.get("method")
    request_id = body.get("id")
    raw_params = body.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    is_notification = "id" not in body

    if not isinstance(method, str) or not method:
        return JSONResponse(
            _jsonrpc_error(request_id, -32600, "Invalid Request"), status_code=400
        )

    # Notifications (no "id") never get a JSON-RPC response body — this
    # covers "notifications/initialized" and any future notification
    # method uniformly, per JSON-RPC 2.0 §4.1.
    if is_notification:
        return Response(status_code=202)

    if method == "initialize":
        return _handle_initialize(request_id)

    if method == "tools/list":
        return await _handle_tools_list(request_id, user_context)

    if method == "tools/call":
        return await _handle_tools_call(
            request=request,
            body=body,
            request_id=request_id,
            params=params,
            user_context=user_context,
        )

    if method == "ping":
        return JSONResponse(_jsonrpc_result(request_id, {}))

    return JSONResponse(
        _jsonrpc_error(request_id, -32601, f"Method not found: {method}")
    )
