"""Identity primitives — Keycloak is the source of truth.

ADR-026 §15. Sovereign-memory-server holds NO
local users table; identity comes from Keycloak-issued JWTs validated
against the JWKS endpoint. The hot path is a Redis-backed token cache
keyed on ``sha256(token)``; the cold path validates the JWT against
JWKS and writes the resulting ``UserContext`` to the cache.

This module exports:

- ``UserContext`` — frozen dataclass passed explicitly to every memory
  service method as the first parameter (Phase 2 plumbing). Source-
  agnostic: it does not know whether it came from a JWT, the cache,
  or the bypass sentinel.
- ``hash_token`` — sha256 hex helper. Used as the cache key so the
  raw token never reaches Redis, logs, or memory dumps.
- ``is_admin_scope`` — checks whether a scope tuple grants admin.
- ``is_privileged_scope`` — checks whether a scope tuple grants admin
  OR a granular ``memory:corpus:*:write`` scope (ADR-062 §4). Used for
  shadow-client blast-radius ranking, deliberately distinct from
  ``is_admin_scope`` (app-layer authz).
- ``CORPUS_WRITE_SCOPE_GLOB`` — the single-source-of-truth glob string
  for a corpus-write scope, mirrored verbatim in
  ``scripts/post-deploy-verify.sh`` Check 11 and cross-checked by
  ``tests/test_chart_drift_guards.py``.
- ``sentinel_user_context`` — bypass-mode UserContext for the
  ``AUDITTRACE_AUTH_REQUIRED=false`` migration window.
- ``TokenCache`` — Redis-backed sha256→UserContext store with TTL.
  Phase 8+ may swap the implementation for a multi-instance shared
  cluster; the interface stays the same.
- ``get_token_cache`` / ``reset_token_cache`` — module-level singleton
  accessors for the production cache instance.

The cache is intentionally per-deployment Redis (a dedicated
``sovereign-redis`` container in ``docker-compose.yml``). Sharing
Redis with Langfuse or other systems is not supported because the
key namespace ``audittrace:token:`` is unprotected from collisions
with other users of the same Redis instance.
"""

from __future__ import annotations

import contextvars
import fnmatch
import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)


# ──────────────────── Request-credential registry (SEC-JWT-IN-LANGFUSE-SPAN) ──
#
# D3 — the equality pin. The bearer token has exactly one process entry
# point: the OAuth2 scheme's ``__call__`` (``audittrace.auth`` — see
# ``_RegisteringOAuth2Scheme``). It registers the raw value here, in a
# request-scoped ContextVar, BEFORE returning it to any ``Depends(...)``
# consumer — so the value is visible to every ``@log_call`` target that
# might observe it in the same task, including ``require_user`` itself.
# ``logging_config.scrub_text`` reads this registry by VALUE EQUALITY
# (longest-first, so a shorter registered value can never leave a
# dangling suffix of a longer one unredacted) and is the primary
# redaction mechanism; the shape net (JWT/Bearer/Basic regexes) is the
# secondary, enumeration-class layer for whatever bypasses this path.
#
# A ContextVar, not a module global: concurrent requests (threadpool +
# asyncio tasks) must never see each other's secrets, and a value must
# never leak across requests. ``contextvars.copy_context()`` semantics
# mean a child task spawned from a context that already registered a
# secret inherits a COPY of the registry — mutating it there does not
# leak back to the parent, which is the same precedent as
# ``db.rls.set_current_user_id``.
#
# SCRUB-EVENT-LOOP-STALL (v1.29.4, R1/S4 — ratified ADDENDUM A1 §9) — the
# registry holds ONE ContextVar of ``(value, window_index)`` pairs, not two
# separate ContextVars. Two independent ContextVars could diverge (a reset
# that clears one but not the other, or a future direct writer of just the
# values) and ``request_secret_index`` would silently fall back to an
# uncached rebuild on every call — safe (redaction still holds) but the
# v1.29.3 event-loop stall this WU fixes would come back on any path the
# fallback is reached from. One ContextVar makes that divergence
# unrepresentable: ``reset_request_secrets`` clears both facts in the same
# ``.set(())`` call, by construction.
_REQUEST_SECRETS: contextvars.ContextVar[
    tuple[tuple[str, Mapping[str, tuple[int, ...]]], ...]
] = contextvars.ContextVar("audittrace_request_secrets", default=())

