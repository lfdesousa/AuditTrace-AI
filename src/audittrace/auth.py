"""Authentication middleware for audittrace-server.

Two FastAPI dependencies live here, both backed by the same Keycloak
JWKS validation logic:

- ``require_scope`` (ADR-022, ADR-023): legacy entry point that returns
  the raw JWT payload as a dict. Used by existing routes today. Kept
  for backwards compatibility while routes migrate to ``require_user``.

- ``require_user`` (ADR-026 §15): the typed
  identity resolution path. Hot path is a Redis-backed cache lookup
  by ``sha256(token)``; cold path validates the JWT against the
  Keycloak JWKS endpoint and writes the resulting ``UserContext`` to
  the cache. When ``AUDITTRACE_AUTH_REQUIRED=false`` (the default
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
from fastapi.security import (
    OAuth2AuthorizationCodeBearer,
    SecurityScopes,
)
from jose import JWTError, jwt

from audittrace.config import get_settings
from audittrace.db.rls import set_current_user_id
from audittrace.identity import (
    UserContext,
    get_token_cache,
    hash_token,
    is_admin_scope,
    sentinel_user_context,
)
from audittrace.logging_config import log_call

logger = logging.getLogger(__name__)

# JWKS cache: {"keys": [...], "fetched_at": timestamp}.
# TTL comes from ``Settings.jwks_cache_ttl_seconds`` (env
# ``AUDITTRACE_JWKS_CACHE_TTL_SECONDS``) — read on each cache check
# rather than baked in, so the operator can tune it without a redeploy.
_jwks_cache: dict[str, Any] = {}


# ── OAuth2 scope catalogue ─────────────────────────────────────────────────
#
# All scopes the API enforces, mapped to a one-line description. This
# dict is the single source of truth surfaced through the OpenAPI
# security scheme — Swagger UI shows the description next to each
# scope checkbox; the OpenAPI snapshot test (tests/test_openapi_drift.py)
# guards against drift between the catalogue and the realm definition.
#
# Adding a new scope: bump the realm in keycloak/realm-audittrace.json
# AND add it here; the Keycloak Job (configmap-memory-scopes-script)
# provisions the binding onto the user-facing clients.
ALL_SCOPES: dict[str, str] = {
    "audittrace:query": "Issue chat completions and inference queries",
    "audittrace:context": "Read context-builder output for a query",
    "audittrace:audit": "Read interactions, sessions, and tool-call audit rows",
    "audittrace:admin": (
        "Operator-grade administration: /memory/upload, /memory/index, "
        "hard-delete, configuration introspection. Optional scope on "
        "user-facing clients; never granted by default."
    ),
    "audittrace:index": "Trigger semantic-store reindex (legacy dev client)",
    "memory:episodic:read": "Read episodic-layer (ADR-style) documents",
    "memory:episodic:write": "Create/update/delete episodic-layer documents",
    "memory:procedural:read": "Read procedural-layer (SKILL-style) documents",
    "memory:procedural:write": "Create/update/delete procedural-layer documents",
    "memory:semantic:read": "Read semantic-layer (vector) documents",
    "memory:semantic:write": "Create/update/delete semantic-layer documents",
    "memory:conversational:read-own": (
        "Read your own past conversations from the conversational layer"
    ),
    "memory:upload:write": (
        "Upload bytes (PDFs, etc.) into the ingestion content-control "
        "pipeline (ADR-048). Distinct from memory:episodic:write because "
        "uploads land in a quarantine prefix the memory-server cannot "
        "read; promotion to episodic/papers/ happens only after a "
        "scanner verdict."
    ),
    "audittrace:scan:retrigger": (
        "Force a re-scan of an object in the ingestion content-control "
        "pipeline (ADR-048 operator surface). Admin-grade; never "
        "granted by default."
    ),
}


# ── OAuth2 security scheme (the actual scheme validate_jwt depends on) ─
#
# `OAuth2AuthorizationCodeBearer` is the OpenAPI-canonical security
# scheme: Swagger UI renders scope checkboxes per route, the spec
# emits ``security: [{audittrace-oauth2: [scope, ...]}]`` per
# operation, and clients that read the spec auto-discover the
# Keycloak authorization + token URLs.
#
# Authorization + token URLs point at the configured Keycloak realm.
# Browser-flow callers (audittrace-webui client, ADR-042) use these
# URLs directly. OpenCode / device-flow callers (ADR-032) go through
# the Device Authorization Grant instead, but their tokens carry the
# same bearer shape — `validate_jwt` accepts either flow because it
# only inspects the JWT, not the issuance path.
#
# Module-level instance so `Depends(oauth2_scheme)` works without a
# wrapper function. ``get_settings()`` is cached and always
# resolvable at import time in normal env-var setups; tests that
# override Settings can patch ``audittrace.auth.oauth2_scheme`` if
# they need a different issuer URL in the spec.
def _build_oauth2_scheme() -> OAuth2AuthorizationCodeBearer:
    settings = get_settings()
    base = settings.keycloak_issuer
    return OAuth2AuthorizationCodeBearer(
        authorizationUrl=f"{base}/protocol/openid-connect/auth",
        tokenUrl=f"{base}/protocol/openid-connect/token",
        scopes=ALL_SCOPES,
        auto_error=False,
        scheme_name="audittrace-oauth2",
    )


oauth2_scheme = _build_oauth2_scheme()


def _decode_jwt_with_allowed_issuers(
    token: str,
    keys: Any,
    audience: str,
    primary_issuer: str,
    extra_issuers: list[str],
) -> dict[str, Any]:
    """Decode + validate a JWT accepting one of several ``iss`` values.

    python-jose's ``jwt.decode`` only accepts a single ``issuer``
    string. ADR-032 introduces Device-Flow tokens that ride the
    Traefik-exposed URL and therefore carry a different ``iss`` even
    though they are signed by the same Keycloak. Decoding without
    enforcing ``issuer`` and then cross-checking against the union
    set keeps both flows working from one validation path.
    """
    payload = jwt.decode(
        token,
        keys,
        algorithms=["RS256"],
        audience=audience,
        # issuer check handled below against the union set
    )
    token_iss = payload.get("iss")
    allowed = {primary_issuer, *(extra_issuers or [])}
    if token_iss not in allowed:
        raise JWTError(
            f"Invalid issuer {token_iss!r}; expected one of {sorted(allowed)!r}"
        )
    # python-jose's jwt.decode returns Any; the dict cast is for mypy.
    assert isinstance(payload, dict), "jwt.decode returned non-dict payload"
    return payload


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
        and now - _jwks_cache.get("fetched_at", 0)
        < get_settings().jwks_cache_ttl_seconds
    ):
        return list(_jwks_cache["keys"])

    keys = _fetch_jwks_keys(jwks_url)
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys  # type: ignore[no-any-return]


async def validate_jwt(
    security_scopes: SecurityScopes,
    token: str | None = Depends(oauth2_scheme),
) -> dict[str, Any]:
    """FastAPI Security dependency: JWT auth + per-route scope check.

    Usage in route handlers (this is the **OpenAPI-surfacing** form):

        @router.post("/something")
        def handler(
            _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:query"]),
        ):
            ...

    The ``security_scopes`` argument is filled by FastAPI from the
    ``scopes=[...]`` list on each ``Security()`` call. Each declared
    scope must be present in the JWT's ``scope`` claim or the request
    is rejected with 403. Per ADR-022 / ADR-023; OpenAPI scheme is
    declared via the module-level ``oauth2_scheme`` so generated specs
    show the OAuth2 authorization URL + per-operation required scopes
    (Swagger UI surfaces them as the lock-icon's checkbox list).

    The legacy ``Depends(require_scope("X"))`` form continues to work
    via the wrapper below for routes that haven't migrated yet.
    """
    settings = get_settings()

    # Auth bypass when disabled — unit-test default.
    if not settings.auth_enabled:
        return {}

    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    try:
        keys = _get_jwks_keys(settings.keycloak_jwks_url)
        payload = _decode_jwt_with_allowed_issuers(
            token,
            keys,
            audience=settings.jwt_audience,
            primary_issuer=settings.keycloak_issuer,
            extra_issuers=settings.keycloak_issuer_extras,
        )
    except JWTError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Multi-scope check: every scope declared on the route must be
    # present in the token. ``SecurityScopes.scopes`` is the ordered
    # list FastAPI extracted from the route's ``Security(..., scopes=[...])``.
    token_scopes = set(payload.get("scope", "").split())
    missing = [s for s in security_scopes.scopes if s not in token_scopes]
    if missing:
        raise HTTPException(
            status_code=403,
            detail=f"Required scope: {missing[0]}"
            if len(missing) == 1
            else f"Required scopes: {missing!r}",
        )

    return dict(payload)


def require_scope(required_scope: str):  # type: ignore[no-untyped-def]
    """Backward-compatibility shim for legacy ``Depends(require_scope("X"))``.

    Returns a closure that calls :func:`validate_jwt` with a single
    declared scope. This preserves behaviour for routes and tests that
    haven't migrated to the explicit ``Security(validate_jwt, scopes=[...])``
    form. New routes should use ``Security`` directly so the required
    scopes appear in the generated OpenAPI spec — the closure-based
    factory is opaque to the schema generator.

    Usage (legacy):
        @router.get("/protected")
        async def endpoint(
            payload: dict[str, Any] = Depends(require_scope("audittrace:query")),
        ):
            ...
    """

    async def _validate(
        token: str | None = Depends(oauth2_scheme),
    ) -> dict[str, Any]:
        # Build a SecurityScopes manually since this path doesn't go
        # through FastAPI's Security() machinery.
        scopes_obj = SecurityScopes(scopes=[required_scope])
        return await validate_jwt(scopes_obj, token)

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
    token: str | None = Depends(oauth2_scheme),
) -> UserContext:
    """Resolve the request's identity to a ``UserContext``.

    Two modes, gated by ``AUDITTRACE_AUTH_REQUIRED``:

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
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    raw_token = token
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
        payload = _decode_jwt_with_allowed_issuers(
            raw_token,
            keys,
            audience=settings.jwt_audience,
            primary_issuer=settings.keycloak_issuer,
            extra_issuers=settings.keycloak_issuer_extras,
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
