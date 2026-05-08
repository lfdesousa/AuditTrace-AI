"""FastAPI application factory for audittrace-server."""

import asyncio
from contextlib import asynccontextmanager
from logging import getLogger
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from audittrace import telemetry
from audittrace.config import get_settings
from audittrace.db.rls import install_rls_listener
from audittrace.dependencies import (
    get_postgres_factory,
    register_default_dependencies,
)
from audittrace.logging_config import setup_logging
from audittrace.routes import admin, audit, chat, context, health, memory, session
from audittrace.services.session_summarizer import SessionSummarizer

logger = getLogger(__name__)


def urllib3_set_server_address(span: Any, _instance: Any, request_info: Any) -> None:
    """Back-fill ``server.address`` + ``server.port`` from the request URL.

    The opentelemetry-instrumentation-urllib3 package at 0.62b0 emits
    ``http.url`` / ``url.full`` on client spans but does NOT set
    ``server.address`` or ``net.peer.name`` in either HTTP semconv
    opt-in mode. Without one of those, Tempo's service-graph processor
    cannot materialise a peer node for MinIO traffic (the ``minio``
    Python SDK uses urllib3 directly).

    Wired as the ``request_hook`` callback on
    ``URLLib3Instrumentor().instrument(...)`` during lifespan setup.
    Must never raise — logging a failure would be noise on every
    outbound request and OTel swallows exceptions here anyway. See
    ADR-029 for the full rationale.
    """
    if span is None or not span.is_recording():
        return
    try:
        parsed = urlparse(request_info.url)
        if parsed.hostname:
            span.set_attribute("server.address", parsed.hostname)
        if parsed.port:
            span.set_attribute("server.port", parsed.port)
    except Exception:  # pragma: no cover - hook must never raise
        pass


def _build_httpx_peer_service_map(settings: Any) -> dict[int, str]:
    """Build a ``{port: peer.service}`` map from the configured LLM URLs.

    Returns the port → service-name mapping used by ``httpx_set_peer_service``
    to disambiguate the three llama-server endpoints on a shared host.
    The map is derived from settings so re-pointing ``AUDITTRACE_*_URL`` at a
    different port updates the service-graph label for free.
    """
    mapping: dict[int, str] = {}
    for attr, name in (
        ("llama_url", "qwen-chat-llm"),
        ("embed_url", "nomic-embed-server"),
        ("summarizer_url", "mistral-summariser-llm"),
        ("langfuse_host", "langfuse"),
    ):
        raw = getattr(settings, attr, None)
        if not raw:
            continue
        try:
            port = urlparse(raw).port
        except Exception:  # pragma: no cover - defensive
            port = None
        if port is not None:
            mapping[port] = name
    return mapping


def _apply_peer_service(
    span: Any, request_info: Any, peer_service_by_port: dict[int, str]
) -> None:
    """Shared peer.service logic used by both sync and async hooks."""
    if span is None or not span.is_recording():
        return
    try:
        parsed = urlparse(str(request_info.url))
        if parsed.port is None:
            return
        name = peer_service_by_port.get(parsed.port)
        if name is not None:
            span.set_attribute("peer.service", name)
    except Exception:  # pragma: no cover - hook must never raise
        pass


def make_httpx_peer_service_hook(
    peer_service_by_port: dict[int, str],
) -> Any:
    """Closure factory for the sync httpx ``request_hook``.

    Tempo's service-graph processor collapses every HTTP outbound to a
    shared hostname into a single edge. On a single-host deployment all
    three llama-servers live under ``host.docker.internal`` and the
    default service graph shows one lumped edge per host. Setting
    ``peer.service`` explicitly per destination splits that into three
    semantic edges (``qwen-chat-llm``, ``nomic-embed-server``,
    ``mistral-summariser-llm``). Tempo reads ``peer.service`` ahead of
    ``server.address`` when it is present. Requests to destinations not
    in the map fall back to ``server.address``.
    """

    def _hook(span: Any, request_info: Any) -> None:
        _apply_peer_service(span, request_info, peer_service_by_port)

    return _hook