# Never register a value shorter than this — avoids an accidental scrub of
# a common short string (a one-word query, a short id) that happens to
# equal some unrelated registered value. The shape net (D4) covers short
# or malformed credentials this floor excludes.
_MIN_REGISTERED_SECRET_LEN = 16

# SCRUB-EVENT-LOOP-STALL (R1) — moved here from ``logging_config`` (pure
# string code, no imports) together with ``build_secret_window_index``
# below. Re-exported from ``logging_config`` under its original name
# (``_FRAGMENT_WINDOW_LEN``) so no existing import site moves.
_FRAGMENT_WINDOW_LEN = 16


def build_secret_window_index(secret: str) -> Mapping[str, tuple[int, ...]]:
    """Map every ``_FRAGMENT_WINDOW_LEN``-char window of ``secret`` to the
    tuple of start offsets within ``secret`` where that window occurs
    (handles a secret with internal repeats).

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R1) — moved here, unchanged in
    behaviour, from ``logging_config._secret_window_index`` (still
    re-exported there under that name). The ratified fix for the
    v1.29.3 event-loop stall is calling this function ONCE per
    registered secret, at registration (``register_request_secret``
    below), instead of once per scrubbed string leaf
    (``logging_config._scrub_secret_fragments`` /
    ``logging_config.secret_leaks_into`` now read the pre-built result
    via ``request_secret_index`` below, O(1), rather than rebuilding it
    on every call).
    """
    index: dict[str, list[int]] = {}
    for i in range(0, len(secret) - _FRAGMENT_WINDOW_LEN + 1):
        index.setdefault(secret[i : i + _FRAGMENT_WINDOW_LEN], []).append(i)
    return {k: tuple(v) for k, v in index.items()}


def register_request_secret(value: str) -> None:
    """Register ``value`` as a secret to redact for the rest of this request.

    Idempotent (registering the same value twice is a no-op beyond the
    dedup) and additive — multiple secrets (e.g. a bearer token AND a
    downstream-minted token) can be registered in the same request. Values
    shorter than ``_MIN_REGISTERED_SECRET_LEN`` are silently ignored (D3).

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R1) — builds the 16-char window index
    for ``value`` exactly ONCE, here, and stores it alongside the value.
    Every later reader (``request_secret_index`` below) gets that one
    object back in O(1) instead of rebuilding it on every scrubbed string
    leaf — the per-leaf rebuild was the v1.29.3 event-loop-stall defect.
    """
    if not value or len(value) < _MIN_REGISTERED_SECRET_LEN:
        return
    current = _REQUEST_SECRETS.get()
    if any(existing == value for existing, _ in current):
        return
    index = build_secret_window_index(value)
    _REQUEST_SECRETS.set((*current, (value, index)))


def request_secrets() -> tuple[str, ...]:
    """Return every secret registered so far in the current request context.

    Empty tuple when nothing has been registered (unauthenticated routes,
    bypass mode, unit tests without a request scope) — callers must treat
    that as "nothing to scrub by equality", not an error.
    """
    return tuple(value for value, _ in _REQUEST_SECRETS.get())


def request_secret_index(secret: str) -> Mapping[str, tuple[int, ...]]:
    """Return the registered window index for ``secret`` in O(1).

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R1) — the production read path:
    ``secret`` was registered via ``register_request_secret`` above, so
    its index was already built once, at registration, and is returned
    here by identity (not rebuilt). For a ``secret`` that is NOT
    registered (the redaction-net's registered-value leg in
    ``tests/conftest.py`` calls ``secret_leaks_into`` with an explicit
    secret that may never have gone through ``register_request_secret``)
    this builds one uncached — correct, just not memoised, and the
    registry is never widened to cache it (no module-level cache: a
    ``lru_cache`` here would retain a secret's windows for the process
    lifetime, long past the request that registered it — rejected by the
    ratified spec, G-P6).
    """
    for value, index in _REQUEST_SECRETS.get():
        if value == secret:
            return index
    return build_secret_window_index(secret)


