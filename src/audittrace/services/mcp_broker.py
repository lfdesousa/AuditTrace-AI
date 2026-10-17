"""Broker: MCP ``tools/call`` → operator-configured DOWNSTREAM MCP servers,
audited (ADR-063 Phase 2 Track B, ratified 2026-08-23: **broker, not
federated** — see the ADR's "Phase 2 Track B" section).

**The honesty boundary this module exists to keep (ADR-037).** AuditTrace
audits what it actually brokers and can prove against ground truth — a
tool call the agent makes DIRECTLY, not routed through this module, stays
outside the boundary. Everything here is the "price of the proof": every
brokered call is proxied through this process (never a pointer/redirect
to the downstream server) so the two audit events below are records of
something THIS process actually saw happen, not a client's self-report.

**Per-call contract** (mirrors ``services.mcp_bridge.call_read_tool``'s
authorize → execute → record → return shape, extended to two record
points either side of the network hop):

1. **Resolve.** ``broker:<server>:<tool>`` is parsed; an unrecognised or
   unconfigured server denies with no forward attempt (§Out of scope: no
   untrusted auto-discovery — only an operator-configured
   ``Settings.mcp_broker_servers`` entry is ever brokered).
2. **Authorize.** The caller's scopes are checked against the downstream
   server's configured ``required_scope`` *before* anything is sent
   downstream — deny means no forward, no data (falsifiable acceptance:
   "an unauthorized brokered call reaches downstream → RED").
3. **Record(request).** A ``PendingToolCall`` with ``phase="request"`` is
   built — caller, session (via the route layer), trace (via the route
   layer's ``_persist_interaction`` call), tool, args-digest — BEFORE the
   outbound call is issued.
4. **Forward** the JSON-RPC ``tools/call`` to the downstream server's MCP
   endpoint, under the caller's own resolved identity (no client-supplied
   identity ever reaches the downstream request — mirrors
   ``mcp_bridge._strip_identity_args``).
5. **Record(result).** A second ``PendingToolCall`` with ``phase="result"``
   is built — result-digest + status — whether the forward SUCCEEDED,
   FAILED, or TIMED OUT (all three paths reach this record; "no silent
   gaps").
6. **Return** the downstream result (or a denial/failure result) to the
   route layer, which persists ONE ``InteractionRecord`` (one trace) and
   flushes BOTH ``PendingToolCall`` rows via the existing
   ``_flush_pending_tool_calls`` (ADR-037/058) — a new emitter over the
   same tamper-evident record, not a second recorder.

This module never talks to Postgres directly — like ``mcp_bridge``, it
returns ``PendingToolCall`` records for the route layer to persist, so
there is exactly one write path for the audit tables in this codebase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import mcp.types as mcp_types

from audittrace.config import get_settings
from audittrace.identity import UserContext
from audittrace.routes._memory_tool_loop import PendingToolCall

logger = logging.getLogger(__name__)

_NAMESPACE_PREFIX = "broker:"
_JSONRPC_VERSION = "2.0"
_PROVENANCE = "brokered"

# Caller-supplied argument keys stripped before forwarding downstream —
# same guard, same rationale as ``mcp_bridge._strip_identity_args``
# (``feedback_never_trust_caller_metadata_for_security_fields``): the
# token-derived ``UserContext`` is the only identity this module ever
# uses, never anything inside the caller's ``arguments`` dict.
_IDENTITY_ARG_KEYS = frozenset({"user_id", "sub", "userId"})


@dataclass(frozen=True)
class DownstreamServer:
    """One operator-configured downstream MCP server (a
    ``Settings.mcp_broker_servers`` entry, parsed)."""

    name: str
    url: str
    required_scope: str
    timeout_seconds: float = 10.0
    enabled: bool = True


def _strip_identity_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop any caller-supplied identity-shaped keys before forwarding."""
    return {k: v for k, v in arguments.items() if k not in _IDENTITY_ARG_KEYS}


