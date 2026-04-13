"""Authentication middleware for sovereign-memory-server.

Two FastAPI dependencies live here, both backed by the same Keycloak
JWKS validation logic:

- ``require_scope`` (ADR-022, ADR-023): legacy entry point that returns
  the raw JWT payload as a dict. Used by existing routes today. Kept
  for backwards compatibility while routes migrate to ``require_user``.

- ``require_user`` (ADR-026 §15): the typed
  identity resolution path. Hot path is a Redis-backed cache lookup
  by ``sha256(token)``; cold path validates the JWT against the
  Keycloak JWKS endpoint and writes the resulting ``UserContext`` to
  the cache. When ``SOVEREIGN_AUTH_REQUIRED=false`` (the default
  during the multi-user migration window), returns a sentinel
  ``UserContext`` with full admin scopes so existing tests and dev
  workflows keep working unchanged.

The §15 refactor removed the local ``users`` table and the PAT model;
identity is now delegated entirely to Keycloak.
"""

import logging
import time
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from sovereign_memory.config import get_settings
from sovereign_memory.db.rls import set_current_user_id
from sovereign_memory.identity import (
    UserContext,
    get_token_cache,
    hash_token,
    is_admin_scope,
    sentinel_user_context,
)
from sovereign_memory.logging_config import log_call

logger = logging.getLogger(__name__)

# JWKS cache: {"keys": [...], "fetched_at": timestamp}
_jwks_cache: dict[str, Any] = {}
_JWKS_CACHE_TTL = 300  # 5 minutes

_bearer_scheme = HTTPBearer(auto_error=False)


_JWKS_FETCH_RETRIES = 3
_JWKS_FETCH_BACKOFF_BASE = 2.0  # seconds — exponential: 2, 4, 8