def make_httpx_async_peer_service_hook(
    peer_service_by_port: dict[int, str],
) -> Any:
    """Async counterpart for ``httpx.AsyncClient`` outbound calls.

    The ``opentelemetry-instrumentation-httpx`` package discriminates
    between sync (``Client``) and async (``AsyncClient``) hooks by
    coroutine-function check (``iscoroutinefunction``). A sync hook
    passed as ``async_request_hook`` is silently dropped — which is how
    the Mistral summariser's outbound spans went unlabelled on the
    first wiring pass. This factory returns a coroutine function with
    the same body so both transports emit ``peer.service``.
    """

    async def _hook(span: Any, request_info: Any) -> None:
        _apply_peer_service(span, request_info, peer_service_by_port)

    return _hook


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Application lifespan handler - startup and shutdown."""
    settings = get_settings()
    setup_logging(level=settings.log_level, structured=False)

    telemetry.init_telemetry(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otlp_endpoint,
        tracing_enabled=settings.tracing_enabled,
        metrics_enabled=settings.metrics_enabled,
    )

    # Auto-instrument outbound I/O AFTER init_telemetry so the
    # instrumentors bind their closures to the concrete TracerProvider
    # that Langfuse just installed as global — not the earlier
    # ProxyTracerProvider. Spans then fan out to both Tempo (via our
    # OTLP processor attached in init_telemetry) and Langfuse.
    # SQLAlchemy is instrumented per-engine in db/postgres.py so query
    # spans (SELECT/INSERT) are emitted. Global SQLAlchemyInstrumentor()
    # was tried here and produced connect-only spans — engine-scoped is
    # the supported hook for before/after_cursor_execute events.
    if settings.tracing_enabled:  # pragma: no cover - smoke-tested live, not in pytest
        try:
            from opentelemetry import trace
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            from opentelemetry.instrumentation.redis import RedisInstrumentor
            from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

            tp = trace.get_tracer_provider()
            peer_service_map = _build_httpx_peer_service_map(settings)
            HTTPXClientInstrumentor().instrument(
                tracer_provider=tp,
                request_hook=make_httpx_peer_service_hook(peer_service_map),
                async_request_hook=make_httpx_async_peer_service_hook(peer_service_map),
            )
            URLLib3Instrumentor().instrument(
                tracer_provider=tp,
                request_hook=urllib3_set_server_address,
            )
            RedisInstrumentor().instrument(tracer_provider=tp)
            logger.info(
                "OTel outbound instrumentation: HTTPX (peer.service map=%s) + urllib3 + Redis",
                peer_service_map,
            )
        except Exception as e:
            logger.warning("Outbound OTel instrumentation failed: %s", e)

    # Register DI container with all services (ADR-020)
    register_default_dependencies(settings)

    # Install the Phase 4 RLS after_begin listener so every DB
    # transaction pushes app.current_user_id into Postgres for RLS
    # evaluation. Idempotent and a no-op on SQLite (tests).
    install_rls_listener()

    logger.info("Starting audittrace-server v0.3.1")
    logger.info("LLM URL: %s", settings.llama_url)
    logger.info("ChromaDB URL: %s", settings.chroma_url)
    logger.info("Auth enabled: %s", settings.auth_enabled)
    logger.info("Log level: %s", settings.log_level)
    logger.info(
        "OTel: service=%s export=%s",
        settings.otel_service_name,
        settings.otlp_endpoint or "disabled",
    )

    # ADR-030 Part 2 — background session summariser. Started here so
    # it shares the FastAPI event loop and is cancelled cleanly on
    # shutdown. Guarded by ``summarizer_enabled`` so operators can
    # disable without removing settings. Exercised via live stack
    # spin-up, not pytest (the summariser itself is unit-tested in
    # test_session_summarizer.py).
    #
    # The summariser uses a DEDICATED Postgres factory when
    # ``summarizer_postgres_url`` is set, built with the owner-role
    # credentials so ``SET LOCAL row_security = off`` in the
    # eligibility txn actually bypasses RLS for the cross-user read.
    # Without a dedicated URL (tests, single-tenant dev), falls back
    # to the main factory — RLS is then the caller's problem.
    summarizer_task: asyncio.Task[None] | None = None
    if (
        settings.summarizer_enabled and settings.summarizer_database_url
    ):  # pragma: no cover - live-startup path
        if (
            settings.summarizer_postgres_url
            and settings.summarizer_postgres_url != settings.database_url
        ):
            from audittrace.db.postgres import URLPostgresFactory

            summarizer_factory = URLPostgresFactory(
                settings.summarizer_postgres_url, pool_size=2
            )
            summarizer_session_factory = summarizer_factory.get_session_factory()
            logger.info("Session summariser: using dedicated owner-role connection")
        else:
            summarizer_session_factory = get_postgres_factory().get_session_factory()
            logger.info(
                "Session summariser: sharing main Postgres factory (RLS bypass may not apply)"
            )
        summarizer = SessionSummarizer(
            settings=settings,
            session_factory=summarizer_session_factory,
        )
        summarizer_task = asyncio.create_task(
            summarizer.run(), name="session-summarizer"
        )
        logger.info(
            "Session summariser scheduled — idle=%dm interval=%dm",
            settings.summarizer_idle_minutes,
            settings.summarizer_interval_minutes,
        )
    else:
        logger.info(
            "Session summariser NOT started (enabled=%s, db=%s)",
            settings.summarizer_enabled,
            "yes" if settings.summarizer_database_url else "no",
        )

    # ADR-046 — async chat-completion persistence consumer worker.
    # Per-pod background task; multi-pod-safe via Redis consumer groups.
    # Gated by ``async_persist_enabled`` so the worker is dormant until
    # the chart values opt in (default off until live evidence captured).
    async_persist_task: asyncio.Task[None] | None = None
    if settings.async_persist_enabled:  # pragma: no cover - live-startup path
        from audittrace.routes.chat import (
            _flush_pending_tool_calls,
            _persist_interaction,
        )
        from audittrace.services.async_persist import (
            AsyncPersistConsumer,
            get_async_persist_redis,
        )

        async_persist_consumer = AsyncPersistConsumer(
            settings=settings,
            persist_callable=_persist_interaction,
            flush_tool_calls_callable=_flush_pending_tool_calls,
            redis=get_async_persist_redis(),
        )
        async_persist_task = asyncio.create_task(
            async_persist_consumer.run(), name="async-persist-consumer"
        )
        logger.info(
            "Async-persist consumer scheduled — group=%s consumer=%s stream=%s",
            settings.async_persist_group,
            async_persist_consumer.consumer_name,
            settings.async_persist_stream,
        )
    else:
        logger.info("Async-persist consumer NOT started (async_persist_enabled=False)")

    yield

    logger.info("Shutting down audittrace-server")
    if summarizer_task is not None:  # pragma: no cover - paired with startup
        summarizer_task.cancel()
        try:
            await asyncio.wait_for(summarizer_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
    if async_persist_task is not None:  # pragma: no cover - paired with startup
        async_persist_task.cancel()
        try:
            await asyncio.wait_for(async_persist_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
    telemetry.shutdown()


def _resolve_version() -> str:
    """Read the package version from installed metadata.

    Tries ``audittrace-ai`` (the pyproject ``project.name``); falls back
    to a constant when running from a source tree without
    ``pip install -e``. The constant tracks the latest release tag.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("audittrace-ai")
    except (PackageNotFoundError, ImportError):  # pragma: no cover - dev path
        return "1.0.13"