def reset_request_secrets() -> None:
    """Clear the registry. Test-only — production requests get a fresh
    ContextVar value for free because each request runs in its own task."""
    _REQUEST_SECRETS.set(())


# ─────────────────────────── Sentinel for bypass mode ────────────────────────
# A fixed Keycloak-shaped subject claim used by ``sentinel_user_context``.
# Phase 5 will remove the bypass mode entirely once cross-user isolation
# tests land; until then, this is the identity used for backwards-compat
# tests and dev workflows that pre-date multi-user.

SENTINEL_SUBJECT = "00000000-0000-0000-0000-000000000001"
SENTINEL_USERNAME = "default"


# ──────────────────────────────── UserContext ────────────────────────────────


@dataclass(frozen=True)
class UserContext:
    """Resolved identity for a single request, source-agnostic.

    Constructed by the auth middleware after JWT validation (cold path)
    or by the cache lookup (hot path). The frozen dataclass guarantees
    that no code downstream can mutate the identity once it has been
    resolved.
    """

    user_id: str
    """Keycloak ``sub`` claim — opaque UUID identifying the human."""

    username: str
    """Keycloak ``preferred_username`` claim, falling back to ``sub``."""

    agent_type: str
    """Best-effort detection from the request's User-Agent header.
    Recorded in audit rows for forensics; not an authorization gate."""

    scopes: tuple[str, ...]
    """OAuth2 scopes from the JWT ``scope`` claim, split on whitespace."""

    token_id: str | None = None
    """Keycloak ``jti`` claim — used to correlate audit rows back to a
    specific token issuance event."""

    is_admin: bool = False
    """Derived from ``scopes`` via ``is_admin_scope``."""

    extra: dict[str, str] = field(default_factory=dict)
    """Free-form extra claims (email, name, etc.) carried for display
    purposes only — never load-bearing for authorization."""


# ─────────────────────────────── Helpers ────────────────────────────────────