@log_call(logger=logger)
def _fetch_jwks_keys(jwks_url: str) -> list[Any]:
    """Fetch public keys from Keycloak JWKS endpoint.

    Retries with exponential backoff to handle the startup race where the
    memory-server is ready before Keycloak has finished initialising.
    """
    last_exc: Exception | None = None
    for attempt in range(_JWKS_FETCH_RETRIES + 1):
        try:
            response = httpx.get(jwks_url, timeout=10)
            response.raise_for_status()
            jwks = response.json()
            return list(jwks.get("keys", []))
        except (httpx.HTTPError, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < _JWKS_FETCH_RETRIES:
                delay = _JWKS_FETCH_BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "JWKS fetch attempt %d/%d failed (%s), retrying in %.0fs",
                    attempt + 1,
                    _JWKS_FETCH_RETRIES + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _get_jwks_keys(jwks_url: str) -> list[Any]:
    """Get JWKS keys with caching."""
    now = time.time()
    if (
        "keys" in _jwks_cache
        and now - _jwks_cache.get("fetched_at", 0) < _JWKS_CACHE_TTL
    ):
        return list(_jwks_cache["keys"])

    keys = _fetch_jwks_keys(jwks_url)
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys  # type: ignore[no-any-return]


def require_scope(required_scope: str):  # type: ignore[no-untyped-def]
    """FastAPI dependency that enforces JWT authentication and scope.

    Usage:
        @router.get("/protected")
        async def endpoint(payload: dict[str, Any] = Depends(require_scope("sovereign-ai:query"))):
            ...
    """

    async def _validate(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> dict[str, Any]:
        settings = get_settings()

        # Auth bypass when disabled
        if not settings.auth_enabled:
            return {}

        # No token provided
        if credentials is None:
            raise HTTPException(status_code=401, detail="Missing authentication token")

        token = credentials.credentials

        # Decode and validate JWT
        try:
            keys = _get_jwks_keys(settings.keycloak_jwks_url)
            payload = jwt.decode(
                token,
                keys,
                algorithms=["RS256"],
                audience=settings.jwt_audience,
                issuer=settings.keycloak_issuer,
            )
        except JWTError as e:
            logger.warning("JWT validation failed: %s", e)
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        # Check scope
        token_scopes = payload.get("scope", "").split()
        if required_scope not in token_scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Required scope: {required_scope}",
            )

        return dict(payload)

    return _validate


# ─────────────────────── Keycloak-delegated identity ────────────────────────
# ADR-026 §15. Identity comes from a Keycloak-issued
# JWT validated against the JWKS endpoint; the hot path is a Redis-backed
# token cache keyed on sha256(token).


_AGENT_MARKERS = ("opencode", "continue", "roocode", "curl", "httpx")


def _detect_agent_from_user_agent(ua: str) -> str:
    """Best-effort agent identification from the User-Agent header.

    Recorded in audit rows for forensics; not an authorization gate.
    Mirrors chat.py:_detect_source so audit values stay consistent
    across the proxy and the auth middleware.
    """
    ua_lower = (ua or "").lower()
    for marker in _AGENT_MARKERS:
        if marker in ua_lower:
            return marker
    return "opencode"  # default to the daily driver


@log_call(logger=logger)
async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> UserContext:
    """Resolve the request's identity to a ``UserContext``.

    Two modes, gated by ``SOVEREIGN_AUTH_REQUIRED``:

    **Bypass (default).** No JWT validation, no cache lookup. Returns
    a sentinel UserContext with admin scopes; agent_type is derived
    from the User-Agent header for observability. Used during the
    multi-user migration window so existing routes and tests keep
    working unchanged.

    **Required.** Validates a Keycloak-issued JWT:

    1. Bearer header present
    2. ``token_hash = sha256(raw_token)`` — used as the cache key
    3. **Hot path** — ``TokenCache.get(token_hash)`` returns the
       previously-validated UserContext if present and not expired
    4. **Cold path** — JWT signature validated against Keycloak JWKS
       (cached for 5 min via ``_jwks_cache``); claims become the new
       UserContext; cache it under the token hash for the
       configured TTL or the JWT exp, whichever is shorter
    5. Return the UserContext

    No agent_type defense layer here (deferred — see §15.6 of the
    design doc). The agent_type is recorded for audit but is not an
    authorization gate when using Keycloak JWTs.

    Raises ``401`` for any token validation failure.
    """
    settings = get_settings()

    # ── Bypass mode ───────────────────────────────────────────────────
    if not settings.auth_required:
        agent = _detect_agent_from_user_agent(request.headers.get("user-agent") or "")
        ctx = sentinel_user_context(agent_type=agent)
        # Phase 4: bind the request-scoped ContextVar so any downstream
        # DB query sees SENTINEL_SUBJECT via set_config. Postgres RLS
        # policies then evaluate against that.
        set_current_user_id(ctx.user_id)
        return ctx

    # ── Required mode ─────────────────────────────────────────────────
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    raw_token = credentials.credentials
    token_hash_value = hash_token(raw_token)

    # Hot path: cache hit returns sub-millisecond
    cache = get_token_cache()
    cached = cache.get(token_hash_value)
    if cached is not None:
        # Phase 4: bind the ContextVar on the hot path too so the
        # downstream DB session sees the correct user_id.
        set_current_user_id(cached.user_id)
        return cached

    # Cold path: validate JWT against Keycloak JWKS
    try:
        keys = _get_jwks_keys(settings.keycloak_jwks_url)
        payload = jwt.decode(
            raw_token,
            keys,
            algorithms=["RS256"],
            audience=settings.jwt_audience,
            issuer=settings.keycloak_issuer,
        )
    except JWTError as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    agent = _detect_agent_from_user_agent(request.headers.get("user-agent") or "")

    # Scopes from JWT scope claim — space-separated per OAuth2 standard
    scope_str = payload.get("scope", "")
    scopes: tuple[str, ...] = tuple(sorted(scope_str.split())) if scope_str else ()

    user_ctx = UserContext(
        user_id=sub,
        username=payload.get("preferred_username") or sub,
        agent_type=agent,
        scopes=scopes,
        token_id=payload.get("jti"),
        is_admin=is_admin_scope(scopes),
        extra={
            "email": payload.get("email", ""),
            "name": payload.get("name", ""),
        },
    )

    # Cache the result. TTL is bounded by min(jwt.exp - now, default_ttl)
    # so the cache never holds an entry beyond the JWT's own validity.
    jwt_exp = payload.get("exp")
    cache_ttl: int | None = None
    if jwt_exp is not None:
        remaining = int(jwt_exp - time.time())
        if remaining > 0:
            cache_ttl = min(remaining, settings.token_cache_ttl_seconds)
    cache.put(token_hash_value, user_ctx, ttl_seconds=cache_ttl)

    # Phase 4: bind the ContextVar so the SQLAlchemy after_begin listener
    # sees the Keycloak sub on the next DB query. Postgres RLS policies
    # then gate cross-user reads at the infrastructure layer.
    set_current_user_id(user_ctx.user_id)

    return user_ctx