# Tag descriptions populate the Swagger UI sidebar. Keep one paragraph
# per tag — ``description`` is rendered as Markdown.
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "chat",
        "description": (
            "OpenAI-compatible chat completions proxy. The default POST shape "
            "is intentionally permissive (raw dict pass-through) per ADR-024 "
            "to preserve unknown tool-calling fields. Memory augmentation is "
            "merged into the system message; an opt-in ``X-Persist-Mode: async`` "
            "header (ADR-046) routes the audit-row write through Redis Streams "
            "instead of the synchronous Postgres write on the response path."
        ),
    },
    {
        "name": "context",
        "description": (
            "Build the augmented system context across all four memory layers "
            "(episodic, procedural, conversational, semantic) without going "
            "through chat completions. Used by the webui memory tab and by "
            "external agents that want to inspect what would be injected."
        ),
    },
    {
        "name": "memory",
        "description": (
            "CRUD over the four memory layers. Episodic = ADRs, "
            "Procedural = skill files, Conversational = past sessions "
            "(read-only), Semantic = ChromaDB-backed RAG. Each write is "
            "scoped to the caller's Keycloak ``sub`` and gated by RLS at the "
            "Postgres + ChromaDB layers."
        ),
    },
    {
        "name": "session",
        "description": (
            "Save + summarise full chat sessions for replay and audit. Used "
            "by the OpenCode bridge to land long-running conversations into "
            "the conversational layer."
        ),
    },
    {
        "name": "audit",
        "description": (
            "Read-only audit endpoints: ``/interactions`` returns every "
            "chat-completion row with token counts, status, failure-class, "
            "and trace_id; ``/sessions`` aggregates them by session. "
            "EU-AI-Act Article 12 compliance hook."
        ),
    },
    {
        "name": "health",
        "description": (
            "Kubernetes-grade liveness + metrics endpoints. ``/health`` "
            "surfaces the summariser ctx-window and async-persist runtime "
            "state alongside the standard component checks."
        ),
    },
]


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="AuditTrace-AI",
        description=(
            "EU-AI-Act-aligned audit trail for LLM workloads. "
            "OpenAI-compatible ``/v1/chat/completions`` proxy with 4-layer memory "
            "augmentation (episodic + procedural + conversational + semantic), "
            "Keycloak-delegated identity, Postgres RLS per-user isolation, and "
            "opt-in Redis-Streams async persistence (ADR-046).\n\n"
            "See ``docs/architecture/`` for C4 + sequence diagrams; the ADR set "
            "(014–046) is the source of truth for design decisions."
        ),
        version=_resolve_version(),
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

    # CORS — required by the minimalist webui (ADR-042 reference impl)
    # and any first-party SPA that's not behind a same-origin BFF. Allowed
    # origins come from settings.cors_origins (env: AUDITTRACE_CORS_ORIGINS).
    # Empty list disables CORS entirely (production-safe default for BFF
    # deployments). Auth header + content-type are explicitly listed so
    # preflight passes for `Authorization: Bearer ...` requests.
    cors_origins = get_settings().cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Project",
                "X-Memory-Mode",
                "X-Thinking",
                "X-Async",
            ],
            expose_headers=["X-Trace-Id"],
        )

    app.include_router(chat.router, prefix="/v1", tags=["chat"])
    app.include_router(context.router, tags=["context"])
    app.include_router(audit.router, tags=["audit"])
    app.include_router(session.router, prefix="/session", tags=["session"])
    app.include_router(memory.router, prefix="/memory", tags=["memory"])
    # `/system/*` (not `/admin/*`) because Keycloak owns `/admin/*`
    # under the same Istio gateway (templates/istio/virtualservice-
    # keycloak.yaml routes `/admin` to keycloak's REST API). ADR-052
    # §5 — public path is /system/trust-store/*; the Python module
    # stays `routes/admin.py` because semantically these are the
    # operator-admin endpoints.
    app.include_router(admin.router, prefix="/system", tags=["system"])
    app.include_router(health.router, tags=["health"])

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request[Any], exc: Exception
    ) -> JSONResponse:
        """Return the ADR-033 3-audience error envelope for unhandled 500s.

        Non-streaming routes only: FastAPI cannot intercept an exception
        raised inside a ``StreamingResponse`` generator after the response
        headers have been sent — the streaming chat path in
        ``routes/chat.py`` handles its own taxonomy.

        The envelope shape is the seed for ADR-033: one payload with
        signal for three audiences (user, operator, engineer). ``trace_id``
        is the pivot that links the 5xx back to Loki / Langfuse / Grafana.
        """
        from opentelemetry import trace

        try:
            span = trace.get_current_span()
            ctx = span.get_span_context() if span is not None else None
            trace_id_hex = (
                format(ctx.trace_id, "032x") if ctx and ctx.is_valid else None
            )
        except Exception:  # pragma: no cover - defensive
            trace_id_hex = None

        logger.exception(
            "Unhandled exception on %s %s (trace_id=%s): %s",
            request.method,
            request.url.path,
            trace_id_hex,
            exc,
        )
        # Envelope is a strict SUPERSET of OpenAI's error shape
        # ``{message, type, param, code}`` so any OpenAI SDK keeps parsing
        # unchanged. AuditTrace-specific keys (``status``,
        # ``operator_hint``, ``trace_id``, ``user_facing_message``) are
        # additive — OpenAI-only readers simply ignore them. See
        # ``feedback_openai_schema_inviolate`` memory; ADR-033 when it
        # lands documents the full contract.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "An internal error occurred.",
                    "type": "api_error",
                    "param": None,
                    "code": "internal_error",
                    "status": 500,
                    "operator_hint": (
                        "Grep memory-server logs in Loki with this trace_id; "
                        "cross-reference Langfuse observations."
                    ),
                    "trace_id": trace_id_hex,
                    "user_facing_message": ("Something went wrong. Please try again."),
                }
            },
        )

    # FastAPI instrumentation must run at app-construction time so the
    # patched ``build_middleware_stack`` is in place before uvicorn
    # builds the stack on first request. Outbound instrumentors
    # (HTTPX/SQLAlchemy/Redis) live in the lifespan handler so they
    # bind to the concrete TracerProvider Langfuse installs there.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:  # pragma: no cover
        logger.warning("FastAPI OTel instrumentation failed: %s", e)

    # Custom OpenAPI generator: emit OR-semantics security scopes for
    # routes whose required scope is dynamic (per-layer). FastAPI's
    # static ``Security(fn, scopes=[...])`` is AND-only — so per-layer
    # routes use ``scopes=[]`` at the decorator level + a runtime
    # check against the parsed ``layer`` query param. This generator
    # rewrites the resulting empty ``security`` block into an OpenAPI
    # 3.x OR-shape (``[{OAuth2: [scopeA]}, {OAuth2: [scopeB]}, ...]``)
    # so the spec accurately reflects "any one of these scopes is
    # sufficient." Reviewers + Bruno-collection consumers see the
    # contract from the spec alone — no need to read prose.
    _wire_per_layer_or_scopes(app)

    return app


