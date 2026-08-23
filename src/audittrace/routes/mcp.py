"""MCP entry-interface — ADR-063 Phase 1 (read/recall tools over MCP).

**Additive.** Mounts a standard MCP transport (streamable-HTTP,
JSON-RPC 2.0 over a single ``POST /mcp``) on a NEW path. Nothing about
``/v1/chat/completions`` changes — this router is registered alongside
it in ``server.py``, never inside it
(``feedback_openai_schema_inviolate``).

**Per-call contract (ADR-063 §Change.3).** Every ``tools/call``:

1. **Authorize.** Identity is resolved by ``require_user`` — the SAME
   dependency ``routes/chat.py`` uses. It validates the Keycloak bearer
   JWT (or returns the bypass sentinel when
   ``AUDITTRACE_AUTH_REQUIRED=false``), and binds the
   ``app.current_user_id`` RLS ContextVar before this handler ever
   runs. The per-tool ``required_scope`` check happens inside
   ``services.mcp_bridge.call_read_tool`` — deny means no execution and
   no data, mirroring the chat tool loop's defensive re-check.
2. **Execute** under the caller's isolated RLS context (bound in step 1).
3. **Record** ONE tamper-evident audit event via the SAME
   ``_persist_interaction`` / ``_flush_pending_tool_calls`` path the
   chat tool loop uses (ADR-037/058) — a new transport over the same
   record, not a second recorder.
4. **Return.**

**Auth scope of this endpoint.** ``require_user`` gates EVERY JSON-RPC
method on this route, including ``initialize`` and ``tools/list`` —
not just ``tools/call``. The spec text is call-focused, but nothing
about MCP discovery should be anonymous on a system whose whole point
is provable authorization; this is the fail-closed reading of an
otherwise-unspecified point (builder's note, not a spec deviation on
any falsifiable acceptance criterion).

**Transport notes.** This is a stateless, non-streaming implementation
of streamable-HTTP: every JSON-RPC request gets exactly one
``application/json`` response body. The MCP spec explicitly permits a
single JSON response instead of an SSE upgrade when the server has no
server-initiated messages to send, which Phase 1's synchronous
read-tool calls never need. No session id / SSE upgrade is implemented
in Phase 1 — a future phase can add it without changing this contract.
Notifications (JSON-RPC requests with no ``id``, e.g.
``notifications/initialized``) get HTTP 202 with an empty body, per
JSON-RPC 2.0 semantics (no response to a notification).
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
from audittrace.routes.chat import (
    _current_trace_id_hex,
    _flush_pending_tool_calls,
    _persist_interaction,
    _resolve_project,
    _resolve_session_id,
)
from audittrace.services.mcp_bridge import call_read_tool, list_read_tools, to_mcp_tool

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


def _handle_tools_list(request_id: Any, user_context: UserContext) -> JSONResponse:
    tools = [to_mcp_tool(t) for t in list_read_tools(user_context)]
    result = mcp_types.ListToolsResult(tools=tools)
    return JSONResponse(
        _jsonrpc_result(request_id, result.model_dump(exclude_none=True, by_alias=True))
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

    outcome = await call_read_tool(
        user_context=user_context,
        session_id=session_id,
        tool_name=tool_name,
        arguments=arguments,
    )

    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    trace_id = _current_trace_id_hex()

    interaction_id = await _persist_interaction(
        project=project,
        source=_MCP_SOURCE,
        question=json.dumps({"tool": tool_name, "arguments": arguments})[:2000],
        answer=(outcome.pending.result_summary or "")[:2000],
        prompt_tokens=0,
        completion_tokens=0,
        session_id=session_id,
        model=None,
        user_id=user_context.user_id,
        status=outcome.status,
        failure_class=outcome.failure_class,
        error_detail=outcome.error_detail,
        duration_ms=duration_ms,
        trace_id=trace_id,
    )
    await _flush_pending_tool_calls([outcome.pending], interaction_id)

    return JSONResponse(
        _jsonrpc_result(
            request_id, outcome.result.model_dump(exclude_none=True, by_alias=True)
        )
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
        return _handle_tools_list(request_id, user_context)

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
