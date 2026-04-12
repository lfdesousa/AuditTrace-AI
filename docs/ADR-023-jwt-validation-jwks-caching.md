# ADR-023: JWT Validation + JWKS Caching Strategy

**Status:** Accepted
**Date:** 2026-04-10
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-022 (Keycloak realm)

## Context

Every protected endpoint must validate the Bearer JWT before processing the
request. The validation must check signature (RS256), issuer, audience, expiry,
and scope. The JWKS endpoint must not be called on every request.

## Decision

Implement JWT validation as a FastAPI dependency (`require_scope`) with JWKS
caching.

### `require_scope(scope: str)` — FastAPI Dependency

```python
@router.post("/v1/chat/completions")
async def chat(request: ChatRequest,
               _auth: dict = Depends(require_scope("sovereign-ai:query"))):
    ...
```

### Validation Steps

1. If `auth_enabled=False` → return empty dict (bypass)
2. Extract Bearer token from `Authorization` header
3. Fetch JWKS keys from `keycloak_jwks_url` (cached, 5-minute TTL)
4. Decode JWT with `python-jose`: RS256, verify audience + issuer + expiry
5. Check `scope` claim contains the required scope
6. Return decoded payload on success

### Error Responses

| Condition | HTTP Status | Detail |
|---|---|---|
| No token | 401 | Missing authentication token |
| Invalid/expired token | 401 | Invalid or expired token |
| Wrong audience/issuer | 401 | Invalid or expired token |
| Missing required scope | 403 | Required scope: `<scope>` |

### JWKS Caching

- In-memory cache with 5-minute TTL
- Cache key: JWKS URL
- On cache miss: HTTP GET to JWKS endpoint
- On JWT validation failure with cached keys: clear cache and retry once
- Thread-safe via module-level dict (GIL-protected for single worker)

### Scope Mapping

| Route | Method | Scope |
|---|---|---|
| `/v1/chat/completions` | POST | `sovereign-ai:query` |
| `/context` | POST | `sovereign-ai:context` |
| `/interactions` | GET/POST | `sovereign-ai:audit` |
| `/session/save` | POST | `sovereign-ai:query` |
| `/metrics` | GET | `sovereign-ai:admin` |
| `/health` | GET | *none* (probe) |

## Consequences

### Positive
- Single decorator pattern — consistent across all routes
- JWKS caching eliminates per-request latency to Keycloak
- Auth bypass for development (`auth_enabled=False`)
- 14 unit tests with test RSA keys — no real Keycloak needed

### Negative
- 5-minute cache TTL means key rotation takes up to 5 minutes to propagate
- `python-jose` is the JWT library (mature but less actively maintained than `PyJWT`)

### Neutral
- Cache TTL is configurable via `_JWKS_CACHE_TTL` constant
- Health endpoint stays unauthenticated for Docker/Kubernetes probes