# Routes whose required scope is dynamic at runtime (parsed from a
# query param). For each, the OpenAPI spec must declare the *union*
# of acceptable scope grants — no single static decorator can express
# this shape. Listed here so the OpenAPI post-processor below applies
# the rewrite consistently and any new per-layer route is a single-
# line addition rather than a fresh patch.
#
# Format: (HTTP method lowercase, path) → list of scope alternatives.
# Each alternative becomes one OpenAPI security requirement object,
# yielding OR semantics across the list.
_PER_LAYER_OR_SCOPES: dict[tuple[str, str], list[list[str]]] = {
    ("post", "/memory/upload"): [
        ["memory:episodic:write"],
        ["memory:procedural:write"],
        ["audittrace:admin"],
    ],
    # /memory/index has two runtime modes:
    #   - bulk (no ?file=) → audittrace:admin
    #   - single-file (?file=<layer>/<key>) → memory:<layer>:write OR admin
    # Spec declares the union: any of the three is *sufficient* to
    # call the operation; the body branches on ?file= to enforce the
    # narrower bulk-only-admin rule. Documented in the route prose
    # (and tested by tests/test_memory_routes.py::TestIndexAuth).
    ("post", "/memory/index"): [
        ["memory:episodic:write"],
        ["memory:procedural:write"],
        ["audittrace:admin"],
    ],
}


