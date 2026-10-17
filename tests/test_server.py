"""Unit tests for audittrace.server module-level helpers.

Integration coverage of the FastAPI app assembly + lifespan lives in the
route tests (``test_routes.py``, ``test_chat_proxy.py``). These tests
cover the small pieces of ``server.py`` that don't exercise naturally
through request traffic — primarily the urllib3 OTel request hook that
back-fills ``server.address`` per ADR-029.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from audittrace.server import (
    _bootstrap_scan_pipeline,
    _build_httpx_peer_service_map,
    _connect_scan_amqp_or_degrade,
    make_httpx_async_peer_service_hook,
    make_httpx_peer_service_hook,
    urllib3_set_server_address,
)


def _mk_span(is_recording: bool = True) -> MagicMock:
    """Build a minimal fake of opentelemetry.trace.Span for assertion."""
    span = MagicMock()
    span.is_recording.return_value = is_recording
    return span


def _mk_request(url: str) -> SimpleNamespace:
    """Build a minimal fake of the urllib3 instrumentor's RequestInfo."""
    return SimpleNamespace(url=url)


class TestUrllib3SetServerAddress:
    """ADR-029: hook back-fills server.address / server.port that the
    upstream urllib3 instrumentor fails to emit."""

    def test_sets_server_address_from_hostname(self):
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/bucket"))
        span.set_attribute.assert_any_call("server.address", "minio")

    def test_sets_server_port_when_present(self):
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/bucket"))
        span.set_attribute.assert_any_call("server.port", 9000)

    def test_omits_port_when_url_has_no_port(self):
        """URLs without an explicit port (http://host/path) should only
        set server.address — no bogus default port attribute."""
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("http://example/path"))
        # server.address is set
        span.set_attribute.assert_any_call("server.address", "example")
        # server.port is NOT in any of the calls
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "server.port" not in keys

    def test_no_op_when_span_is_none(self):
        """Passing None as span must not raise — the instrumentor's
        contract allows a None span (e.g. when the sampler drops)."""
        urllib3_set_server_address(None, None, _mk_request("http://minio:9000/x"))

    def test_no_op_when_span_not_recording(self):
        """Non-recording spans are a hot-path optimisation: skip work."""
        span = _mk_span(is_recording=False)
        urllib3_set_server_address(span, None, _mk_request("http://minio:9000/x"))
        span.set_attribute.assert_not_called()

    def test_no_attributes_when_url_has_no_hostname(self):
        """A malformed URL with no hostname still must not raise, just
        no-op. urlparse('///path') parses cleanly but hostname is None."""
        span = _mk_span()
        urllib3_set_server_address(span, None, _mk_request("///nohost"))
        span.set_attribute.assert_not_called()

    def test_https_url_with_port(self):
        """HTTPS URLs round-trip cleanly too (symmetric with http://)."""
        span = _mk_span()
        urllib3_set_server_address(
            span, None, _mk_request("https://api.example.com:8443/v1/x")
        )
        span.set_attribute.assert_any_call("server.address", "api.example.com")
        span.set_attribute.assert_any_call("server.port", 8443)


class TestBuildHttpxPeerServiceMap:
    """The per-port label map is built from Settings at lifespan start,
    so changing an LLM endpoint URL propagates to the service-graph
    label automatically."""

    def test_maps_all_three_llm_endpoints(self):
        settings = SimpleNamespace(
            llama_url="http://host.docker.internal:11435/v1",
            embed_url="http://host.docker.internal:11436/v1",
            summarizer_url="http://host.docker.internal:11437/v1",
        )
        got = _build_httpx_peer_service_map(settings)
        assert got == {
            11435: "qwen-chat-llm",
            11436: "nomic-embed-server",
            11437: "mistral-summariser-llm",
        }

    def test_omits_entry_when_port_is_missing(self):
        """A URL without a port (http://host/path) contributes nothing
        to the map; the edge falls back to server.address."""
        settings = SimpleNamespace(
            llama_url="http://llama/v1",
            embed_url="http://host:11436/v1",
            summarizer_url=None,
        )
        got = _build_httpx_peer_service_map(settings)
        assert got == {11436: "nomic-embed-server"}

    def test_empty_settings_produces_empty_map(self):
        settings = SimpleNamespace(llama_url=None, embed_url=None, summarizer_url=None)
        assert _build_httpx_peer_service_map(settings) == {}


