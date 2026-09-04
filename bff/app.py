"""FastAPI application factory for the LibreChat BFF.

Three routes matter: ``POST /v1/chat/completions``, the proxy target
LibreChat's custom endpoint config points at; ``GET/POST/PUT/DELETE
/memory/{path}``, the Souvenirs panel's memory-proxy (M3-WU-D2-1); and
``POST /console/files``, the console's narrow-scope ephemeral file-ingest
entry (M3 Sovereign-Attach WU-2). ``GET /health`` is the k8s-probe
convenience every AuditTrace deployable carries.

All three proxy routes share one fail-closed shape (see the module
docstrings in ``bff/auth.py`` / ``bff/exchange.py`` / ``bff/proxy.py`` /
``bff/memory_proxy.py`` for the guard each step enforces):

1. Extract ``Authorization: Bearer <token>`` from the inbound request.
   Missing/malformed → 401, orchestrator never contacted.
2. Validate the token against the AuditTrace realm's JWKS. Invalid/
   expired/wrong-issuer → 401, orchestrator never contacted.
3. RFC 8693 token-exchange it for an ``aud=audittrace-server`` token
   minted for the SAME ``sub``. Keycloak failure / identity mismatch →
   502, orchestrator never contacted. The chat route exchanges for
   ``audittrace:query`` (the default scope, unchanged since WU-2); the
   memory route exchanges explicitly for
   ``bff.memory_scopes.MEMORY_SCOPE_STRING``; the console-files route
   exchanges explicitly for
   ``bff.console_files_scopes.INGEST_SCOPE_STRING`` — a single scope,
   ``memory:session:write``, never the memory route's broad set, never
   ``audittrace:admin``.
4. Proxy the raw request body to the orchestrator with the minted token,
   streaming the response back unchanged — including a 401/403/404 the
   orchestrator itself returns, which is forwarded as-is (fail-closed:
   the BFF never manufactures access the exchanged token doesn't carry).
   Orchestrator unreachable → 502. The console-files route additionally
   FORCES the upstream ``/memory/upload`` request's ``layer`` query
   parameter to ``settings.console_files_forced_layer`` regardless of
   what (if anything) the caller's own query string carries — the
   console cannot choose a durable layer from this seam.

There is no code path in this module that can proxy a request without a
freshly minted, per-caller token — the fail-closed guarantee is
structural, not just an if-check.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bff.auth import InboundTokenError, validate_inbound_token
from bff.config import Settings, get_settings
from bff.console_files_scopes import INGEST_SCOPE_STRING
from bff.exchange import TokenExchangeError, exchange_token
from bff.memory_proxy import MemoryProxyError, proxy_memory_request
from bff.memory_scopes import MEMORY_SCOPE_STRING
from bff.proxy import ProxyError, proxy_chat_completions

logger = logging.getLogger(__name__)

# One pooled AsyncClient for the process lifespan (PYTHON-ENGINEERING §2):
# JWKS fetches, RFC 8693 token-exchange, and the orchestrator proxy all
# share this single connection pool. Created in the lifespan handler,
# NOT at import time (binding to the import-time event loop would break
# under a different running loop — same discipline as
# ``audittrace.auth._get_jwks_fetch_lock``); reachable via the
# ``get_http_client`` FastAPI dependency so tests can override it with a
# ``httpx.MockTransport``-backed client.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """FastAPI dependency: the process-wide pooled HTTP client.

    Raises if called before the lifespan has started the client — that
    would be a genuine startup-ordering bug, not a request-time failure
    to paper over.
    """
    if _http_client is None:
        raise RuntimeError("http client not initialised — app lifespan has not started")
    return _http_client


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _http_client
    # Fail fast at container startup, not on the first request, if a
    # required setting (e.g. ``exchange_client_secret``) is unset —
    # pydantic-settings raises ``ValidationError`` from the constructor.
    settings = get_settings()
    # ``verify=<path>`` trusts an additional local CA (laptop front
    # door's self-signed cert); ``verify=True`` (empty setting) keeps
    # httpx's normal certifi trust store (cloud rig's real cert). See
    # ``bff/config.py::Settings.ca_bundle_path``.
    _http_client = httpx.AsyncClient(verify=settings.ca_bundle_path or True)
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="audittrace-librechat-bff",
        description=(
            "Backend-for-Frontend sidecar (ADR-042 §5 Option A): validates "
            "the token LibreChat forwards, RFC 8693 token-exchanges it for "
            "an aud=audittrace-server token, and proxies /v1/chat/completions "
            "byte-identical."
        ),
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "component": "audittrace-librechat-bff"}

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning("rejecting request — inbound token invalid: %s", exc)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # validate_inbound_token raises InboundTokenError for a falsy
        # token (see bff/auth.py), so reaching here means it is non-None
        # — the assert makes that narrowing explicit for mypy too.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
            )
        except TokenExchangeError as exc:
            logger.error("token exchange failed for sub=%s: %s", inbound_sub, exc)
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type", "application/json")
        try:
            return await proxy_chat_completions(
                raw_body, content_type, minted_token, settings, http_client
            )
        except ProxyError as exc:
            logger.error("orchestrator unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/memory/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
        response_model=None,
    )
    async def memory_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning("rejecting /memory request — inbound token invalid: %s", exc)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as chat_completions — validate_inbound_token raises
        # for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=MEMORY_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "memory token exchange failed for sub=%s: %s", inbound_sub, exc
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_memory_request(
                request.method,
                path,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except MemoryProxyError as exc:
            logger.error("orchestrator /memory unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.post("/console/files", response_model=None)
    async def console_files(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console's narrow ephemeral file-ingest entry (M3
        Sovereign-Attach WU-2). Exchanges for ONLY
        ``memory:session:write`` and proxies the multipart upload
        byte-faithful to ``/memory/upload``, with the ``layer`` query
        parameter FORCED to ``settings.console_files_forced_layer`` —
        any ``layer`` the caller's own query string carries is ignored,
        never honoured. See the module docstring for the shared
        fail-closed shape.
        """
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/files request — inbound token invalid: %s", exc
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as chat_completions/memory_proxy —
        # validate_inbound_token raises for a falsy token, so reaching
        # here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=INGEST_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-files token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        # Forced server-side, NOT read from request.url.query — the
        # console cannot target any layer other than the configured
        # ephemeral one from this seam (see the WU-2 spec's "forced
        # layer" frozen invariant).
        forced_query_string = f"layer={settings.console_files_forced_layer}"
        try:
            return await proxy_memory_request(
                "POST",
                "upload",
                forced_query_string,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except MemoryProxyError as exc:
            logger.error("orchestrator /memory/upload unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    return app


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header.

    Returns ``None`` for a missing header or any shape other than the
    exact ``Bearer <token>`` form — :func:`bff.auth.validate_inbound_token`
    treats ``None`` the same as an explicitly empty token (fail-closed
    401), so a malformed scheme never silently degrades to "no auth".
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


# Module-level ASGI app instance for ``uvicorn bff.app:app``.
app = create_app()