def hash_token(token: str) -> str:
    """Compute the hex sha256 of a token.

    Used as the cache key so the raw bearer token never reaches Redis,
    logs, or memory dumps. ``hash_token`` is deterministic — the same
    token always hashes to the same key.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_admin_scope(scopes: Iterable[str]) -> bool:
    """True iff any scope grants administrative access.

    Recognises:

    * ``audittrace:admin`` — the canonical operator scope per
      ``ALL_SCOPES`` in ``audittrace.auth`` and the realm definition
      in ``keycloak/realm-audittrace.json``. Carrying this scope
      bypasses the per-user ``where={"user_id": ...}`` filter in
      ``ChromaSemanticService.search`` so admin-ingested shared
      content (ADRs, skills, ai_research_papers PDFs) is reachable
      regardless of who's querying.
    * ``memory:admin`` — historical/legacy alias from the pre-rename
      memory-server era; preserved for back-compat with tokens still
      in flight at upgrade time.
    * ``admin:*`` — future-proofing for any new admin-namespaced
      scope (e.g. ``admin:billing`` if we ever add one) without
      requiring this function to grow with each addition.

    Used to set ``UserContext.is_admin`` for downstream gates and
    the Phase 4 RLS bypass policy.
    """
    return any(
        s in ("audittrace:admin", "memory:admin") or s.startswith("admin:")
        for s in scopes
    )


# The single source of truth for "what is a corpus-write scope" (#371,
# ADR-062 §4: "memory:corpus:<collection>:write" per recall collection,
# {decisions, skills, semantic}). This EXACT string is mirrored verbatim in
# the shell `case` pattern in `scripts/post-deploy-verify.sh` Check 11
# (shell glob syntax happens to be `fnmatch`-compatible), and
# `tests/test_chart_drift_guards.py::TestPrivilegedCorpusScopeSingleSourceOfTruth`
# reads both files and asserts they still agree — so the app classifier and
# the shell guard cannot silently drift apart the way #370/#371 drifted.
CORPUS_WRITE_SCOPE_GLOB = "memory:corpus:*:write"


def is_privileged_scope(scopes: Iterable[str]) -> bool:
    """True iff any scope is admin OR a granular corpus-write scope.

    Recognises everything ``is_admin_scope`` does, PLUS the granular
    ``memory:corpus:<collection>:write`` scopes named in ADR-062 §4
    (``docs/ADR-062-five-layer-memory-model.md:266-269``): "the scope-drift
    guard must learn the granular corpus scopes so an undeclared holder is
    flagged." Corpus write is operator/curator-tier access to the shared
    corpus (Layer 5) — an UNDECLARED client holding one is exactly the
    shadow-writer threat #371 exists to catch, so it is ranked ``!!``
    (privileged blast radius) by ``scripts/post-deploy-verify.sh`` Check 11.

    Deliberately does **not** fold plain ``memory:<layer>:write`` (e.g.
    ``memory:episodic:write``) in here — that is the ordinary per-user
    writer scope every legitimate client (webui, opencode) holds, and
    flagging it would make the shadow-client guard cry wolf on every normal
    deployment. Only the granular ``memory:corpus:*:write`` form counts.
    Read scopes (``memory:corpus:<collection>:read``) are likewise never
    privileged — read access to the shared corpus is not the elevation this
    guards against.

    Kept SEPARATE from ``is_admin_scope`` on purpose: this function drives
    shadow-client blast-radius RANKING only. Folding a corpus-write scope
    into ``is_admin_scope`` would silently grant a Layer-5 writer the RLS
    bypass and every other admin-gated app capability via
    ``UserContext.is_admin`` — ADR-062 §4 calls corpus write
    "operator/curator-tier", not app-admin-tier, so ``is_admin_scope``
    (and therefore ``UserContext.is_admin``) must stay untouched by this
    change.
    """
    return is_admin_scope(scopes) or any(
        fnmatch.fnmatchcase(s, CORPUS_WRITE_SCOPE_GLOB) for s in scopes
    )


def sentinel_user_context(agent_type: str = "opencode") -> UserContext:
    """Build the UserContext returned by ``require_user`` in bypass mode.

    Has full admin scopes so existing tests and dev workflows that
    pre-date the multi-user feature continue to work end-to-end without
    per-call configuration. Production deployments must flip
    ``AUDITTRACE_AUTH_REQUIRED`` to ``true`` to take this path out of
    reach.
    """
    return UserContext(
        user_id=SENTINEL_SUBJECT,
        username=SENTINEL_USERNAME,
        agent_type=agent_type,
        scopes=("memory:admin", "memory:read", "session:read-own"),
        token_id=None,
        is_admin=True,
    )


# ─────────────────────────────── TokenCache ─────────────────────────────────


class TokenCache:
    """Redis-backed sha256(token) → UserContext cache with TTL eviction.

    Cache keys are namespaced under ``audittrace:token:<hash>`` so that
    multiple consumers of the same Redis instance (none today, but
    designing for it) cannot collide.

    The raw bearer token is never stored — only its sha256 hash. Anyone
    who breaches the Redis instance gets a list of valid (hash → claims)
    bindings, which lets them verify an existing token but not forge
    new ones.

    Thread-safe via Redis itself; no in-process locks needed. FastAPI
    runs request handlers across a thread pool that all share a single
    ``redis.Redis[str]`` client.

    Resilience: ``get`` returns ``None`` on any Redis[str] error (treated as
    cache miss — the middleware falls through to JWT validation).
    ``put`` swallows Redis errors with a warning so cache failures
    never break requests.
    """

    KEY_PREFIX = "audittrace:token:"

    def __init__(self, redis_client: Redis[str], default_ttl_seconds: int = 300):
        self._redis = redis_client
        self._default_ttl = default_ttl_seconds

    def _key(self, token_hash: str) -> str:
        return f"{self.KEY_PREFIX}{token_hash}"

    def get(self, token_hash: str) -> UserContext | None:
        """Return the cached ``UserContext`` for this hash, or ``None``.

        ``None`` is returned for: missing key, expired entry (Redis[str]
        evicted), unparseable payload, or any Redis connectivity error.
        Treat ``None`` as "validate the JWT and re-cache".
        """
        try:
            raw = self._redis.get(self._key(token_hash))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(str(raw))
            return UserContext(
                user_id=data["user_id"],
                username=data["username"],
                agent_type=data["agent_type"],
                scopes=tuple(data["scopes"]),
                token_id=data.get("token_id"),
                is_admin=data.get("is_admin", False),
                extra=data.get("extra", {}),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("TokenCache.get malformed payload: %s", exc)
            return None

    def put(
        self,
        token_hash: str,
        ctx: UserContext,
        ttl_seconds: int | None = None,
    ) -> None:
        """Write a ``UserContext`` under the token hash with TTL eviction.

        ``ttl_seconds`` overrides the default. Callers typically pass
        ``min(jwt.exp - now, default)`` so the cache never holds an
        entry beyond the underlying JWT's own expiry.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            return  # nothing to cache — avoid Redis[str] SETEX with TTL=0
        payload = {
            "user_id": ctx.user_id,
            "username": ctx.username,
            "agent_type": ctx.agent_type,
            "scopes": list(ctx.scopes),
            "token_id": ctx.token_id,
            "is_admin": ctx.is_admin,
            "extra": ctx.extra,
        }
        try:
            self._redis.setex(self._key(token_hash), ttl, json.dumps(payload))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.put failed: %s", exc)

    def invalidate(self, token_hash: str) -> None:
        """Remove a token's cache entry. Used on logout/revocation."""
        try:
            self._redis.delete(self._key(token_hash))
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("TokenCache.invalidate failed: %s", exc)

    def clear(self) -> None:
        """Drop every cache entry under the audittrace:token: prefix.

        Uses ``SCAN`` rather than ``KEYS *`` so the operation is
        non-blocking on a busy Redis. Used by tests and admin tools.
        """
        cursor = 0
        while True:
            cursor, keys = self._redis.scan(
                cursor=cursor, match=f"{self.KEY_PREFIX}*", count=100
            )
            if keys:
                self._redis.delete(*keys)
            if cursor == 0:
                break

    def size(self) -> int:
        """Approximate count of cached entries (via SCAN, not KEYS).

        Used for observability — exposed via the /metrics endpoint in
        a future phase. Returns 0 on Redis error.
        """
        count = 0
        cursor = 0
        try:
            while True:
                cursor, keys = self._redis.scan(
                    cursor=cursor, match=f"{self.KEY_PREFIX}*", count=100
                )
                count += len(keys)
                if cursor == 0:
                    break
            return count
        except Exception:  # pragma: no cover - resilience path
            return 0


# ──────────────── Module-level singleton accessors ──────────────────────────
# Lazy: the Redis connection is created on first access so module imports
# stay cheap and tests can monkey-patch ``_token_cache`` with a fakeredis-
# backed instance without ever touching production settings.

_token_cache: TokenCache | None = None
_redis_client: Redis[str] | None = None


def get_token_cache() -> TokenCache:
    """Return the process-wide TokenCache, creating it on first call.

    Lazy construction so importing this module has zero side effects;
    tests can override ``_token_cache`` before any code path reaches
    here.
    """
    global _token_cache, _redis_client
    if _token_cache is not None:
        return _token_cache

    from redis import Redis

    from audittrace.config import get_settings

    settings = get_settings()
    _redis_client = Redis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )
    _token_cache = TokenCache(
        _redis_client,
        default_ttl_seconds=settings.token_cache_ttl_seconds,
    )
    return _token_cache


def reset_token_cache() -> None:
    """Reset the singleton. Used by tests after monkey-patching settings."""
    global _token_cache, _redis_client
    _token_cache = None
    _redis_client = None