class TestHttpxPeerServiceHook:
    """The hook sets peer.service from the URL port via a closed-over map.
    Tempo's service-graph processor reads peer.service ahead of
    server.address, so this produces one semantic edge per endpoint."""

    def _hook_with_defaults(self):
        mapping = {
            11435: "qwen-chat-llm",
            11437: "mistral-summariser-llm",
        }
        return make_httpx_peer_service_hook(mapping)

    def test_sets_peer_service_for_known_port(self):
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host.docker.internal:11437/v1/chat/completions"))
        span.set_attribute.assert_any_call("peer.service", "mistral-summariser-llm")

    def test_does_not_set_for_unknown_port(self):
        """Ports not in the map fall back to server.address — the hook
        must not emit a misleading peer.service for chromadb etc."""
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://chromadb:8000/api/v2/foo"))
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "peer.service" not in keys

    def test_no_op_on_none_span(self):
        hook = self._hook_with_defaults()
        # Must not raise.
        hook(None, _mk_request("http://host.docker.internal:11435/v1/x"))

    def test_no_op_on_non_recording_span(self):
        span = _mk_span(is_recording=False)
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host.docker.internal:11435/v1/x"))
        span.set_attribute.assert_not_called()

    def test_no_op_when_url_has_no_port(self):
        span = _mk_span()
        hook = self._hook_with_defaults()
        hook(span, _mk_request("http://host/path"))
        keys = [c.args[0] for c in span.set_attribute.call_args_list]
        assert "peer.service" not in keys


class TestHttpxAsyncPeerServiceHook:
    """AsyncClient outbound calls need an ``async_request_hook`` that
    passes ``iscoroutinefunction()`` in the httpx instrumentor. A sync
    hook passed there is silently dropped — the failure mode that caused
    the Mistral summariser's spans to land without ``peer.service`` on
    the first wiring pass (2026-04-15)."""

    import pytest as _pytest

    @_pytest.mark.asyncio
    async def test_async_hook_is_coroutine_function(self):
        """The instrumentor gates async hooks on
        ``iscoroutinefunction`` — regression guard for the silent-drop."""
        import inspect

        hook = make_httpx_async_peer_service_hook({11437: "mistral"})
        assert inspect.iscoroutinefunction(hook)

    @_pytest.mark.asyncio
    async def test_async_hook_sets_peer_service(self):
        hook = make_httpx_async_peer_service_hook({11437: "mistral-summariser-llm"})
        span = _mk_span()
        await hook(
            span,
            _mk_request("http://host.docker.internal:11437/v1/chat/completions"),
        )
        span.set_attribute.assert_any_call("peer.service", "mistral-summariser-llm")

    @_pytest.mark.asyncio
    async def test_async_hook_no_op_on_none_span(self):
        hook = make_httpx_async_peer_service_hook({11437: "mistral"})
        await hook(None, _mk_request("http://host.docker.internal:11437/v1/x"))


class TestCorsOriginsToggle:
    """``AUDITTRACE_CORS_ORIGINS=[]`` must disable CORS outright.

    Behind a same-origin BFF (the production shape) no browser ever needs a
    CORS response, and installing the middleware anyway means the service
    answers preflights and echoes ``Access-Control-Allow-Credentials`` for
    traffic it should simply not be brokering. The empty list is the
    production-safe default, so it must actually remove the middleware
    rather than install a permissive-but-empty one.
    """

    def _preflight(self, app, origin: str):
        from fastapi.testclient import TestClient

        # No context manager: we exercise the CORS layer only, which sits
        # ahead of routing, so the lifespan (telemetry, DB) need not run.
        return TestClient(app).options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

    def test_configured_origin_gets_cors_headers(self, monkeypatch, test_container):
        from audittrace import dependencies
        from audittrace.config import Settings
        from audittrace.server import create_app

        dependencies.container = test_container
        monkeypatch.setattr(
            "audittrace.server.get_settings",
            lambda: Settings(cors_origins=["http://localhost:3000"]),
        )
        app = create_app()

        resp = self._preflight(app, "http://localhost:3000")

        assert resp.headers.get("access-control-allow-origin") == (
            "http://localhost:3000"
        )

    def test_empty_origins_emits_no_cors_headers(self, monkeypatch, test_container):
        from audittrace import dependencies
        from audittrace.config import Settings
        from audittrace.server import create_app

        dependencies.container = test_container
        monkeypatch.setattr(
            "audittrace.server.get_settings",
            lambda: Settings(cors_origins=[]),
        )
        app = create_app()

        resp = self._preflight(app, "http://localhost:3000")

        # Nothing is granted: no origin echo, no credentials grant.
        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-credentials" not in resp.headers


