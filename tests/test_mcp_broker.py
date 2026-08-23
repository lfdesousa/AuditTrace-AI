"""Tests for ``audittrace.services.mcp_broker`` (ADR-063 Phase 2 Track B).

Unit-level, mirroring ``test_mcp_bridge.py``'s style: exercises the
broker's own composition logic (registry parsing, namespacing, the
authorize→record→forward→record→return sequence) against a STUB
downstream MCP server (``httpx.MockTransport`` — no real network, no
real downstream process), the same technique
``tests/test_session_summarizer.py`` uses for its stub LLM.

Falsifiable-by-construction, per spec Rule 1:

- ``TestBrokerCallSuccess.test_success_writes_two_phase_tagged_rows``:
  neuter the request-phase record (drop ``request_pending`` from
  ``broker_tool_call``) → RED (only one pending row, no ``phase="request"``
  row).
- ``TestBrokerCallDenied.test_scope_denied_never_forwards``: neuter the
  scope check → the spy handler's call-counter would flip to 1 → RED.
- ``TestBrokerCallDownstreamFailure`` (both the JSON-RPC-error and the
  transport-exception cases): neuter either except-branch's
  ``result_pending`` construction → RED (``result_pending`` would be
  ``None``, failing the "still writes a recorded event" assertion).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from audittrace.identity import sentinel_user_context
from audittrace.services import mcp_broker
from audittrace.services.mcp_broker import (
    DownstreamServer,
    _digest,
    broker_tool_call,
    get_downstream_registry,
    list_downstream_tools,
    namespaced_tool_name,
    parse_namespaced_tool_name,
)

_WEATHER = DownstreamServer(
    name="weather",
    url="http://weather-mcp.test/mcp",
    required_scope="audittrace:broker:weather",
)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _admin_for(scopes: tuple[str, ...] = ()) -> Any:
    return replace(sentinel_user_context(), scopes=scopes, is_admin=False)


# ────────────────────────── namespacing helpers ──────────────────────────


class TestNamespacing:
    def test_namespaced_tool_name_shape(self) -> None:
        assert namespaced_tool_name("weather", "forecast") == "broker:weather:forecast"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("broker:weather:forecast", ("weather", "forecast")),
            ("broker:weather:sub:tool", ("weather", "sub:tool")),
            ("recall_decisions", None),
            ("broker:", None),
            ("broker:weather", None),
            ("broker::forecast", None),
            ("broker:weather:", None),
            ("", None),
        ],
    )
    def test_parse_namespaced_tool_name(
        self, name: str, expected: tuple[str, str] | None
    ) -> None:
        assert parse_namespaced_tool_name(name) == expected


class TestDigest:
    def test_same_content_different_key_order_same_digest(self) -> None:
        a = _digest({"x": 1, "y": 2})
        b = _digest({"y": 2, "x": 1})
        assert a == b

    def test_different_content_different_digest(self) -> None:
        assert _digest({"x": 1}) != _digest({"x": 2})


# ─────────────────────────── registry parsing ────────────────────────────


class TestGetDownstreamRegistry:
    def test_valid_entry_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audittrace import config as config_mod

        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps(
                {
                    "weather": {
                        "url": "http://weather-mcp:9000/mcp",
                        "required_scope": "audittrace:broker:weather",
                        "timeout_seconds": "5",
                    }
                }
            ),
        )
        config_mod.get_settings.cache_clear()
        try:
            registry = get_downstream_registry()
            assert registry["weather"] == DownstreamServer(
                name="weather",
                url="http://weather-mcp:9000/mcp",
                required_scope="audittrace:broker:weather",
                timeout_seconds=5.0,
                enabled=True,
            )
        finally:
            config_mod.get_settings.cache_clear()

    def test_missing_url_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audittrace import config as config_mod

        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps({"broken": {"required_scope": "x"}}),
        )
        config_mod.get_settings.cache_clear()
        try:
            assert get_downstream_registry() == {}
        finally:
            config_mod.get_settings.cache_clear()

    def test_missing_required_scope_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace import config as config_mod

        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps({"broken": {"url": "http://x/mcp"}}),
        )
        config_mod.get_settings.cache_clear()
        try:
            assert get_downstream_registry() == {}
        finally:
            config_mod.get_settings.cache_clear()

    def test_invalid_timeout_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace import config as config_mod

        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps(
                {
                    "weather": {
                        "url": "http://x/mcp",
                        "required_scope": "s",
                        "timeout_seconds": "not-a-number",
                    }
                }
            ),
        )
        config_mod.get_settings.cache_clear()
        try:
            registry = get_downstream_registry()
            assert registry["weather"].timeout_seconds == 10.0
        finally:
            config_mod.get_settings.cache_clear()

    def test_enabled_false_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audittrace import config as config_mod

        monkeypatch.setenv(
            "AUDITTRACE_MCP_BROKER_SERVERS",
            json.dumps(
                {
                    "weather": {
                        "url": "http://x/mcp",
                        "required_scope": "s",
                        "enabled": "false",
                    }
                }
            ),
        )
        config_mod.get_settings.cache_clear()
        try:
            assert get_downstream_registry()["weather"].enabled is False
        finally:
            config_mod.get_settings.cache_clear()

    def test_empty_config_returns_empty_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace import config as config_mod

        monkeypatch.delenv("AUDITTRACE_MCP_BROKER_SERVERS", raising=False)
        config_mod.get_settings.cache_clear()
        try:
            assert get_downstream_registry() == {}
        finally:
            config_mod.get_settings.cache_clear()


# ─────────────────────────── list_downstream_tools ───────────────────────


class TestListDownstreamTools:
    @pytest.mark.asyncio
    async def test_namespaces_discovered_tools(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {
                                "name": "forecast",
                                "description": "Get a forecast",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )

        admin = sentinel_user_context()
        tools = await list_downstream_tools(
            admin, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert [t.name for t in tools] == ["broker:weather:forecast"]
        assert tools[0].description == "Get a forecast"

    @pytest.mark.asyncio
    async def test_disabled_server_excluded(self) -> None:
        disabled = replace(_WEATHER, enabled=False)
        called = {"count": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={"result": {"tools": []}})

        admin = sentinel_user_context()
        tools = await list_downstream_tools(
            admin, {"weather": disabled}, client=_mock_client(_handler)
        )
        assert tools == []
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_caller_without_scope_sees_no_tools_from_that_server(self) -> None:
        """Manifest-level least-privilege: mirrors ``mcp_bridge.list_read_tools``
        filtering own tools by scope. Neuter (drop the scope check inside
        ``list_downstream_tools``) → RED because a scopeless caller would
        see ``broker:weather:forecast`` in the manifest."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"tools": [{"name": "forecast"}]}}
            )

        caller = _admin_for(scopes=())
        tools = await list_downstream_tools(
            caller, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert tools == []

        with_scope = _admin_for(scopes=("audittrace:broker:weather",))
        tools2 = await list_downstream_tools(
            with_scope, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert [t.name for t in tools2] == ["broker:weather:forecast"]

    @pytest.mark.asyncio
    async def test_admin_bypasses_manifest_scope_filter(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"tools": [{"name": "forecast"}]}}
            )

        admin = replace(sentinel_user_context(), scopes=(), is_admin=True)
        tools = await list_downstream_tools(
            admin, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert [t.name for t in tools] == ["broker:weather:forecast"]

    @pytest.mark.asyncio
    async def test_unreachable_server_skipped_not_fatal(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        admin = sentinel_user_context()
        tools = await list_downstream_tools(
            admin, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert tools == []

    @pytest.mark.asyncio
    async def test_malformed_tool_entries_skipped(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "tools": [
                            {"description": "no name"},
                            "not-even-a-dict",
                            {"name": "ok-tool"},
                        ]
                    }
                },
            )

        admin = sentinel_user_context()
        tools = await list_downstream_tools(
            admin, {"weather": _WEATHER}, client=_mock_client(_handler)
        )
        assert [t.name for t in tools] == ["broker:weather:ok-tool"]


# ───────────────────────────── broker_tool_call ──────────────────────────


class TestBrokerCallDenied:
    @pytest.mark.asyncio
    async def test_non_namespaced_name_denied(self) -> None:
        admin = sentinel_user_context()
        outcome = await broker_tool_call(
            user_context=admin, tool_name="not_namespaced", arguments={}
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_not_found"
        assert outcome.result_pending is None
        assert outcome.result.isError is True

    @pytest.mark.asyncio
    async def test_unconfigured_server_denied(self) -> None:
        admin = sentinel_user_context()
        outcome = await broker_tool_call(
            user_context=admin,
            tool_name="broker:ghost:forecast",
            arguments={},
            registry={},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_not_found"
        assert outcome.result_pending is None
        assert outcome.request_pending.downstream_server == "ghost"

    @pytest.mark.asyncio
    async def test_disabled_server_denied(self) -> None:
        admin = sentinel_user_context()
        outcome = await broker_tool_call(
            user_context=admin,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": replace(_WEATHER, enabled=False)},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_not_found"

    @pytest.mark.asyncio
    async def test_scope_denied_never_forwards(self) -> None:
        """Falsifiable acceptance: 'an unauthorized brokered call reaches
        downstream' must stay RED. Spies on the transport so the
        assertion is non-vacuous — neuter the scope check inside
        ``broker_tool_call`` and the handler's call-counter flips to 1."""
        called = {"count": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={"result": {"content": []}})

        caller = _admin_for(scopes=())
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={"city": "Zurich"},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert called["count"] == 0, "downstream must never be reached on scope denial"
        assert outcome.status == "failed"
        assert outcome.failure_class == "scope_denied"
        assert outcome.result_pending is None
        assert outcome.request_pending.downstream_server == "weather"
        assert outcome.request_pending.granted_scope == "audittrace:broker:weather"

    @pytest.mark.asyncio
    async def test_admin_bypasses_scope_check(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"content": []}})

        admin = replace(sentinel_user_context(), scopes=(), is_admin=True)
        outcome = await broker_tool_call(
            user_context=admin,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "success"


class TestBrokerCallSuccess:
    @pytest.mark.asyncio
    async def test_success_writes_two_phase_tagged_rows(self) -> None:
        """Falsifiable acceptance: a brokered call writes TWO tamper-evident
        events (request + result) tagged brokered. Neuter (drop building
        ``request_pending`` before the forward, or drop ``result_pending``
        after) → RED because one of the two assertions below fails."""
        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "sunny"}],
                        "structuredContent": {"forecast": "sunny"},
                        "isError": False,
                    },
                },
            )

        caller = _admin_for(scopes=("audittrace:broker:weather",))
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={"city": "Zurich"},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )

        assert outcome.status == "success"
        assert outcome.result.isError is False
        assert outcome.result.structuredContent == {"forecast": "sunny"}

        req, res = outcome.request_pending, outcome.result_pending
        assert res is not None
        assert req.provenance == "brokered"
        assert res.provenance == "brokered"
        assert req.phase == "request"
        assert res.phase == "result"
        assert req.downstream_server == "weather"
        assert res.downstream_server == "weather"
        assert req.downstream_tool == "forecast"
        assert req.args_digest == res.args_digest
        assert req.result_digest is None
        assert res.result_digest is not None
        assert res.error is None
        assert res.result_summary is not None

        # forwarded under the downstream tool name, not the namespaced one
        assert captured["body"]["params"]["name"] == "forecast"
        assert captured["body"]["params"]["arguments"] == {"city": "Zurich"}

    @pytest.mark.asyncio
    async def test_caller_supplied_identity_stripped_before_forward(self) -> None:
        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"content": []}})

        admin = sentinel_user_context()
        await broker_tool_call(
            user_context=admin,
            tool_name="broker:weather:forecast",
            arguments={"city": "Zurich", "user_id": "attacker-controlled"},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert "user_id" not in captured["body"]["params"]["arguments"]

    @pytest.mark.asyncio
    async def test_malformed_downstream_result_falls_back_to_text(self) -> None:
        """Downstream returns a result payload that isn't a valid
        ``CallToolResult`` (no ``content`` key) — the broker must not
        crash; it degrades to a text-wrapped result."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"just": "some data"}})

        admin = sentinel_user_context()
        outcome = await broker_tool_call(
            user_context=admin,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "success"
        assert outcome.result.isError is False
        assert outcome.result.structuredContent == {"just": "some data"}


class TestBrokerCallDownstreamFailure:
    @pytest.mark.asyncio
    async def test_transport_exception_still_writes_result_event(self) -> None:
        """Falsifiable acceptance: 'a downstream failure/timeout still
        writes a recorded event'. Neuter the except-branch's
        ``result_pending`` construction → RED (``result_pending`` None)."""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        caller = _admin_for(scopes=("audittrace:broker:weather",))
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={"city": "Zurich"},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "downstream_error"
        assert outcome.result.isError is True
        assert outcome.request_pending.phase == "request"
        res = outcome.result_pending
        assert res is not None
        assert res.phase == "result"
        assert res.provenance == "brokered"
        assert res.error is not None
        assert res.result_digest is not None

    @pytest.mark.asyncio
    async def test_http_error_status_still_writes_result_event(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        caller = _admin_for(scopes=("audittrace:broker:weather",))
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "downstream_error"
        assert outcome.result_pending is not None

    @pytest.mark.asyncio
    async def test_jsonrpc_error_object_still_writes_result_event(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "tool exploded"},
                },
            )

        caller = _admin_for(scopes=("audittrace:broker:weather",))
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "downstream_error"
        assert outcome.error_detail == "tool exploded"
        assert outcome.result_pending is not None
        assert outcome.result_pending.error == "tool exploded"

    @pytest.mark.asyncio
    async def test_jsonrpc_error_non_dict_still_handled(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": "opaque failure"}
            )

        caller = _admin_for(scopes=("audittrace:broker:weather",))
        outcome = await broker_tool_call(
            user_context=caller,
            tool_name="broker:weather:forecast",
            arguments={},
            registry={"weather": _WEATHER},
            client=_mock_client(_handler),
        )
        assert outcome.status == "failed"
        assert outcome.error_detail == "opaque failure"


class TestDownstreamClientSingleton:
    @pytest.mark.asyncio
    async def test_singleton_is_reused_across_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PYTHON-ENGINEERING §2: one pooled client for the process, not
        one per call."""
        monkeypatch.setattr(mcp_broker, "_downstream_client", None)
        first = mcp_broker._downstream_client_singleton()
        second = mcp_broker._downstream_client_singleton()
        try:
            assert first is second
        finally:
            await first.aclose()
            monkeypatch.setattr(mcp_broker, "_downstream_client", None)