def _wire_per_layer_or_scopes(app: FastAPI) -> None:
    """Install ``app.openapi`` override that rewrites the ``security``
    block on the routes listed in :data:`_PER_LAYER_OR_SCOPES`.

    The rewrite uses the OAuth2 scheme name FastAPI picks for our
    ``OAuth2PasswordBearer`` instance — looked up dynamically from the
    generated schema rather than hardcoded so a future scheme rename
    does not silently break the rewrite.
    """
    from fastapi.openapi.utils import get_openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        # Find the OAuth2 scheme name. There is exactly one in our
        # config (``OAuth2PasswordBearer`` / ``OAuth2`` per FastAPI's
        # auto-naming); guard for absence to keep dev-mode (no auth)
        # builds happy.
        sec_schemes: dict[str, Any] = schema.get("components", {}).get(
            "securitySchemes", {}
        )
        oauth2_name = next(
            (
                name
                for name, defn in sec_schemes.items()
                if defn.get("type") == "oauth2"
            ),
            None,
        )
        if oauth2_name is None:
            app.openapi_schema = schema
            return schema
        for (method, path), alternatives in _PER_LAYER_OR_SCOPES.items():
            op = schema.get("paths", {}).get(path, {}).get(method)
            if op is None:
                continue
            op["security"] = [{oauth2_name: scope_list} for scope_list in alternatives]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


app = create_app()


def main() -> None:  # pragma: no cover
    """Entry point for CLI execution (exercised via the docker entrypoint)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "audittrace.server:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