class TestPerLayerOrScopesOpenAPI:
    """``_wire_per_layer_or_scopes`` rewrites the OpenAPI ``security`` block
    for the routes that accept any-of-several scopes.

    The rewrite runs against whatever schema FastAPI generated, so it has to
    survive app shapes that differ from the fully-wired production one —
    auth-disabled builds and builds where the memory router is not mounted.
    Emitting a ``security`` entry naming a scheme that isn't declared, or
    inventing a path that isn't served, produces an invalid spec: the
    vendored ``docs/reference/audittrace/openapi.yaml`` is generated from
    this output and client generators consume it directly.
    """

    @staticmethod
    def _app_with_oauth2(include_upload: bool):
        from fastapi import Depends, FastAPI
        from fastapi.security import OAuth2PasswordBearer

        oauth2 = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)
        app = FastAPI()
        if include_upload:

            @app.post("/memory/upload")
            async def upload(_t: str = Depends(oauth2)) -> dict[str, str]:
                return {"ok": "yes"}

        @app.get("/health")
        async def health(_t: str = Depends(oauth2)) -> dict[str, str]:
            return {"ok": "yes"}

        return app

    def test_schema_is_built_once_and_cached(self):
        from audittrace.server import _wire_per_layer_or_scopes

        app = self._app_with_oauth2(include_upload=True)
        _wire_per_layer_or_scopes(app)

        first = app.openapi()
        # A marker the generator would never produce: it can only still be
        # present on the second call if the cached dict was returned rather
        # than the schema being rebuilt (and re-rewritten) per request.
        first["x-cache-marker"] = "sentinel"
        second = app.openapi()

        assert second is first
        assert second["x-cache-marker"] == "sentinel"

    def test_no_oauth2_scheme_leaves_security_untouched(self):
        """Auth-disabled builds declare no securitySchemes — the rewrite must
        not name a scheme that does not exist in the document."""
        from fastapi import FastAPI

        from audittrace.server import _wire_per_layer_or_scopes

        app = FastAPI()

        @app.post("/memory/upload")
        async def upload() -> dict[str, str]:
            return {"ok": "yes"}

        _wire_per_layer_or_scopes(app)
        schema = app.openapi()

        assert "securitySchemes" not in schema.get("components", {})
        # The operation exists but carries no dangling security reference.
        assert "security" not in schema["paths"]["/memory/upload"]["post"]

    def test_missing_route_is_skipped_not_fabricated(self):
        """When the memory router is not mounted, the rewrite must skip those
        entries rather than materialise paths the service does not serve."""
        from audittrace.server import _wire_per_layer_or_scopes

        app = self._app_with_oauth2(include_upload=False)
        _wire_per_layer_or_scopes(app)
        schema = app.openapi()

        assert "/memory/upload" not in schema["paths"]
        assert "/memory/index" not in schema["paths"]
        # The scheme was found, so the rewrite ran — it simply had nothing
        # to rewrite. The routes that do exist are untouched.
        assert "/health" in schema["paths"]

    def test_present_route_gets_or_scope_alternatives(self):
        """Positive control: a mounted /memory/upload gets one security entry
        per accepted scope, which is how OpenAPI expresses OR."""
        from audittrace.server import _wire_per_layer_or_scopes

        app = self._app_with_oauth2(include_upload=True)
        _wire_per_layer_or_scopes(app)
        schema = app.openapi()

        security = schema["paths"]["/memory/upload"]["post"]["security"]
        scope_sets = [next(iter(entry.values())) for entry in security]
        assert scope_sets == [
            ["memory:episodic:write"],
            ["memory:procedural:write"],
            ["audittrace:admin"],
        ]