def _digest(payload: Any) -> str:
    """SHA-256 hex digest of a canonicalised JSON payload.

    Deterministic across processes (sorted keys, compact separators),
    mirroring ``audittrace.integrity.content_hash``'s canonicalisation —
    a different digest for a different call, matching hashes for the
    same call regardless of dict key order.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_downstream_registry() -> dict[str, DownstreamServer]:
    """Build the downstream-server registry from operator config.

    Reads fresh from ``get_settings()`` on every call (no module-level
    cache) — the registry is small (operator-configured, not per-request
    hot-path work) and tests must be able to swap ``Settings`` via
    ``monkeypatch``/env without fighting a stale cache. A malformed entry
    (missing ``url`` or ``required_scope``) is skipped with a logged
    warning rather than failing the whole registry — one bad operator
    entry must not take the entire MCP surface down.
    """
    settings = get_settings()
    registry: dict[str, DownstreamServer] = {}
    for name, cfg in settings.mcp_broker_servers.items():
        url = cfg.get("url")
        required_scope = cfg.get("required_scope")
        if not url or not required_scope:
            logger.warning(
                "mcp broker: skipping malformed downstream server config name=%s "
                "(url and required_scope are both mandatory)",
                name,
            )
            continue
        try:
            timeout = float(
                cfg.get("timeout_seconds", settings.mcp_broker_default_timeout_seconds)
            )
        except (TypeError, ValueError):
            timeout = settings.mcp_broker_default_timeout_seconds
        enabled = str(cfg.get("enabled", "true")).strip().lower() != "false"
        registry[name] = DownstreamServer(
            name=name,
            url=url,
            required_scope=required_scope,
            timeout_seconds=timeout,
            enabled=enabled,
        )
    return registry


def namespaced_tool_name(server: str, tool: str) -> str:
    """Render the manifest-facing, namespaced tool name for a downstream
    tool (spec: ``broker:<server>:<tool>``)."""
    return f"{_NAMESPACE_PREFIX}{server}:{tool}"


def parse_namespaced_tool_name(name: str) -> tuple[str, str] | None:
    """Split a ``broker:<server>:<tool>`` name into ``(server, tool)``.

    Returns ``None`` for anything not shaped like a brokered tool name
    (including Phase 1 own-tool names, which never carry the prefix) —
    the route layer uses this to decide which dispatcher owns a call.
    """
    if not name.startswith(_NAMESPACE_PREFIX):
        return None
    rest = name[len(_NAMESPACE_PREFIX) :]
    server, sep, tool = rest.partition(":")
    if not sep or not server or not tool:
        return None
    return server, tool


# One shared async client for the life of the process (PYTHON-ENGINEERING
# §2 — pooled connections, not one per call). Lazily created so import
# stays side-effect-free; injectable ``client`` param on every function
# below lets tests supply an ``httpx.MockTransport``-backed stub instead
# of a real downstream server.
_downstream_client: httpx.AsyncClient | None = None


def _downstream_client_singleton() -> httpx.AsyncClient:
    global _downstream_client
    if _downstream_client is None:
        _downstream_client = httpx.AsyncClient()
    return _downstream_client


async def list_downstream_tools(
    user_context: UserContext,
    registry: dict[str, DownstreamServer] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[mcp_types.Tool]:
    """Discover + namespace tools from every ENABLED configured downstream
    server the caller is authorized for, for merging into the
    ``tools/list`` manifest.

    Scope-filtered PER SERVER, mirroring ``mcp_bridge.list_read_tools``'s
    own-tool filtering: a caller without ``server.required_scope`` never
    sees that server's tools in the manifest at all (defense in depth —
    ``broker_tool_call`` re-checks the same scope at dispatch time, so a
    client that already knows a tool name from elsewhere still can't call
    it, but the manifest itself stays least-privilege too). Admins bypass
    the filter, matching every other scope check in this codebase.

    Best-effort PER SERVER beyond that: an unreachable/misbehaving
    downstream server is logged and skipped rather than failing the whole
    manifest — one broken external server must not take Phase 1's
    own-tool listing (or any OTHER configured downstream server) down
    with it.
    """
    reg = registry if registry is not None else get_downstream_registry()
    cl = client or _downstream_client_singleton()
    tools: list[mcp_types.Tool] = []
    for server in reg.values():
        if not server.enabled:
            continue
        if (
            not user_context.is_admin
            and server.required_scope not in user_context.scopes
        ):
            continue
        try:
            resp = await cl.post(
                server.url,
                json={
                    "jsonrpc": _JSONRPC_VERSION,
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
                timeout=server.timeout_seconds,
            )
            resp.raise_for_status()
            body = resp.json()
            raw_tools = (body.get("result") or {}).get("tools", [])
        except Exception as exc:  # noqa: BLE001 - one bad downstream is not fatal
            logger.warning(
                "mcp broker: tools/list failed for downstream=%s: %s", server.name, exc
            )
            continue
        for raw in raw_tools:
            name = raw.get("name") if isinstance(raw, dict) else None
            if not isinstance(name, str) or not name:
                continue
            tools.append(
                mcp_types.Tool(
                    name=namespaced_tool_name(server.name, name),
                    description=raw.get("description"),
                    inputSchema=raw.get("inputSchema") or {"type": "object"},
                )
            )
    return tools


def _text_result(
    payload: dict[str, Any], *, is_error: bool
) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=None if is_error else payload,
        isError=is_error,
    )


@dataclass
class BrokerCallOutcome:
    """Result of one brokered ``tools/call`` dispatch, paired with its
    audit record(s).

    A DENIED call (unknown/unconfigured server, scope denied) never
    forwards, so it produces exactly ONE ``PendingToolCall``
    (``request_pending``; ``result_pending`` is ``None`` — there is no
    result to record, nothing was sent). A call that reaches the forward
    step ALWAYS produces both — success, downstream error, AND
    timeout/exception all populate ``result_pending`` (no silent gaps).
    """

    result: mcp_types.CallToolResult
    request_pending: PendingToolCall
    result_pending: PendingToolCall | None
    status: str  # "success" | "failed" — mirrors InteractionRecord.status
    failure_class: str | None
    error_detail: str | None


def _denied(
    *,
    tool_name: str,
    user_context: UserContext,
    arguments: dict[str, Any],
    started: datetime,
    perf_start: float,
    granted_scope: str,
    downstream_server: str | None,
    downstream_tool: str | None,
    error: str,
    failure_class: str,
    log_reason: str,
) -> BrokerCallOutcome:
    """Shared denial-path builder — unknown-tool-name / unconfigured-server
    / scope-denied branches all build the SAME shape so the three call
    sites can't drift from each other (mirrors ``mcp_bridge._denied``)."""
    logger.info(
        "mcp broker tools/call denied tool=%s user=%s reason=%s",
        tool_name,
        user_context.user_id,
        log_reason,
    )
    return BrokerCallOutcome(
        result=_text_result({"error": error}, is_error=True),
        request_pending=PendingToolCall(
            tool_name=tool_name,
            user_id=user_context.user_id,
            agent_type=user_context.agent_type,
            args=json.dumps(arguments),
            result_summary=None,
            error=error,
            started_at=started,
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
            granted_scope=granted_scope,
            provenance=_PROVENANCE,
            phase="request",
            downstream_server=downstream_server,
            downstream_tool=downstream_tool,
            args_digest=_digest(arguments),
            result_digest=None,
        ),
        result_pending=None,
        status="failed",
        failure_class=failure_class,
        error_detail=error,
    )


