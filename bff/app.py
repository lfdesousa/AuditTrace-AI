"""FastAPI application factory for the LibreChat BFF.

Nine routes matter: ``POST /v1/chat/completions``, the proxy target
LibreChat's custom endpoint config points at; ``GET/POST/PUT/DELETE
/memory/{path}``, the Souvenirs panel's memory-proxy (M3-WU-D2-1);
``POST /console/files``, the console's narrow-scope ephemeral file-ingest
entry (M3 Sovereign-Attach WU-2); ``POST /console/files/{filename}/
promote``, the "keep this" durable-promote entry (M3 Sovereign-Attach
WU-4); ``GET/POST/PATCH/DELETE /console/conversations[/{path}]``, the
console-conversations proxy (WU-1, MongoDB-elimination EPIC);
``GET/POST/DELETE /console/presets[/{path}]``, the console-presets proxy
(Mongo-repl WU-presets, MongoDB-elimination EPIC); and
``GET/POST/PATCH/DELETE /console/prompts[/{path}]``, the console-prompts
proxy (Mongo-repl WU-prompts, MongoDB-elimination EPIC); and
``GET/POST/DELETE /console/chat-projects[/{path}]``, the
console-chat-projects proxy (Chat-Projects domain, MongoDB-elimination
EPIC); and ``GET/POST/DELETE /console/file-records[/{path}]``, the
console-files-METADATA proxy (Files-metadata domain, MongoDB-elimination
EPIC) — named ``file-records`` rather than ``files`` on THIS side only,
to avoid colliding with the pre-existing ``/console/files`` ephemeral-
ingest route above (see ``bff/config.py``'s
``orchestrator_console_files_path_prefix`` docstring for the full
rationale); it forwards to the orchestrator's real, spec-literal
``/console/files`` mount. ``GET/POST/DELETE /console/agents[/{path}]``,
the console-agents proxy (Agents domain, MongoDB-elimination EPIC) —
own-agents-only v1, no naming collision on this side.
``GET/POST/DELETE /console/conversation-tags[/{path}]``, the
console-conversation-tags proxy (Conversation-Tags domain, MongoDB-
elimination EPIC) — own-tags-only v1, no naming collision on this side.
``GET /health`` is the k8s-probe convenience every AuditTrace deployable
carries.

All proxy routes share one fail-closed shape (see the module
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
   ``audittrace:admin``; the console-files-promote route exchanges
   explicitly for the SINGLE configured durable scope
   (``bff.console_promote_scopes.promote_scope_string_for_layer``,
   default ``memory:episodic:write``) — a THIRD, distinct exchange, never
   the session scope, never the broad set, never admin; the console-
   conversations proxy exchanges explicitly for
   ``bff.console_conversations_scopes.CONSOLE_CONVERSATIONS_SCOPE_STRING``
   (``memory:conversations:read-own`` + ``memory:conversations:write``) —
   a FOURTH, distinct exchange, own scope pair, never any other route's
   scopes; the console-presets proxy exchanges explicitly for
   ``bff.console_presets_scopes.CONSOLE_PRESETS_SCOPE_STRING``
   (``memory:presets:read-own`` + ``memory:presets:write``) — a FIFTH,
   distinct exchange, own scope pair, never any other route's scopes;
   the console-prompts proxy exchanges explicitly for
   ``bff.console_prompts_scopes.CONSOLE_PROMPTS_SCOPE_STRING``
   (``memory:prompts:read-own`` + ``memory:prompts:write``) — a SIXTH,
   distinct exchange, own scope pair, never any other route's scopes;
   the console-chat-projects proxy exchanges explicitly for
   ``bff.console_chat_projects_scopes.CONSOLE_CHAT_PROJECTS_SCOPE_STRING``
   (``memory:chat_projects:read-own`` + ``memory:chat_projects:write``)
   — a SEVENTH, distinct exchange, own scope pair, never any other
   route's scopes; the console-file-records proxy exchanges explicitly
   for
   ``bff.console_file_records_scopes.CONSOLE_FILE_RECORDS_SCOPE_STRING``
   (``memory:files:read-own`` + ``memory:files:write``) — an EIGHTH,
   distinct exchange, own scope pair, never any other route's scopes
   (in particular never the console-files-ingest route's
   ``memory:session:write``, despite the similar name); the console-
   agents proxy exchanges explicitly for
   ``bff.console_agents_scopes.CONSOLE_AGENTS_SCOPE_STRING``
   (``memory:agents:read-own`` + ``memory:agents:write``) — a NINTH,
   distinct exchange, own scope pair, never any other route's scopes;
   the console-conversation-tags proxy exchanges explicitly for
   ``bff.console_conversation_tags_scopes.CONSOLE_CONVERSATION_TAGS_SCOPE_STRING``
   (``memory:conversation_tags:read-own`` + ``memory:conversation_tags:write``)
   — a TENTH, distinct exchange, own scope pair, never any other route's
   scopes.
4. Proxy the raw request body to the orchestrator with the minted token,
   streaming the response back unchanged — including a 401/403/404 the
   orchestrator itself returns, which is forwarded as-is (fail-closed:
   the BFF never manufactures access the exchanged token doesn't carry).
   Orchestrator unreachable → 502. The console-files route additionally
   FORCES the upstream ``/memory/upload`` request's ``layer`` query
   parameter to ``settings.console_files_forced_layer`` regardless of
   what (if anything) the caller's own query string carries — the
   console cannot choose a durable layer from this seam. The
   console-files-promote route similarly FORCES the upstream
   ``/memory/promote`` request's ``target_layer`` JSON field to
   ``settings.console_promote_default_layer`` — the caller supplies only
   the path-parameter ``filename``, never a target-layer override.

There is no code path in this module that can proxy a request without a
freshly minted, per-caller token — the fail-closed guarantee is
structural, not just an if-check.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bff.auth import InboundTokenError, validate_inbound_token
from bff.config import Settings, get_settings
from bff.console_agents_proxy import (
    ConsoleAgentsProxyError,
    proxy_console_agents_request,
)
from bff.console_agents_scopes import CONSOLE_AGENTS_SCOPE_STRING
from bff.console_chat_projects_proxy import (
    ConsoleChatProjectsProxyError,
    proxy_console_chat_projects_request,
)
from bff.console_chat_projects_scopes import CONSOLE_CHAT_PROJECTS_SCOPE_STRING
from bff.console_conversation_tags_proxy import (
    ConsoleConversationTagsProxyError,
    proxy_console_conversation_tags_request,
)
from bff.console_conversation_tags_scopes import (
    CONSOLE_CONVERSATION_TAGS_SCOPE_STRING,
)
from bff.console_conversations_proxy import (
    ConsoleConversationsProxyError,
    proxy_console_conversations_request,
)
from bff.console_conversations_scopes import CONSOLE_CONVERSATIONS_SCOPE_STRING
from bff.console_file_records_proxy import (
    ConsoleFileRecordsProxyError,
    proxy_console_file_records_request,
)
from bff.console_file_records_scopes import CONSOLE_FILE_RECORDS_SCOPE_STRING
from bff.console_files_scopes import INGEST_SCOPE_STRING
from bff.console_presets_proxy import (
    ConsolePresetsProxyError,
    proxy_console_presets_request,
)
from bff.console_presets_scopes import CONSOLE_PRESETS_SCOPE_STRING
from bff.console_promote_scopes import promote_scope_string_for_layer
from bff.console_prompts_proxy import (
    ConsolePromptsProxyError,
    proxy_console_prompts_request,
)
from bff.console_prompts_scopes import CONSOLE_PROMPTS_SCOPE_STRING
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

    @app.post("/console/files/{filename}/promote", response_model=None)
    async def console_files_promote(
        filename: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console's "keep this" promote entry (M3 Sovereign-Attach
        WU-4): exchanges for ONLY the configured target durable scope
        (``settings.console_promote_default_layer``, default
        ``memory:episodic:write``) — a SEPARATE, narrower exchange from
        both the broad Souvenirs scope set and WU-2's ephemeral ingest
        scope, per ``bff/console_promote_scopes.py`` — and proxies to the
        orchestrator's ``POST /memory/promote`` with a JSON body naming
        *filename* (the session doc reference) and the configured
        ``target_layer``. See the module docstring for the shared
        fail-closed shape.
        """
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/files promote request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        requested_scope = promote_scope_string_for_layer(
            settings.console_promote_default_layer
        )
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=requested_scope,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-files-promote token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        # Body is server-CONSTRUCTED, not forwarded from the caller's own
        # request body — the caller supplies no target_layer/filename
        # override from this seam; ``filename`` is the path parameter,
        # ``target_layer`` is the configured default, matching the
        # console-files route's own "forced, never caller-chosen" wall.
        raw_body = json.dumps(
            {
                "filename": filename,
                "target_layer": settings.console_promote_default_layer,
            }
        ).encode("utf-8")
        try:
            return await proxy_memory_request(
                "POST",
                "promote",
                "",
                raw_body,
                "application/json",
                minted_token,
                settings,
                http_client,
            )
        except MemoryProxyError as exc:
            logger.error("orchestrator /memory/promote unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    async def _console_conversations_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-conversations routes below (the
        base path with no suffix, and the ``{path:path}`` catch-all) —
        same shape as ``memory_proxy`` above, but exchanges for
        ``CONSOLE_CONVERSATIONS_SCOPE_STRING`` and forwards to the
        orchestrator's ``/console/conversations`` mount (WU-1, MongoDB-
        elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/conversations request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_CONVERSATIONS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-conversations token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_conversations_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsoleConversationsProxyError as exc:
            logger.error("orchestrator /console/conversations unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/conversations",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_conversations_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-conversations list/create entry (WU-1, MongoDB-
        elimination EPIC) — no path suffix. See
        ``_console_conversations_proxy_impl`` for the shared shape."""
        return await _console_conversations_proxy_impl(
            "", request, settings, http_client
        )

    @app.api_route(
        "/console/conversations/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
        response_model=None,
    )
    async def console_conversations_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-conversations per-resource entry (WU-1, MongoDB-
        elimination EPIC) — ``{conversation_id}``,
        ``{conversation_id}/messages``,
        ``{conversation_id}/messages/{message_id}``. See
        ``_console_conversations_proxy_impl`` for the shared shape."""
        return await _console_conversations_proxy_impl(
            path, request, settings, http_client
        )

    async def _console_presets_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-presets routes below (the base
        path with no suffix, and the ``{path:path}`` catch-all) — same
        shape as ``_console_conversations_proxy_impl`` above, but
        exchanges for ``CONSOLE_PRESETS_SCOPE_STRING`` and forwards to
        the orchestrator's ``/console/presets`` mount (Mongo-repl
        WU-presets, MongoDB-elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/presets request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_PRESETS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-presets token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_presets_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsolePresetsProxyError as exc:
            logger.error("orchestrator /console/presets unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/presets",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_presets_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-presets list/create entry (Mongo-repl WU-presets,
        MongoDB-elimination EPIC) — no path suffix. See
        ``_console_presets_proxy_impl`` for the shared shape."""
        return await _console_presets_proxy_impl("", request, settings, http_client)

    @app.api_route(
        "/console/presets/{path:path}",
        methods=["GET", "POST", "DELETE"],
        response_model=None,
    )
    async def console_presets_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-presets per-resource entry (Mongo-repl WU-presets,
        MongoDB-elimination EPIC) — ``{preset_id}``. See
        ``_console_presets_proxy_impl`` for the shared shape."""
        return await _console_presets_proxy_impl(path, request, settings, http_client)

    async def _console_prompts_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-prompts routes below (the base
        path with no suffix, and the ``{path:path}`` catch-all) — same
        shape as ``_console_presets_proxy_impl`` above, but exchanges
        for ``CONSOLE_PROMPTS_SCOPE_STRING`` and forwards to the
        orchestrator's ``/console/prompts`` mount (Mongo-repl
        WU-prompts, MongoDB-elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/prompts request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_PROMPTS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-prompts token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_prompts_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsolePromptsProxyError as exc:
            logger.error("orchestrator /console/prompts unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/prompts",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_prompts_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-prompts list-groups/create-group entry (Mongo-repl
        WU-prompts, MongoDB-elimination EPIC) — no path suffix. See
        ``_console_prompts_proxy_impl`` for the shared shape."""
        return await _console_prompts_proxy_impl("", request, settings, http_client)

    @app.api_route(
        "/console/prompts/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
        response_model=None,
    )
    async def console_prompts_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-prompts per-resource entry (Mongo-repl WU-prompts,
        MongoDB-elimination EPIC) — ``{group_id}``,
        ``{group_id}/versions``, ``{group_id}/production``. See
        ``_console_prompts_proxy_impl`` for the shared shape."""
        return await _console_prompts_proxy_impl(path, request, settings, http_client)

    async def _console_chat_projects_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-chat-projects routes below (the
        base path with no suffix, and the ``{path:path}`` catch-all) —
        same shape as ``_console_presets_proxy_impl`` above, but
        exchanges for ``CONSOLE_CHAT_PROJECTS_SCOPE_STRING`` and
        forwards to the orchestrator's ``/console/chat-projects`` mount
        (Chat-Projects domain, MongoDB-elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/chat-projects request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_CHAT_PROJECTS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-chat-projects token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_chat_projects_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsoleChatProjectsProxyError as exc:
            logger.error("orchestrator /console/chat-projects unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/chat-projects",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_chat_projects_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-chat-projects list/create entry (Chat-Projects
        domain, MongoDB-elimination EPIC) — no path suffix. See
        ``_console_chat_projects_proxy_impl`` for the shared shape."""
        return await _console_chat_projects_proxy_impl(
            "", request, settings, http_client
        )

    @app.api_route(
        "/console/chat-projects/{path:path}",
        methods=["GET", "POST", "DELETE"],
        response_model=None,
    )
    async def console_chat_projects_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-chat-projects per-resource entry (Chat-Projects
        domain, MongoDB-elimination EPIC) — ``{chat_project_id}``. See
        ``_console_chat_projects_proxy_impl`` for the shared shape."""
        return await _console_chat_projects_proxy_impl(
            path, request, settings, http_client
        )

    async def _console_file_records_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-file-records routes below (the
        base path with no suffix, and the ``{path:path}`` catch-all) —
        same shape as ``_console_chat_projects_proxy_impl`` above, but
        exchanges for ``CONSOLE_FILE_RECORDS_SCOPE_STRING`` and forwards
        to the orchestrator's ``/console/files`` mount (Files-metadata
        domain, MongoDB-elimination EPIC). See ``bff/config.py``'s
        ``orchestrator_console_files_path_prefix`` docstring for why
        this proxy's OWN BFF-facing path is ``/console/file-records``,
        not ``/console/files``."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/file-records request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_FILE_RECORDS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-file-records token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_file_records_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsoleFileRecordsProxyError as exc:
            logger.error("orchestrator /console/files unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/file-records",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_file_records_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-file-records list/create entry (Files-metadata
        domain, MongoDB-elimination EPIC) — no path suffix. See
        ``_console_file_records_proxy_impl`` for the shared shape."""
        return await _console_file_records_proxy_impl(
            "", request, settings, http_client
        )

    @app.api_route(
        "/console/file-records/{path:path}",
        methods=["GET", "POST", "DELETE"],
        response_model=None,
    )
    async def console_file_records_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-file-records per-resource entry (Files-metadata
        domain, MongoDB-elimination EPIC) — ``{file_id}``,
        ``batch-get``. See ``_console_file_records_proxy_impl`` for the
        shared shape."""
        return await _console_file_records_proxy_impl(
            path, request, settings, http_client
        )

    async def _console_agents_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-agents routes below (the base
        path with no suffix, and the ``{path:path}`` catch-all) — same
        shape as ``_console_chat_projects_proxy_impl`` above, but
        exchanges for ``CONSOLE_AGENTS_SCOPE_STRING`` and forwards to
        the orchestrator's ``/console/agents`` mount (Agents domain,
        MongoDB-elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/agents request — inbound token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_AGENTS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-agents token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_agents_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsoleAgentsProxyError as exc:
            logger.error("orchestrator /console/agents unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/agents",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_agents_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-agents list/create entry (Agents domain,
        MongoDB-elimination EPIC) — no path suffix. See
        ``_console_agents_proxy_impl`` for the shared shape."""
        return await _console_agents_proxy_impl("", request, settings, http_client)

    @app.api_route(
        "/console/agents/{path:path}",
        methods=["GET", "POST", "DELETE"],
        response_model=None,
    )
    async def console_agents_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-agents per-resource entry (Agents domain,
        MongoDB-elimination EPIC) — ``{agent_id}``, ``batch-get``. See
        ``_console_agents_proxy_impl`` for the shared shape."""
        return await _console_agents_proxy_impl(path, request, settings, http_client)

    async def _console_conversation_tags_proxy_impl(
        path_suffix: str,
        request: Request,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> StreamingResponse | JSONResponse:
        """Shared body for both console-conversation-tags routes below
        (the base path with no suffix, and the ``{path:path}``
        catch-all) — same shape as ``_console_agents_proxy_impl`` above,
        but exchanges for ``CONSOLE_CONVERSATION_TAGS_SCOPE_STRING`` and
        forwards to the orchestrator's ``/console/conversation-tags``
        mount (Conversation-Tags domain, MongoDB-elimination EPIC)."""
        token = _extract_bearer_token(request.headers.get("authorization"))
        try:
            claims = await validate_inbound_token(token, settings, http_client)
        except InboundTokenError as exc:
            logger.warning(
                "rejecting /console/conversation-tags request — inbound "
                "token invalid: %s",
                exc,
            )
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Same narrowing as every other route above — validate_inbound_token
        # raises for a falsy token, so reaching here means it is non-None.
        assert token is not None
        inbound_sub = claims["sub"]
        try:
            minted_token = await exchange_token(
                token,
                inbound_sub,
                settings,
                http_client,
                requested_scope=CONSOLE_CONVERSATION_TAGS_SCOPE_STRING,
            )
        except TokenExchangeError as exc:
            logger.error(
                "console-conversation-tags token exchange failed for sub=%s: %s",
                inbound_sub,
                exc,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Upstream authentication service error"},
            )

        raw_body = await request.body()
        content_type = request.headers.get("content-type")
        try:
            return await proxy_console_conversation_tags_request(
                request.method,
                path_suffix,
                request.url.query,
                raw_body,
                content_type,
                minted_token,
                settings,
                http_client,
            )
        except ConsoleConversationTagsProxyError as exc:
            logger.error("orchestrator /console/conversation-tags unreachable: %s", exc)
            return JSONResponse(
                status_code=502, content={"detail": "Upstream service unavailable"}
            )

    @app.api_route(
        "/console/conversation-tags",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def console_conversation_tags_base(
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-conversation-tags list/create entry
        (Conversation-Tags domain, MongoDB-elimination EPIC) — no path
        suffix. See ``_console_conversation_tags_proxy_impl`` for the
        shared shape."""
        return await _console_conversation_tags_proxy_impl(
            "", request, settings, http_client
        )

    @app.api_route(
        "/console/conversation-tags/{path:path}",
        methods=["GET", "POST", "DELETE"],
        response_model=None,
    )
    async def console_conversation_tags_proxy(
        path: str,
        request: Request,
        settings: Settings = Depends(get_settings),
        http_client: httpx.AsyncClient = Depends(get_http_client),
    ) -> StreamingResponse | JSONResponse:
        """The console-conversation-tags per-resource entry
        (Conversation-Tags domain, MongoDB-elimination EPIC) —
        ``{tag}``. See ``_console_conversation_tags_proxy_impl`` for the
        shared shape."""
        return await _console_conversation_tags_proxy_impl(
            path, request, settings, http_client
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