class TestConnectScanAmqpOrDegrade:
    """#384 WS3 — the eager scan-pipeline AMQP connect FAILS OPEN. A
    RabbitMQ that isn't AMQP-ready at startup (cold reboot) must not
    crash the lifespan; the audit plane keeps serving recall while the
    publisher/consumers self-connect lazily once the broker is up."""

    async def test_success_returns_true(self) -> None:
        client = MagicMock()
        client.ensure_connected = AsyncMock()
        assert await _connect_scan_amqp_or_degrade(client) is True
        client.ensure_connected.assert_awaited_once()

    async def test_connect_budget_exhausted_degrades_to_lazy(self) -> None:
        # The exact RuntimeError ScanAmqpClient raises on budget
        # exhaustion — swallowed + logged, returns False (fail-open).
        client = MagicMock()
        client.ensure_connected = AsyncMock(
            side_effect=RuntimeError(
                "scan_amqp.connect failed after 9 attempts "
                "(last error: ConnectionResetError(104, 'reset'))"
            )
        )
        assert await _connect_scan_amqp_or_degrade(client) is False

    async def test_unrelated_runtime_error_propagates(self) -> None:
        # Only the connect-exhaustion RuntimeError is swallowed; a
        # genuine misconfiguration must still fail loudly.
        client = MagicMock()
        client.ensure_connected = AsyncMock(
            side_effect=RuntimeError("scan_amqp_url is required")
        )
        with pytest.raises(RuntimeError, match="scan_amqp_url is required"):
            await _connect_scan_amqp_or_degrade(client)


class TestBootstrapScanPipeline:
    """#384 WS3 — even when the eager AMQP connect is exhausted, the
    lifespan bootstrap does NOT raise, still sets ``app.state.scan_queue``,
    and still creates the four background tasks (they self-connect once
    RabbitMQ is up — invariant I2)."""

    async def test_fails_open_sets_queue_and_creates_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[str] = []

        class _FakeAmqp:
            def __init__(self, settings: object) -> None:
                self.aclose = AsyncMock()

            async def ensure_connected(self) -> None:
                # Cold RabbitMQ: budget exhausted.
                raise RuntimeError(
                    "scan_amqp.connect failed after 9 attempts "
                    "(last error: ConnectionResetError(104, 'reset'))"
                )

        def _make_task_cls(label: str) -> type:
            class _FakeTask:
                def __init__(self, **_kwargs: object) -> None:
                    started.append(label)

                async def run(self) -> None:
                    await asyncio.Event().wait()  # park until cancelled

                async def aclose(self) -> None:
                    return None

            return _FakeTask

        monkeypatch.setattr(
            "audittrace.services.scan_amqp_client.ScanAmqpClient", _FakeAmqp
        )
        monkeypatch.setattr(
            "audittrace.services.scan_request_publisher.ScanRequestPublisher",
            _make_task_cls("publisher"),
        )
        monkeypatch.setattr(
            "audittrace.services.scan_request_janitor.ScanRequestJanitor",
            _make_task_cls("janitor"),
        )
        monkeypatch.setattr(
            "audittrace.services.scan_verdict_consumer.ScanVerdictConsumer",
            _make_task_cls("verdict"),
        )
        monkeypatch.setattr(
            "audittrace.services.scan_audit_consumer.ScanAuditConsumer",
            _make_task_cls("audit"),
        )
        # SPEC #387 Phase 1 (WU-1/WU-2/WU-3) — the auto-index outbox worker
        # + janitor are wired in the same bootstrap alongside the four
        # scan-pipeline tasks above; mock them the same way so this test
        # stays a pure "does bootstrap fail open + create every task"
        # check, not a real-DI-dependent one.
        monkeypatch.setattr(
            "audittrace.services.index_worker.IndexWorker",
            _make_task_cls("index-worker"),
        )
        monkeypatch.setattr(
            "audittrace.services.index_janitor.IndexJanitor",
            _make_task_cls("index-janitor"),
        )
        fake_factory = MagicMock()
        fake_factory.get_session_factory.return_value = MagicMock()
        monkeypatch.setattr(
            "audittrace.server.get_postgres_factory", lambda: fake_factory
        )

        app = SimpleNamespace(state=SimpleNamespace())
        settings = SimpleNamespace(
            scan_request_exchange="audittrace.scan",
            scan_request_routing_key="scan.requested",
        )

        stack = await _bootstrap_scan_pipeline(app, settings)  # must NOT raise
        try:
            # Fail-open: queue is live so /memory/upload can enqueue.
            assert isinstance(app.state.scan_queue, asyncio.Queue)
            # All six background tasks were created despite the dead broker
            # (the original four scan-pipeline tasks + SPEC #387's
            # index-worker + index-janitor).
            assert sorted(started) == [
                "audit",
                "index-janitor",
                "index-worker",
                "janitor",
                "publisher",
                "verdict",
            ]
        finally:
            await stack.aclose()  # cancels tasks + runs aclose callbacks