async def broker_tool_call(
    *,
    user_context: UserContext,
    tool_name: str,
    arguments: dict[str, Any],
    registry: dict[str, DownstreamServer] | None = None,
    client: httpx.AsyncClient | None = None,
) -> BrokerCallOutcome:
    """Authorize → record(request) → forward → record(result) → return
    (ADR-063 Phase 2 Track B §Decision.2).

    ``tool_name`` MUST be the namespaced ``broker:<server>:<tool>`` form —
    the route layer is responsible for routing to this function only when
    ``mcp_broker.parse_namespaced_tool_name`` recognises the name.
    ``user_context`` MUST already be token-derived (bound by
    ``audittrace.auth.require_user`` before this is ever called) — this
    function never resolves identity itself and never trusts anything
    inside ``arguments`` as identity.
    """
    started = datetime.now()
    perf_start = time.perf_counter()
    arguments = _strip_identity_args(arguments)

    parsed = parse_namespaced_tool_name(tool_name)
    if parsed is None:
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope="",
            downstream_server=None,
            downstream_tool=None,
            error=f"not a brokered tool name: {tool_name!r}",
            failure_class="tool_not_found",
            log_reason="not_namespaced",
        )
    server_name, downstream_tool = parsed

    reg = registry if registry is not None else get_downstream_registry()
    server = reg.get(server_name)
    if server is None or not server.enabled:
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope="",
            downstream_server=server_name,
            downstream_tool=downstream_tool,
            error=f"unknown or disabled downstream server: {server_name!r}",
            failure_class="tool_not_found",
            log_reason="unconfigured_server",
        )

    # Authorize BEFORE any forward attempt (falsifiable acceptance: an
    # unauthorized call must never reach the downstream server).
    if not user_context.is_admin and server.required_scope not in user_context.scopes:
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope=server.required_scope,
            downstream_server=server.name,
            downstream_tool=downstream_tool,
            error=(
                f"scope denied: {server.required_scope} not in caller scopes "
                f"for downstream server {server.name}"
            ),
            failure_class="scope_denied",
            log_reason="scope_denied",
        )

    args_digest = _digest(arguments)
    request_pending = PendingToolCall(
        tool_name=tool_name,
        user_id=user_context.user_id,
        agent_type=user_context.agent_type,
        args=json.dumps(arguments),
        result_summary=None,
        error=None,
        started_at=started,
        duration_ms=None,
        granted_scope=server.required_scope,
        provenance=_PROVENANCE,
        phase="request",
        downstream_server=server.name,
        downstream_tool=downstream_tool,
        args_digest=args_digest,
        result_digest=None,
    )

    cl = client or _downstream_client_singleton()
    try:
        resp = await cl.post(
            server.url,
            json={
                "jsonrpc": _JSONRPC_VERSION,
                "id": 1,
                "method": "tools/call",
                "params": {"name": downstream_tool, "arguments": arguments},
            },
            timeout=server.timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and "error" in body:
            rpc_error = body["error"]
            message = (
                rpc_error.get("message", "downstream error")
                if isinstance(rpc_error, dict)
                else str(rpc_error)
            )
            raise _DownstreamRpcError(message)
        result_payload = body.get("result", {}) if isinstance(body, dict) else {}
    except Exception as exc:  # noqa: BLE001 - every downstream failure mode lands here
        duration_ms = int((time.perf_counter() - perf_start) * 1000)
        error_text = str(exc) or exc.__class__.__name__
        result_pending = PendingToolCall(
            tool_name=tool_name,
            user_id=user_context.user_id,
            agent_type=user_context.agent_type,
            args=json.dumps(arguments),
            result_summary=None,
            error=error_text,
            started_at=started,
            duration_ms=duration_ms,
            granted_scope=server.required_scope,
            provenance=_PROVENANCE,
            phase="result",
            downstream_server=server.name,
            downstream_tool=downstream_tool,
            args_digest=args_digest,
            result_digest=_digest({"error": error_text}),
        )
        logger.info(
            "mcp broker tools/call downstream failure tool=%s user=%s error=%s",
            tool_name,
            user_context.user_id,
            error_text,
        )
        return BrokerCallOutcome(
            result=_text_result({"error": error_text}, is_error=True),
            request_pending=request_pending,
            result_pending=result_pending,
            status="failed",
            failure_class="downstream_error",
            error_detail=error_text,
        )

    duration_ms = int((time.perf_counter() - perf_start) * 1000)
    result_digest = _digest(result_payload)
    summary = json.dumps(result_payload)[:1000]
    result_pending = PendingToolCall(
        tool_name=tool_name,
        user_id=user_context.user_id,
        agent_type=user_context.agent_type,
        args=json.dumps(arguments),
        result_summary=summary,
        error=None,
        started_at=started,
        duration_ms=duration_ms,
        granted_scope=server.required_scope,
        provenance=_PROVENANCE,
        phase="result",
        downstream_server=server.name,
        downstream_tool=downstream_tool,
        args_digest=args_digest,
        result_digest=result_digest,
    )
    logger.info(
        "mcp broker tools/call ok tool=%s user=%s duration_ms=%d",
        tool_name,
        user_context.user_id,
        duration_ms,
    )
    return BrokerCallOutcome(
        result=_result_from_downstream_payload(result_payload),
        request_pending=request_pending,
        result_pending=result_pending,
        status="success",
        failure_class=None,
        error_detail=None,
    )


class _DownstreamRpcError(RuntimeError):
    """The downstream MCP server returned a JSON-RPC ``error`` object."""


def _result_from_downstream_payload(
    result_payload: dict[str, Any],
) -> mcp_types.CallToolResult:
    """Render the downstream server's ``tools/call`` result as our own
    ``CallToolResult``.

    The downstream result is ALREADY MCP-shaped (it came from a real MCP
    server's ``tools/call`` response), so this re-validates it against
    our own ``CallToolResult`` model rather than re-wrapping it as opaque
    text — a well-formed downstream response round-trips faithfully
    (content blocks, structuredContent, isError all preserved). A
    malformed/unexpected shape degrades to a text-wrapped result instead
    of raising, so a downstream server's schema quirk surfaces as data,
    not a 500.
    """
    try:
        return mcp_types.CallToolResult.model_validate(result_payload)
    except Exception:  # noqa: BLE001 - fall back to opaque text, never crash
        return _text_result(result_payload, is_error=False)
