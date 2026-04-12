# ADR-026: Multi-user identity, scopes, and cross-user isolation

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-022 (Keycloak realm), ADR-023 (JWT validation + JWKS caching),
ADR-024 (chat proxy pass-through + Langfuse trace decoupling),
ADR-025 (memory-as-tools)
**Seed:** `docs/architecture/BRAINSTORM-memory-as-tools.md` §13
(historical exploration of the liability scenario that motivated the work)

> **Promotion history.** This ADR started life as
> `docs/ADR-026-multi-user-identity.md` on 2026-04-11 morning.
> It went through a major in-session revision — §4's PAT model was
> replaced by §15's Keycloak-delegated model later the same day — and
> shipped Phases 2, 3, 4, 5a, 5b, 7 end-to-end before being promoted to
> `docs/ADR-026-multi-user-identity.md` on 2026-04-11 evening with
> Accepted status. The body retains the original §1-§13 design-space
> exploration, the §14 transition log, the §15 Keycloak rewrite, and
> the §16 status snapshot so a reader can follow the journey that
> produced the accepted decision.
>
> **Read §15 first** for the canonical Keycloak-delegated design,
> **then §16** for the end-of-2026-04-11 "what shipped" snapshot with
> commit SHAs, **then come back to §§1-13** for the pre-revision
> exploration that survives (UserContext shape, scope-driven tool
> availability, cross-user isolation requirements, sequencing relative
> to memory-as-tools).
>
> **⚠ MAJOR IN-SESSION REVISION 2026-04-11:** §4 (PAT model) is
> superseded by §15 (Keycloak delegation). The PAT model was a
> reasonable bootstrap but the wrong long-term seam for an enterprise
> architecture. Identity is now delegated entirely to Keycloak;
> sovereign-memory-server holds no `users` table. The superseded
> sections are retained for the historical record.

This is the actionable follow-on to `BRAINSTORM-memory-as-tools.md` §13.5.
The brainstorm explored *whether* multi-user mattered. This document picks
*how* it gets implemented. Decisions taken below are explicit and
labelled. Open questions are still flagged.

---

## 1. Decisions accepted

The following are no longer up for debate. They are the load-bearing
constraints everything else hangs from.

### D1. Identity is mandatory across the entire request path, from day one

Every chat request, every memory tool call, every persisted row must
carry a verifiable user identity. There is no "anonymous" mode in
production. The single-user assumption that hides in today's code is
treated as a bug, not a feature.

### D2. Scopes drive tool availability

The set of memory tools the LLM sees in any given request is filtered
by the requesting user's scopes. A user without `memory:write-skills`
literally does not see `save_skill` in the `tools` array sent to the
model. The tool registry is the authorization boundary.

### D3. Cross-user isolation is enforced by design, not by convention

User A can never see User B's data. This is enforced at five
independent layers (§7) so that a bug in any one of them does not
silently leak. PostgreSQL Row-Level Security is the load-bearing
guarantee.

### D4. First client is OpenCode, with PAT-based auth (Phase 1)

> **SUPERSEDED — see §15.** The first multi-user iteration ships
> against OpenCode using **Keycloak-issued JWTs** acquired via OAuth2
> device flow, with an in-memory token cache for the hot path. The PAT
> model from §4 is dropped because it required maintaining a local
> users table — which violates the architectural seam (Keycloak owns
> identity, we own audit). PATs may return as a Keycloak service
> account credential mechanism in Phase 8+ if required for headless
> automation.

The first multi-user iteration ships against OpenCode using personal
access tokens. OAuth2 device flow with Keycloak is the explicit
follow-up phase, not a "maybe". Both end up resolved to the same
internal `User` record, so the change is a flag flip not a re-design.

### D5. The interfaces are designed today as if implementation were complete

Even where the first iteration uses sentinel values (single user,
single agent type), the *interfaces* assume the full multi-user
shape. Every memory service method takes a `UserContext` parameter.
Every persisted row has `user_id`. Every tool call carries the
requesting identity. This is the non-negotiable lesson from the
brainstorm: cheap to do early, expensive to retrofit later.

---

## 2. Out of scope for this design

- **Async persistence** — separate design (brainstorm §12, future ADR-027).
- **Memory writes via tools** — agency/audit conversation, separate.
- **OAuth2 device flow implementation** — Phase 7, deferred but
  interface-compatible.
- **VSCode native auth provider** — later phase, same wire shape.
- **Continue and Roo Code clients** — same wire shape, config-only
  difference, deferred to a follow-up phase.
- **Cryptographic audit anchoring** — out of scope for this design but
  the schema does not preclude it.

---

## 3. The identity flow, end-to-end

```
┌─────────────────┐
│   Luis (human)  │
└────────┬────────┘
         │ types prompt
         ▼
┌─────────────────┐
│    OpenCode     │  config: { api_key = "smk_xxxxxxx..." }
│      (CLI)      │  one PAT per (human, agent) pair
└────────┬────────┘
         │ POST /v1/chat/completions
         │ Authorization: Bearer smk_xxxxxxx...
         ▼
┌──────────────────────────────────────────────────────────┐
│              sovereign-memory-server                      │
│                                                           │
│  ┌──────────────────────────────────────┐                │
│  │ auth middleware (require_user)        │                │
│  │  - look up PAT in pat_tokens table    │                │
│  │  - resolve to User record             │                │
│  │  - load user.scopes from roles        │                │
│  │  - construct UserContext              │                │
│  │  - SET LOCAL app.current_user_id = .. │ ← for RLS      │
│  └──────────────────┬───────────────────┘                │
│                     │                                     │
│                     ▼                                     │
│  ┌──────────────────────────────────────┐                │
│  │ chat handler                         │                │
│  │  - build available_tools from        │                │
│  │    MEMORY_TOOL_REGISTRY filtered by  │                │
│  │    user.scopes                       │                │
│  │  - forward to llama.cpp with         │                │
│  │    tools = [opencode tools, ...]     │                │
│  └──────────────────┬───────────────────┘                │
│                     │                                     │
│                     ▼                                     │
│  ┌──────────────────────────────────────┐                │
│  │ memory tool execution                │                │
│  │  - tool dispatcher checks scope      │                │
│  │  - service method receives            │                │
│  │    UserContext explicitly             │                │
│  │  - RLS enforces row visibility       │                │
│  └──────────────────┬───────────────────┘                │
│                     │                                     │
│                     ▼                                     │
│  ┌──────────────────────────────────────┐                │
│  │ persistence (async — see §12 brain)  │                │
│  │  - interactions row carries user_id  │                │
│  │  - Langfuse trace carries user.id    │                │
│  └──────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────┘
```

The same flow applies to Continue / Roo Code / future agents — only the
"OpenCode" box on the left changes. Everything to the right of the
auth middleware is agent-agnostic.

---

## 4. Identity propagation: PAT model

### 4.1 Schema

```sql
-- New table: users
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(255) NOT NULL UNIQUE,
    email           VARCHAR(255) NOT NULL UNIQUE,
    display_name    VARCHAR(255),
    keycloak_sub    VARCHAR(255) UNIQUE,  -- nullable until Phase 7
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- New table: user roles (Keycloak roles cached locally)
CREATE TABLE user_roles (
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            VARCHAR(255) NOT NULL,
    PRIMARY KEY (user_id, role)
);

-- New table: PAT tokens
CREATE TABLE pat_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,            -- "opencode-workstation", "continue-laptop"
    agent_type      VARCHAR(64) NOT NULL,             -- "opencode" | "continue" | "roocode" | ...
    token_hash      VARCHAR(128) NOT NULL UNIQUE,     -- sha256 of the raw token, never the raw token
    prefix          VARCHAR(16) NOT NULL,             -- first 8 chars of token for human display
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMP,
    expires_at      TIMESTAMP,                        -- nullable = never expires
    revoked_at      TIMESTAMP                         -- nullable = active
);

CREATE INDEX ix_pat_tokens_user_active ON pat_tokens(user_id) WHERE revoked_at IS NULL;
```

**Token format:** `smk_<32-byte-base64url>` where `smk` = "sovereign
memory key". The literal token is shown to the user *exactly once* on
issuance and never stored — only `sha256(token)` lives in the database.
This is the standard pattern (GitHub PATs, Stripe API keys).

### 4.2 Issuance flow

A new admin endpoint `POST /admin/tokens` (scope `admin:tokens:write`):

```
POST /admin/tokens
{
    "user_id": "uuid",
    "name": "opencode-workstation",
    "agent_type": "opencode",
    "expires_in_days": null
}

response (200, ONCE):
{
    "id": "uuid",
    "token": "smk_<example-placeholder-not-a-real-token>",
    "prefix": "smk_aB3c",
    "name": "opencode-workstation",
    "user_id": "uuid",
    "agent_type": "opencode",
    "created_at": "2026-04-11T..."
}
```

For Phase 1 there is exactly one user (Luis), one agent (OpenCode),
one token. The endpoint exists for future use; today it's bootstrapped
via a Makefile target or Alembic seed.

### 4.3 OpenCode integration

OpenCode reads its API key from config (the same place it reads the
existing `OPENAI_API_KEY` or whatever it calls it). The user pastes the
PAT once.

**No OpenCode-side code changes.** The proxy presents itself as a
standard OpenAI-compatible endpoint that requires bearer auth — exactly
what every agent already supports.

### 4.4 Resolution middleware

```python
async def require_user(
    authorization: str = Header(...)
) -> UserContext:
    token = _extract_bearer(authorization)
    pat = await pat_token_repo.lookup(sha256(token))
    if pat is None or pat.revoked_at is not None:
        raise HTTPException(401, "invalid token")
    if pat.expires_at and pat.expires_at < now():
        raise HTTPException(401, "token expired")
    user = await user_repo.get(pat.user_id)
    if not user.is_active:
        raise HTTPException(403, "user inactive")
    scopes = await scope_resolver.resolve(user, pat.agent_type)
    await pat_token_repo.touch(pat.id)  # async, fire-and-forget

    # Set the RLS guard for this connection
    await db.execute("SET LOCAL app.current_user_id = :uid", uid=user.id)

    return UserContext(
        user_id=user.id,
        username=user.username,
        agent_type=pat.agent_type,
        scopes=scopes,
        token_id=pat.id,
    )
```

The dependency is required on every protected route. Unauthenticated
requests get 401. The `UserContext` is the **only** way to learn who
is making a request — it is passed explicitly to every service method.

### 4.5 Phase 7 — OAuth2 device flow

Same `UserContext`, different acquisition path. The token type
becomes a JWT. The middleware checks the JWT signature against
Keycloak's JWKS (already wired in ADR-023). The `keycloak_sub` claim
maps to `users.keycloak_sub`. Scopes come from JWT claims instead of
the local `user_roles` table — or rather, the local table becomes a
cache of the Keycloak role assignment.

The point: `chat_completions` does not change. The middleware does.

---

## 5. Scope vocabulary

The scope namespace must be designed deliberately. A bad vocabulary
means either too coarse (everyone is "admin" or "reader") or too fine
(one scope per tool, unmanageable). The strawman:

```
memory:read                 — call any recall_* tool
memory:read-decisions       — call recall_decisions specifically
memory:read-skills          — call recall_skills specifically
memory:read-sessions        — call recall_recent_sessions (cross-user only with admin)
memory:read-semantic        — call recall_semantic
memory:write                — call any save_* tool
memory:write-skills         — call save_skill (deferred — memory writes are out of scope)
memory:write-decisions      — call save_decision (deferred)
memory:admin                — call administrative tools (audit log read, user listing)

admin:users:read            — list users
admin:users:write           — create / disable users
admin:tokens:read           — list tokens
admin:tokens:write          — issue / revoke tokens
admin:audit:read            — read the immutable audit trail

session:read-own            — implicit; everyone has this
session:read-others         — admin-only
```

**Design rules:**

1. Scope names are colon-separated, three segments max:
   `<domain>:<action>:<qualifier>`. Easy to read, easy to wildcard.
2. Scopes are *additive*. Granting `memory:read` grants every
   `memory:read-*`. Granting `memory:read-decisions` grants only that.
3. The default user has `memory:read` plus `session:read-own`. That's
   it. Everything else is opt-in.
4. Admin scopes are NEVER granted by default, even for the bootstrap
   user. Admin requires an explicit role assignment.

**Roles → scopes mapping** (Keycloak roles cached in `user_roles`):

| Role | Scopes |
|---|---|
| `member` (default) | `memory:read`, `session:read-own` |
| `senior-member` | + `memory:read-sessions` (own + team's, not cross-user) |
| `admin` | + `memory:admin`, `admin:users:*`, `admin:tokens:*`, `admin:audit:read` |
| `auditor` | `admin:audit:read`, `admin:users:read` (read-only governance) |

These are illustrative — the actual role names should match your
organisation's conventions. Scope vocabulary is the part that needs
to stay stable across role renames.

---

## 6. Tool registry as authorization boundary

This is where the §13.5 brainstorm recommendation becomes concrete.

### 6.1 Tool definitions carry their required scope

```python
@dataclass(frozen=True)
class MemoryToolDef:
    name: str
    description: str
    schema: dict  # JSON Schema for the params
    required_scope: str
    handler: Callable[[UserContext, dict], Awaitable[dict]]


MEMORY_TOOL_REGISTRY: list[MemoryToolDef] = [
    MemoryToolDef(
        name="recall_decisions",
        description="Recall ADRs (architectural decisions) relevant to a topic.",
        schema={...},
        required_scope="memory:read-decisions",
        handler=_handle_recall_decisions,
    ),
    MemoryToolDef(
        name="recall_skills",
        description="Recall skill / how-to documentation relevant to a topic.",
        schema={...},
        required_scope="memory:read-skills",
        handler=_handle_recall_skills,
    ),
    # ...
]
```

### 6.2 Per-request filter

```python
def tools_visible_to(user: UserContext) -> list[dict]:
    return [
        t.schema
        for t in MEMORY_TOOL_REGISTRY
        if _scope_matches(t.required_scope, user.scopes)
    ]


def _scope_matches(required: str, granted: list[str]) -> bool:
    """memory:read grants every memory:read-* — additive scope semantics."""
    if required in granted:
        return True
    parent = ":".join(required.split(":")[:-1])  # "memory:read-decisions" -> "memory:read"
    return parent in granted
```

### 6.3 Defense in depth at execution time

The filter at the request level is the *first* defense. The handler
itself checks again before doing any work:

```python
async def _handle_recall_decisions(user: UserContext, args: dict) -> dict:
    require_scope(user, "memory:read-decisions")
    query = args["query"]
    return await episodic_service.search(user_context=user, query=query)
```

A misconfigured tool registry that exposes a tool the user shouldn't
have is still caught by the handler. Two failures must occur for an
unauthorised call to succeed.

### 6.4 Tool call audit

Every tool dispatch writes a row to `tool_calls` (a new table):

```sql
CREATE TABLE tool_calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id  UUID NOT NULL REFERENCES interactions(id),
    user_id         UUID NOT NULL REFERENCES users(id),
    agent_type      VARCHAR(64) NOT NULL,
    tool_name       VARCHAR(255) NOT NULL,
    args            JSONB NOT NULL,
    result_summary  TEXT,
    error           TEXT,
    started_at      TIMESTAMP NOT NULL,
    duration_ms     INTEGER,
    granted_scope   VARCHAR(255) NOT NULL
);
```

This is the audit story made concrete. "Who did what" becomes a
single SQL query.

---

## 7. Cross-user isolation: five enforcement layers

D3 says isolation is enforced by design. Here is what "by design"
means in practice. The cost of one user seeing another's data is
high enough that we accept the redundancy.

### 7.1 Layer 1 — Schema NOT NULL constraints

Every per-user table has `user_id UUID NOT NULL REFERENCES users(id)`.
Insertions without an identity are rejected by the database, not the
application.

Tables that gain `user_id`:
- `interactions` (the audit trail)
- `sessions` (conversation summaries)
- `tool_calls` (new in this design)
- ChromaDB metadata (every embedded chunk has `user_id` in its metadata)

Tables that stay user-less (intentionally global):
- `users`, `user_roles`, `pat_tokens` (about users, not owned by them)
- `memory/episodic/*.md` (ADRs are project-public, not user-owned —
  scoped via project ACL instead)
- `memory/procedural/*.md` (skills are mostly global)

### 7.2 Layer 2 — Centralised query filter at the service layer

Every memory service method takes `user_context: UserContext` as the
**first parameter**. The query construction is centralised so every
`SELECT` is built through one function:

```python
def _scoped_query(base: Select, user: UserContext) -> Select:
    return base.where(InteractionRecord.user_id == user.user_id)
```

There is no public method on a service that builds a query without
going through `_scoped_query`. Code review enforces this.

### 7.3 Layer 3 — PostgreSQL Row-Level Security (load-bearing)

This is the layer that catches application bugs. RLS policies on every
per-user table:

```sql
ALTER TABLE interactions ENABLE ROW LEVEL SECURITY;

CREATE POLICY interactions_user_isolation ON interactions
    USING (user_id = current_setting('app.current_user_id')::uuid);

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY sessions_user_isolation ON sessions
    USING (user_id = current_setting('app.current_user_id')::uuid);

ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;

CREATE POLICY tool_calls_user_isolation ON tool_calls
    USING (user_id = current_setting('app.current_user_id')::uuid);
```

The middleware (`require_user`, §4.4) sets `app.current_user_id` at
the start of every request via `SET LOCAL`. The setting lasts for the
duration of the transaction and is automatically discarded on commit
or rollback.

**Even if the application code forgets the WHERE clause**, the database
returns zero rows. Application bugs cannot leak data.

The admin role gets a bypass policy:

```sql
CREATE POLICY interactions_admin_bypass ON interactions
    USING (current_setting('app.is_admin', true) = 'true');
```

Admin scope check happens in the middleware: if the user has
`memory:admin`, also `SET LOCAL app.is_admin = true`.

### 7.4 Layer 4 — ChromaDB metadata filter

ChromaDB has no RLS equivalent. Isolation comes from a mandatory
metadata filter on every query:

```python
class UserScopedSemanticService:
    def search(self, user_context: UserContext, query: str, k: int = 4):
        return self._chroma.query(
            query_texts=[query],
            n_results=k,
            where={"user_id": str(user_context.user_id)},  # MANDATORY
        )
```

The wrapper class enforces that no caller can construct a ChromaDB
query without the filter. The raw `chromadb` client is never imported
by service code — only the wrapper.

For "global" embeddings (e.g. shared skill RAG), the wrapper accepts
a `scope=GLOBAL` parameter that uses `where={"user_id": "GLOBAL"}`.
Two query paths, never mixed.

### 7.5 Layer 5 — Cross-user isolation tests

A dedicated test class that exists for one purpose: prove that user A's
queries return zero of user B's data. These are the tests I would be
most reluctant to ever delete.

```python
class TestCrossUserIsolation:
    """Every test in this class follows the same pattern:
    1. Create user A and user B
    2. User A persists some data
    3. Assert user B's queries return ZERO of user A's data
    4. Assert user A's queries still return user A's data (positive control)
    """

    def test_interactions_isolation(self, two_users_db):
        a, b = two_users_db
        as_user(a) → persist 3 interactions
        as_user(b) → query interactions → assert returns []
        as_user(a) → query interactions → assert returns 3 rows  # control

    def test_sessions_isolation(self, two_users_db):
        # same pattern
        ...

    def test_tool_calls_isolation(self, two_users_db):
        ...

    def test_semantic_isolation(self, two_users_chromadb):
        a, b = two_users_chromadb
        as_user(a) → embed 5 documents
        as_user(b) → semantic_search("anything") → assert returns []
        as_user(a) → semantic_search("anything") → assert returns 5 hits

    def test_rls_catches_application_bug(self, two_users_db):
        """Even if the application code forgets the user filter,
        RLS must catch it. Bypass the service layer entirely."""
        a, b = two_users_db
        as_user(a) → INSERT interaction directly via raw SQL
        as_user(b) → SELECT * FROM interactions  # no WHERE clause
        # → must return [] because RLS filtered it
```

These tests run in CI on every PR. Failing one of them blocks the
merge.

---

## 8. Migration phases

OpenCode-first. Each phase is a working state — the system runs at the
end of every phase, just with progressively more identity awareness.

### Phase 0 — Schema migration (1 day)

- Alembic migration: create `users`, `user_roles`, `pat_tokens`,
  `tool_calls` tables
- Add `user_id UUID` to `interactions` and `sessions`, NULL for now
- Seed: insert `users` row for Luis, role `admin`, one PAT for OpenCode
- Backfill: set `user_id` on existing rows to Luis's UUID
- Migration adds NOT NULL constraint AFTER the backfill in the same
  migration

**Test gate:** existing tests pass, all rows have `user_id`.

### Phase 1 — UserContext + auth middleware (1 day)

- New module `src/sovereign_memory/auth/identity.py` defining
  `UserContext` dataclass + `require_user` dependency
- Middleware reads PAT from Authorization header, looks up, returns
  `UserContext`
- A `SOVEREIGN_AUTH_REQUIRED=false` env defaults the dependency to a
  hardcoded "luis" UserContext for backwards compatibility during the
  migration; flipped to `true` at the end of Phase 5

**Test gate:** existing tests pass, new auth middleware tests pass.

### Phase 2 — UserContext plumbing (2 days)

- Every memory service method gains a `user_context: UserContext`
  parameter (first positional)
- `_persist_interaction` writes `user_context.user_id` into
  `interactions.user_id`
- `_compute_session_id` includes `user_context.user_id` in the hash
- Langfuse `langfuse.user.id` attribute populated from
  `user_context.username` (replacing the agent name)

**Test gate:** all existing tests updated to pass UserContext, full
suite green.

### Phase 3 — Tool registry filter (1-2 days)

- New module `src/sovereign_memory/tools/registry.py` with
  `MEMORY_TOOL_REGISTRY` and `tools_visible_to(user)` function
- Tool handlers receive `UserContext` and call `require_scope` defensively
- Scope vocabulary documented in `docs/architecture/scopes.md`
- This phase technically depends on the memory-as-tools work landing
  first (or at least the tool dispatch loop). Sequence with §1-9 of
  the brainstorm.

**Test gate:** scope filter unit tests, defense-in-depth handler tests.

### Phase 4 — RLS + ChromaDB wrapper (2 days)

- Alembic migration enables RLS on `interactions`, `sessions`,
  `tool_calls`
- Middleware sets `SET LOCAL app.current_user_id = ...` per request
- New `UserScopedSemanticService` wrapping the existing semantic
  service with mandatory `where` filter
- Update `dependencies.py` to inject the wrapper

**Test gate:** TestCrossUserIsolation class added (Phase 5 fills it in,
this phase scaffolds it).

### Phase 5 — Cross-user isolation tests + flip the flag (2 days)

- Implement the full TestCrossUserIsolation class from §7.5
- Run them; expect to find at least one bug they catch (the test
  exists for that reason)
- Fix what they find
- Flip `SOVEREIGN_AUTH_REQUIRED=true`
- End-to-end smoke test from OpenCode with the new PAT

**Test gate:** TestCrossUserIsolation passes, OpenCode round-trip works.

### Phase 6 — ADR-026 + cleanup (1 day)

- Promote this design doc to ADR-026
- Update `BRAINSTORM-memory-as-tools.md` §13 to reference the ADR
- Update `agent-configuration.md` to document the new auth requirement
- Update `README.md` setup instructions

**Test gate:** no code changes; documentation only.

### Phase 7+ (deferred)

- **Phase 7** — OAuth2 device flow with Keycloak (replaces PAT for
  human users; PATs remain for service accounts)
- **Phase 8** — Continue and Roo Code documentation; same wire shape
- **Phase 9** — VSCode native auth provider for VSCode-based agents
- **Phase 10** — Admin UI for token management

These do not block the first multi-user release.

**Total Phase 0-6 estimate:** ~10 days of focused work, sequenced
strictly. Phases are intentionally small enough that a half-finished
phase is recoverable.

---

## 9. Open questions

These are the points where I still need a decision before writing
ADR-026:

1. **Token prefix.** `smk_` (sovereign memory key) is my proposal.
   Alternative: `sov_` or `slmk_`. Style call.

2. **Token expiry default.** PATs default to never expire. Should they?
   90-day default with renewal would be more enterprise-friendly but
   more friction for the single-user case today.

3. **Bootstrap user creation.** Makefile target (`make bootstrap-user`)
   that runs an Alembic seed, or a one-shot CLI command? The Makefile
   target is friendlier for local setup but less general.

4. **Scope vocabulary review.** The §5 strawman is my opinion. Want to
   align with the organisation's existing scope conventions if there are any?

5. **Admin RLS bypass mechanism.** The `app.is_admin` setting is one
   approach; another is a separate `admin` Postgres role with the
   `BYPASSRLS` attribute. The former is per-request, the latter is
   per-connection. Per-request is more flexible.

6. **What does an "agent_type = opencode" PAT mean if used from curl?**
   Does the proxy enforce that the User-Agent matches the registered
   agent_type? Probably yes — it's another defense layer at no cost.

7. **`pat_tokens.last_used_at` is a write on every authenticated
   request.** That's a Postgres write per request, which is exactly
   the kind of latency the §12 async persistence design is trying to
   avoid. Solution: route this write through the same async persistence
   path. Couples §13 and §12.

8. **Scope check inside tool handler vs at the registry filter.** The
   defense-in-depth in §6.3 is duplicative — should the registry
   check be the only one (trust the filter) or keep both? My instinct
   is keep both.

9. **GDPR delete.** When a user is deleted, what happens to their
   `tool_calls` and `interactions`? Hard delete (loses audit), soft
   delete with PII redaction (keeps audit, may not satisfy GDPR), or
   per-row retention metadata (most flexible)?

10. **How does ChromaDB handle the `where={"user_id": ...}` filter at
    scale?** Single global collection with metadata filter is simplest
    but may have query performance issues at >1M chunks. Alternative:
    one collection per user. Out of scope for Phase 1 (Luis only) but
    needs a decision before Phase 8.

---

## 10. Risks

- **R1 — Migration order matters and is hard to roll back.** Adding
  `user_id NOT NULL` to existing tables requires backfill. A bad
  migration is hard to undo. Mitigation: backup before Phase 0, test
  the migration on a dev DB first.

- **R2 — RLS settings are easy to forget.** A request that hits
  Postgres without `SET LOCAL app.current_user_id` will return zero
  rows for everyone, which is technically safe but surprising.
  Mitigation: a session-level default + an assertion in tests.

- **R3 — `SET LOCAL` requires a transaction.** SQLAlchemy's autocommit
  mode would skip it. Mitigation: enforce that all DB sessions are
  transactional, no autocommit.

- **R4 — PAT in plaintext config files.** A user committing OpenCode
  config to git leaks the token. Mitigation: token revocation endpoint,
  short prefix display so leaked tokens are easy to identify, document
  the risk in setup instructions.

- **R5 — Cross-user isolation tests can become slow.** They create
  multiple users per test. Mitigation: shared two-user fixture at
  module scope.

- **R6 — Phase 3 depends on memory-as-tools.** The tool registry
  doesn't exist yet. Sequencing: either land the tool registry first
  (memory-as-tools brainstorm §§ 1-9) or scaffold it as part of this
  work. My instinct is to land memory-as-tools (or at least the
  registry) first, then layer multi-user on top.

- **R7 — Admin bypass policy is a footgun.** A bug that grants admin
  scope incorrectly nukes the entire isolation guarantee. Mitigation:
  admin scope is granted explicitly per Keycloak role, never by
  default; admin actions are themselves audited.

- **R8 — Token rotation operational overhead.** With one user it's
  zero. With 100 users it's a procedure. Out of scope for Phase 1 but
  worth flagging for the deployment runbook.

---

## 11. What this design does NOT change

For clarity, things that stay the same:

- The chat completions API surface remains OpenAI-compatible. Clients
  see no protocol change beyond the requirement to send a Bearer token.
- The four memory layers themselves (episodic / procedural /
  conversational / semantic) keep their current implementation; only
  their *callers* change to pass `UserContext`.
- Langfuse trace shape — only the `user.id` attribute changes from
  the agent name to the resolved username.
- The streaming generator and tool-call loop (post-ADR-024) keep their
  current async machinery.
- ChromaDB stays in server mode (ADR-020); just the wrapper around it
  changes.

---

## 12. Sequencing relative to memory-as-tools

The brainstorm §13.5 made the case that multi-user identity should be
designed *before* memory-as-tools so the tool registry is built with
the user dimension from day one. This design doc honours that:

- **Phases 0-2 of this work can land independently** of memory-as-tools.
  They add the user dimension to the existing service surface without
  touching the chat handler's tool logic.
- **Phase 3 (tool registry filter) requires the memory-as-tools work
  to exist** — the tool registry doesn't exist yet. Two options:
  - (a) Land memory-as-tools brainstorm §§ 1-9 first, then this
    design's Phase 3 layers identity on top.
  - (b) Land Phases 0-2 of this design first, and include the tool
    registry as part of the memory-as-tools implementation when it
    happens (so the registry is multi-user from line one).
- **Phases 4-6 do not depend on memory-as-tools** and can land in
  parallel.

My recommendation: **option (b)**. Phases 0-2 of multi-user land first
(adds the dimension without changing tool logic). Then memory-as-tools
implementation builds the registry with multi-user awareness from the
start. Phase 3 of this design becomes a thin verification step.

---

## 13. Next steps

1. **Luis reviews this design doc.** Push back on §1 decisions, §5
   scope vocabulary, §8 phase sequencing, §9 open questions.
2. **Decisions on §9 questions 1-6** (the implementation-blocking
   ones) before any code is written.
3. **Decide the sequencing question in §12** — option (a) or (b).
4. **Promote to ADR-026** once the design is settled.
5. **Spike Phase 0** (the schema migration) as the first concrete
   work — it's the smallest verifiable step.

---

*Design in progress. No code touched. The shape of multi-user is now
on paper; the remaining work is settling the open questions and
sequencing it against memory-as-tools.*

---

## 14. Status update — Phase 0 and Phase 1 shipped

Phase 0 (schema, models, repos) landed in `c5de424`. Phase 1
(`require_user` PAT dependency) landed in `fe2c033`. Both passed all
gates. The work was sound on its own terms — but during the post-Phase-1
review, the architectural seam was challenged and corrected (see §15).

**What survives:**
- `UserContext` frozen dataclass — pure data, source-agnostic, kept
- `is_admin_scope` helper — kept
- `sentinel_user_context` — kept (slightly retuned, now keyed on a
  Keycloak-shaped sub claim)
- `require_user` FastAPI dependency interface — kept (the function
  still returns a `UserContext`; only the source changes)
- `tool_calls` audit table — kept, with `user_id` repurposed from a
  FK to a plain Keycloak `sub` string
- `interactions.user_id` and `sessions.user_id` — kept, same shape
  change
- The bypass mode env var pattern (`SOVEREIGN_AUTH_REQUIRED=false`) —
  kept for backwards compat during the migration window
- All quality discipline (test-first, per-file 90% gate, lint clean,
  TDD red→green) — non-negotiable, unchanged

**What gets removed in the §15 refactor:**
- `users` table
- `user_roles` table
- `pat_tokens` table (the table; the *cache idea* survives in memory)
- `UserRepo` class
- `PatTokenRepo` class
- `_ROLE_SCOPES` static mapping and `roles_to_scopes` function
- The agent_type defense layer in `require_user` (PAT-specific; with
  JWTs the token is agent-agnostic — see §15.6 for the trade-off)

**Migration approach:** option (b) **forward migration**. Alembic 004
drops the orphaned tables and FKs cleanly. Git history stays honest:
Phase 0/1 commits are preserved (they taught us something), the new
direction lands as a follow-on migration that you can read in
chronological order.

---

## 15. Identity is delegated to Keycloak (the corrected design)

This section replaces §4 (PAT model) and §13.5's recommendation about
designing local identity interfaces. The architectural seam is moved.

### 15.1 The corrected mental model

> **Keycloak knows who you are. Sovereign-memory-server knows what
> you did. The two never share state except via tokens in flight.**

- **Keycloak owns:** users, passwords, MFA, roles, scopes, group
  membership, token issuance, token revocation, session lifecycle,
  authentication audit.
- **Sovereign-memory-server owns:** the audit trail of agent
  interactions (`interactions`, `sessions`, `tool_calls`), the four
  memory layers, the in-memory token cache for performance.

This split means we never duplicate user state. Adding/removing/
disabling a user is a single Keycloak operation; sovereign-memory-server
finds out about it via the next token validation that fails, with
bounded latency (the cache TTL).

### 15.2 The new identity flow, end-to-end

```
┌─────────────────┐
│   Luis (human)  │
└────────┬────────┘
         │ types prompt
         ▼
┌─────────────────────────────────┐
│         OpenCode (CLI)          │
│                                 │
│  one-time:  opencode auth login │
│  → device flow → Keycloak       │
│  → JWT cached locally           │
│  → refresh token kept           │
│                                 │
│  on every request:              │
│  Authorization: Bearer <jwt>    │
└────────┬────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│              sovereign-memory-server                      │
│                                                           │
│  ┌──────────────────────────────────────────────────┐   │
│  │ require_user (FastAPI dependency)                │   │
│  │                                                  │   │
│  │  1. Extract bearer token                         │   │
│  │  2. token_hash = sha256(raw)                     │   │
│  │                                                  │   │
│  │  3. TokenCache.get(token_hash)  ← hot path       │   │
│  │     ├─ HIT → return cached UserContext           │   │
│  │     │       (sub-millisecond, no Keycloak call)  │   │
│  │     │                                            │   │
│  │     └─ MISS → cold path                          │   │
│  │              ┌─────────────────────────────┐    │   │
│  │              │ JWT signature validation    │    │   │
│  │              │  - fetch JWKS (cached 1h)   │    │   │
│  │              │  - verify RS256 signature    │    │   │
│  │              │  - check exp / iss / aud   │    │   │
│  │              └──────────────┬──────────────┘    │   │
│  │                             ▼                    │   │
│  │              ┌─────────────────────────────┐    │   │
│  │              │ Build UserContext from claims│   │   │
│  │              │  user_id  = sub              │    │   │
│  │              │  username = preferred_username│  │   │
│  │              │  scopes   = scope.split()    │    │   │
│  │              │  is_admin = is_admin_scope() │    │   │
│  │              └──────────────┬──────────────┘    │   │
│  │                             ▼                    │   │
│  │              TokenCache.put(hash, ctx, ttl)     │   │
│  │              Return UserContext                 │   │
│  └──────────────────────────────────────────────────┘   │
│                     │                                    │
│                     ▼                                    │
│  ┌──────────────────────────────────────┐               │
│  │ chat handler (Phase 2 plumbing)      │               │
│  │  service methods receive UserContext │               │
│  └──────────────────────────────────────┘               │
└──────────────────────────────────────────────────────────┘
```

**Hot path:** ~50µs (cache lookup + return). Indistinguishable from
no-auth latency-wise. This is the path 99%+ of authenticated requests
take after the first one.

**Cold path:** ~1-2ms (JWT signature validation, mostly RSA). The first
request with a new token, plus every TTL boundary thereafter. Zero
network round-trips because JWS validation is local against cached
JWKS keys.

**No introspection in Phase 1.** The cache TTL is the only revocation
latency. Phase 8+ may add periodic introspection for paranoid
revocation if needed; for now, 5-minute revocation latency is
acceptable for the threat model.

### 15.3 Schema, after the forward migration (Alembic 004)

```sql
-- DROPPED: users, user_roles, pat_tokens (Phase 0 schema)

-- KEPT, with FK to users removed:

interactions:
  ...
  user_id VARCHAR(36) NULL  -- Keycloak sub claim, no FK

sessions:
  ...
  user_id VARCHAR(36) NULL  -- Keycloak sub claim, no FK

tool_calls:
  ...
  user_id VARCHAR(36) NOT NULL  -- Keycloak sub claim, no FK
  interaction_id INTEGER NOT NULL REFERENCES interactions(id)  -- FK kept
  agent_type VARCHAR(64) NOT NULL  -- from User-Agent at request time
  ...
```

That is the entire schema change. Three tables dropped, four FK
constraints removed, all `user_id` columns become opaque-string
references to whatever Keycloak says is the subject.

### 15.4 The TokenCache (Redis-backed)

> **Revised 2026-04-11:** the cache lives in **Redis**, not in
> Python process memory. In-memory was rejected because:
> 1. It does not scale beyond a single sovereign-memory-server process
> 2. Horizontal scaling (which is on the enterprise roadmap) requires
>    a shared cache from day one — retrofitting later means every
>    process has its own cache state, leading to inconsistent
>    revocation behaviour and duplicate Keycloak introspection load
> 3. Redis is a familiar, well-supported, ops-friendly piece of infra
>    that the team already runs (Langfuse uses one — separate instance)

A dedicated **`sovereign-redis`** container is added to
`docker-compose.yml`. It is **not** the Langfuse Redis — keeping the
two systems decoupled (separate containers, separate passwords,
separate volumes) avoids cross-system coupling and lets each scale
independently.

```python
class TokenCache:
    """sha256(token) → UserContext + TTL, backed by Redis.
    
    Cache keys are namespaced under ``sovereign:token:<hash>`` so
    multiple consumers of the same Redis instance (none today, but
    designing for it) cannot collide.
    
    Thread-safe by virtue of Redis itself — no in-process locks needed.
    Survives process restart (the Redis container persists across
    sovereign-memory-server restarts).
    
    Phase 8+ horizontal scaling: just point N processes at the same
    Redis. No code change.
    """
    KEY_PREFIX = "sovereign:token:"
    
    def __init__(self, redis_client: Redis, default_ttl_seconds: int = 300): ...
    
    def get(self, token_hash: str) -> UserContext | None: ...
    def put(self, token_hash: str, ctx: UserContext, ttl_seconds: int | None = None) -> None: ...
    def invalidate(self, token_hash: str) -> None: ...
    def clear(self) -> None: ...  # tests — uses SCAN, not KEYS *
    def size(self) -> int: ...    # observability — approximate via SCAN
```

**Default TTL:** 5 minutes (`SOVEREIGN_TOKEN_CACHE_TTL_SECONDS=300`),
overridable per-call (the cold path uses `min(jwt.exp - now, default_ttl)`
so the cache never holds an entry longer than the JWT itself is valid).

**Eviction:** Redis handles it. We use `SETEX` (set with expiry) so
keys vanish automatically at TTL. If the cache grows huge,
`maxmemory-policy allkeys-lru` on the Redis side enforces a soft cap.
Default Redis config (no maxmemory) is fine for the single-user case
and even small enterprise deployments — token caches are tiny
(hundreds of entries, not millions).

**Cache key invariant** (unchanged from in-memory design): the key is
always `sovereign:token:<sha256(raw_token)>`. The raw token never
crosses the wire to Redis, never lives in any log, never survives a
`print()`. A `redis-cli KEYS *` dump shows only opaque hashes that
are useless without the original token. The serialized payload
contains the `UserContext` JSON — including the `sub`, scopes, etc. —
but no raw token material. Anyone who breaches the Redis instance
gets a list of valid (hash → claims) bindings, which lets them
**verify** an existing token but not **forge** new ones.

**Test strategy:** unit tests use `fakeredis` (a Redis-protocol-
compatible in-process implementation, no real Redis required). The
`TokenCache` class is constructed with a fake client in test
fixtures; production wires a real `redis.Redis` client from settings.
The interface is identical, so the same tests cover both paths.

**Connection management:** a single shared `redis.Redis` client
instance lives in `identity.py` as a module-level singleton, lazily
constructed on first access. Connection pooling is handled inside
the redis-py library (default pool size 10). FastAPI's threadpool
workers all share the same client; redis-py is thread-safe.

### 15.4a New configuration

```python
# config.py additions
redis_url: str = "redis://localhost:6379/0"
redis_password: str | None = None
token_cache_ttl_seconds: int = 300
```

`SOVEREIGN_REDIS_PASSWORD` is read from the secrets file at startup,
same pattern as `SOVEREIGN_POSTGRES_PASSWORD` and
`SOVEREIGN_CHROMA_TOKEN`. The new secret file is
`secrets/redis_password.txt`.

### 15.4b docker-compose additions

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: sovereign-redis
    restart: unless-stopped
    command:
      - redis-server
      - --requirepass
      - ${SOVEREIGN_REDIS_PASSWORD}
      - --appendonly
      - "no"  # token cache is ephemeral by design
    volumes:
      - sovereign-redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "$$SOVEREIGN_REDIS_PASSWORD", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - sovereign-ai-net

  memory-server:
    depends_on:
      redis:
        condition: service_healthy
      # ... existing dependencies
    environment:
      - SOVEREIGN_REDIS_URL=redis://redis:6379/0
      - SOVEREIGN_REDIS_PASSWORD=${SOVEREIGN_REDIS_PASSWORD}
      # ... existing env

volumes:
  sovereign-redis-data:
```

The volume exists so a container restart doesn't blow away the cache,
but `--appendonly no` means we don't write a durable AOF — token
caches are ephemeral by definition; if Redis itself dies, every
sovereign-memory-server instance falls back to JWT validation on the
cold path and re-populates within seconds.

### 15.5 What the JWT carries (Phase 1, JWS)

Standard Keycloak JWT claims:

| Claim | Source | Used as |
|---|---|---|
| `sub` | Keycloak user UUID | `UserContext.user_id`, audit `user_id` |
| `preferred_username` | Keycloak `username` | `UserContext.username` |
| `email` | Keycloak `email` | `UserContext.extra["email"]` |
| `name` | Keycloak `name` | `UserContext.extra["name"]` |
| `scope` | Keycloak client scopes | `UserContext.scopes` (split on whitespace) |
| `jti` | Keycloak JWT id | `UserContext.token_id` (for audit) |
| `exp` | Keycloak expiry | bounds the cache TTL |
| `iss`, `aud` | issuer + audience | validated against settings |

**Scopes come from Keycloak**, not from a local roles→scopes mapping.
The realm administrator grants OAuth2 scopes to the client (e.g. via
client scope mappers in the Keycloak admin console). What lands in the
JWT `scope` claim is the source of truth for what the user is allowed
to do. We do not maintain a parallel mapping table.

This is the **biggest change from §4**: the `_ROLE_SCOPES` dict in
`identity.py` and the `roles_to_scopes` function go away. Less code,
fewer places to drift.

### 15.6 Trade-offs the new model accepts

**Lost: agent_type defense layer.** In §4 the PAT was registered for
a specific agent_type (`opencode`, `continue`, etc.) and the middleware
enforced that the User-Agent header matched. With Keycloak-issued JWTs,
the token is agent-agnostic by construction — the same token works
from any client that has it. We **record** the agent_type from the
User-Agent header into `tool_calls` and `interactions` for forensics,
but it is not an authorization gate.

**Mitigation if you want it back:** add a custom Keycloak claim
(`sovereign:agent_type`) via a Keycloak mapper. The middleware checks
the claim against the User-Agent. This is a Keycloak config change
plus 5-10 lines of code in `require_user`. Out of scope for Phase 1
but trivial to add later.

**Lost: per-instance token revocation visibility.** With the in-memory
cache, a token revoked at Keycloak remains usable until the next
cache miss (≤ 5 minutes). For paranoid scenarios, two upgrade paths:

1. Lower the cache TTL (60s, 30s) — trades latency/Keycloak load for
   tighter revocation
2. Add periodic introspection — every cache hit also fires an async
   `is_token_active` check against Keycloak, results piggyback on the
   cache TTL

Either is a Phase 8+ change. Phase 1 accepts 5-minute revocation
latency as the default.

### 15.7 What goes away from Phase 0/1

| Phase 0/1 artifact | Fate |
|---|---|
| `users` table | Dropped in migration 004 |
| `user_roles` table | Dropped in migration 004 |
| `pat_tokens` table | Dropped in migration 004 (cache idea moves in-memory) |
| `UserRepo` class | Deleted from `identity.py` |
| `PatTokenRepo` class | Deleted from `identity.py` |
| `_ROLE_SCOPES` mapping | Deleted from `identity.py` |
| `roles_to_scopes` function | Deleted from `identity.py` |
| `generate_token` / `extract_prefix` | Deleted (no PAT issuance) |
| `hash_token` | **Kept** — the cache uses it to hash JWT tokens |
| `UserContext` | **Kept** — pure data, source-agnostic |
| `sentinel_user_context` | **Kept** — bypass mode unchanged |
| `is_admin_scope` | **Kept** |
| `require_user` (interface) | **Kept** — same signature, new implementation |
| `pat_auth_required` setting | Deleted, replaced by reusing `auth_enabled` |
| `tool_calls` table | Kept, FK to `users` removed |
| `interactions.user_id` / `sessions.user_id` | Kept, FK removed |

### 15.8 New decisions accepted

These supersede §1 D4 and the §6 open questions on PAT token format.

**D4'.** First client is OpenCode, **with Keycloak-issued JWS tokens**
acquired via OAuth2 device flow. The middleware reuses the existing
JWKS validation logic from `require_scope` (ADR-022/023), which is
already wired. JWE encryption is deferred to Phase 8+ behind a
config flag — we get there by changing one validation call, not by
re-architecting.

**D6 (new).** Identity is delegated entirely to Keycloak.
sovereign-memory-server holds **no** local users table, no roles
table, no token table. Caching is in-process memory only.

**D7 (new).** The `auth_enabled` setting (existing, JWT path) is the
single master switch. The `pat_auth_required` setting introduced in
Phase 1 is removed. Both `require_scope` and `require_user` honour
`auth_enabled`.

**D8 (new).** Tool authorization (originally proposed via `_ROLE_SCOPES`)
moves to **Keycloak client scopes**. The realm administrator configures
which OAuth2 scopes are granted to which roles via the Keycloak admin
console. The `scope` claim in the JWT is authoritative. Our code
checks for scopes via `is_admin_scope` and a future
`require_scope_in_user_context` helper — no local mapping table.

### 15.9 Open questions that remain

1. **Cache TTL default — 5 min, 1 min, or env-configurable?** I'd
   default to 5 min and add `SOVEREIGN_TOKEN_CACHE_TTL_SECONDS` as an
   override. Unanswered.

2. **JWT issuer / audience for the dev environment.** We need a
   Keycloak realm running locally with a client configured for
   sovereign-memory-server, and OpenCode needs to know the device-flow
   endpoint. ADR-022/023 already wired the server side; the client
   side (OpenCode `opencode auth login` flow) is the missing piece.

3. **OpenCode integration — does it support OAuth2 device flow today?**
   If not, we need either (a) a wrapper script that does the device
   flow and writes the JWT to `~/.config/opencode/api_key`, or (b) a
   PR upstream to add OAuth2 support.

4. **Bypass mode default in the test container.** Currently
   `auth_enabled=false`. Same default after the refactor, no behaviour
   change for tests. ✓

5. **Cross-user isolation tests under the new model.** The Phase 5
   strategy still applies — but instead of seeding two users via
   `UserRepo`, we generate two test JWTs with different `sub` claims
   (the existing `_make_token` helper in `test_auth.py` already does
   this). Cleaner.

### 15.10 Sequencing relative to the original phases

> **This table was the pre-flight plan. See §16 for the actual
> post-flight status with commit SHAs.**

| Original phase | Status under §15 (as-planned) | Actual status (end of 2026-04-11) |
|---|---|---|
| Phase 0 — schema (users, pat_tokens, etc.) | Superseded by migration 004 | ✅ **Done.** `c5de424` then dropped via `e9b8799` / migration 004. |
| Phase 1 — `require_user` PAT | Replaced — JWT validation + Redis cache | ✅ **Done.** `e9b8799` refactor. Live-verified in Phase 7a smoke test (9c8af21). |
| Phase 2 — `UserContext` plumbing through services | Plumbing target unchanged | ✅ **Done.** Seven atomic commits `afb0c5d..5857d46` on 2026-04-11 morning. 313 → 321 tests. Cross-user isolation tests at the service layer included. |
| Phase 3 — tool registry filter | Filters by `user.scopes`, source-agnostic | ✅ **Done as part of ADR-025** (memory-as-tools). `tools_visible_to(user_context)` in `src/sovereign_memory/tools/__init__.py`, scope enforced at tool advertisement (`c8d469e`) AND defensively at dispatch (`74ac439`). Live-verified in Phase 7a (audit row landed with `granted_scope=memory:episodic:read`). |
| Phase 4 — RLS + ChromaDB wrapper | `user_id` column is a Keycloak sub string, RLS still works on string equality | ✅ **Done 2026-04-11 evening.** Alembic migration `005_enable_rls_policies` + ContextVar + SQLAlchemy `after_begin` listener (`db/rls.py`) + `UserScopedSemanticService` thin wrapper + non-superuser `sovereign_app` role + init script. Two commits: `fb005a0` (infra + tests) and `f0f88ce` (sovereign_app + live verification). RLS live-biting against the running stack — 3-way proof documented in §16. Soft follow-up: per-request wrapper wiring in `dependencies.py`. |
| Phase 5 — cross-user isolation tests + flag flip | Tests use generated JWTs with different `sub`s | ✅ **Done 2026-04-11 evening.** Phase 5a consolidated `TestCrossUserIsolation` class (`e745b32`) runs alice + bob across every layer in one file (10 cases). Phase 5b flipped `SOVEREIGN_AUTH_REQUIRED=true` as the docker-compose default (`c0243e0`); conftest env-wipe keeps the Python suite in bypass mode so the 421 existing tests are unaffected. Live-verified: no-auth → 401, real JWT → 200, interaction row persists with the Keycloak service account sub. |
| Phase 6 — ADR-026 | The ADR captures §15 as the canonical decision | ✅ **Done 2026-04-11 evening.** This file was promoted from `docs/architecture/DESIGN-multi-user-identity.md` to `docs/ADR-026-multi-user-identity.md` with `Status: Accepted`. Cross-refs across 19 files updated via `git mv` + global find-replace. You are reading the accepted ADR. |
| Phase 7 — OAuth2 device flow | Already covered by §15 | ✅ **Done 2026-04-11 evening** (dev client path). New `sovereign-memory-dev` Keycloak client (client-secret + service account + audience mapper), `scripts/mint-dev-jwt.sh` helper, realm JSON mirrors the running realm. Device flow for human auth + OpenCode token configuration docs remain as Phase 8+ operator work, but the Phase 5b flag flip did not need them — the dev client is enough for curl / Bruno / ad-hoc dogfooding. |
| Phase 8+ — Continue / Roo Code / VSCode native auth | Same JWT shape, different acquisition flow per client | ⏳ **Deferred.** |
| Phase 8+ — JWE + introspection | New deferred phase. Behind config flags, no architectural change. | ⏳ **Deferred.** |

Net effect (original plan): Phases 0/1 redone, 2–6 unchanged, Phase 7
absorbed into the new Phase 1, Phase 8+ stays the same. The original
~10-day estimate for Phases 0-6 stands, with Phase 0/1 now both fitting
in a single day instead of two.

**Actual schedule delivered by end of 2026-04-11:** Phases 0 → 3 done
in a single day because Phase 3 was absorbed into the memory-as-tools
work that happened to land on the same day. Phases 4/5/6/7 remain.

### 15.11 What the §15 refactor commit will do

In one atomic commit on `feat/memory-as-tools`:

1. **`requirements.txt` / `pyproject.toml`** — add `redis>=5.0` and
   `fakeredis>=2.30` (the latter as a dev dep).
2. **`docker-compose.yml`** — add `sovereign-redis` service (Redis 7
   Alpine), volume, healthcheck, env wiring on `memory-server`,
   `depends_on`.
3. **`secrets/redis_password.txt`** — new secret file (gitignored,
   the developer creates it via the existing setup script pattern).
4. **Alembic migration 004** — drop `users`, `user_roles`,
   `pat_tokens`; drop FKs from `interactions`/`sessions`/`tool_calls`
   to `users`.
5. **`db/models.py`** — delete `User`, `UserRole`, `PatToken` classes;
   adjust FK declarations on the surviving columns.
6. **`identity.py`** — delete `UserRepo`, `PatTokenRepo`, `_ROLE_SCOPES`,
   `roles_to_scopes`, `generate_token`, `extract_prefix`. Add
   `TokenCache` class **backed by Redis**, plus `get_token_cache()`
   singleton accessor that lazily constructs the client from settings.
   Keep `UserContext`, `hash_token`, `is_admin_scope`,
   `sentinel_user_context`.
7. **`auth.py`** — replace `require_user` implementation: cache lookup
   first (Redis), then JWT validation against JWKS, then cache write
   (Redis SETEX). Reuses the existing `_get_jwks_keys` function.
8. **`config.py`** — delete `pat_auth_required`. Add `redis_url`,
   `redis_password`, `token_cache_ttl_seconds` (default 300).
9. **Tests** — delete the obsolete classes (`TestUserSchema`,
   `TestUserRoleSchema`, `TestPatTokenSchema`, `TestUserRepo`,
   `TestPatTokenRepo`, `TestRolesToScopes`, `TestRequireUserPATSuccess`,
   `TestRequireUserPATFailures`). Add new ones: `TestTokenCache` (with
   `fakeredis` fixture), `TestRequireUserJWTSuccess`,
   `TestRequireUserJWTFailures`, `TestRequireUserCacheBehavior`, plus
   migration 004 verification in `test_alembic.py`.
10. **All gates green:** ruff check, ruff format --check, full pytest
    suite, per-file 90% coverage gate, lint clean.

The post-refactor branch state should pass `make test` end-to-end with
zero new known issues. Then we resume the original phase plan from
Phase 2.

---

## 16. Status snapshot — end of 2026-04-11

> **This section is the authoritative current-state record. Read it
> first if you are returning to this doc after any gap.**

The day's work pushed the multi-user roadmap from "Phase 0/1 shipped
but architecturally wrong" to "Phases 0-3 shipped on the corrected §15
seam, Phases 4/5/6/7 queued". Two major arcs shipped in a single
session — (a) multi-user Phase 2 `UserContext` plumbing and (b)
ADR-025 memory-as-tools (which absorbed Phase 3). Everything is on
`feat/memory-as-tools` and pushed to origin.

### 16.1 What shipped (by commit)

**Phase 2 — `UserContext` plumbing through every memory service
method** (2026-04-11 morning, 7 commits):

| Commit | Scope |
|---|---|
| `614010a` | `test(conftest)` — session-scoped `user_context` fixture backed by `sentinel_user_context()` |
| `8fb93cf` | `refactor(episodic)` — pure plumbing (shared filesystem corpus) |
| `74c25a5` | `refactor(procedural)` — same |
| `ae58265` | `refactor(semantic)` — plumbing + **admin-gated Chroma `where={"user_id": ...}` filter** (Phase 4 preview at the service layer) |
| `81449e9` | `refactor(conversational)` — plumbing + per-user `SELECT` filter + `save_session` writes `user_id` + cross-user isolation test at the SQL layer + microsecond session-id fix (pre-existing bug surfaced by the new isolation test) |
| `7ffcdb0` | `refactor(context-builder)` — `UserContext` threaded to all four layers |
| `5857d46` | `refactor(routes)` — `chat_completions` + `/context` + `/session/summary` routes `Depends(require_user)`, `_compute_session_id(source, first, user_id)`, `_persist_interaction(..., user_id)`, `langfuse.user.id = user.user_id` |

**Phase 3 — tool registry filter** shipped inside ADR-025 (2026-04-11
afternoon, 9 commits total including Phase 7a):

| Commit | Scope |
|---|---|
| `1f49628` | `docs(adr)` — draft ADR-025 memory-as-tools (proposed) |
| `7ed9808` | `feat(config)` — ADR-025 Phase 0: settings + drop Langchain deps |
| `c8d469e` | `feat(tools)` — ADR-025 Phase 1: **dynamic memory-tool registry primitives**, `MemoryTool` dataclass, `@register_memory_tool` decorator, **`tools_visible_to(user_context)`**, TOML overlay, `get_tool_by_name` |
| `04c2459` | `feat(tools)` — ADR-025 Phase 2: four concrete handlers + `ToolResultCache` (Redis, `sovereign:tool-result:*` prefix disjoint from TokenCache) |
| `9b6a577` | `feat(context-builder)` — ADR-025 Phase 3: `build_ambient_context` |
| `74ac439` | `feat(chat)` — ADR-025 Phase 4a: **proxy-internal memory tool-call loop** with defensive per-dispatch scope re-check |
| `75b9447` | `feat(chat)` — ADR-025 Phase 4b: loop wired into `chat.py`, `_handle_tools_mode`, SSE synthesis, `ToolCall` audit row flush post-interaction |
| `779bc3b` | `docs(arch)` — ADR-025 Phase 5: sequence diagrams |
| `76230ac` | `docs(arch)` — ADR-025 Phase 6: C4 workspace update |
| `9c8af21` | `feat(compose)+docs` — ADR-025 Phase 7a: **live smoke test passed** + docker-compose env knobs exposed |

**Multi-user Phase 4 — Postgres RLS + ChromaDB wrapper** (2026-04-11
evening, 2 commits after the doc reality-check):

| Commit | Scope |
|---|---|
| `952c242` | `docs(arch)` — reality-check DESIGN + BRAINSTORM against what actually shipped |
| `fb005a0` | `feat(rls)` — Phase 4 infrastructure: Alembic migration 005 RLS policies (ENABLE + FORCE on interactions/sessions/tool_calls), `db/rls.py` (ContextVar + after_begin listener), `UserScopedSemanticService` thin wrapper, `require_user` populates the ContextVar on all paths, `server.py` lifespan installs the listener. 20 new tests across `test_rls.py` (15 unit), `test_rls_isolation.py` (5 integration against real Postgres with a non-superuser test role via `SET ROLE`), and `test_semantic_service.py::TestUserScopedSemanticService` (5 wrapper cases). |
| `f0f88ce` | `feat(stack)` — non-superuser `sovereign_app` role completes Phase 4 in the running stack. New `scripts/init-sovereign-app-role.sh` (idempotent, docker-entrypoint-initdb.d compatible, password rotation aware), docker-compose wiring memory-server as `sovereign_app` with sovereign_app_password fallback. RLS live-verified 3-way: no-GUC sovereign_app = 0 rows, GUC-sentinel sovereign_app = 1 row, superuser sovereign = 945 rows. |

### 16.2 Live verification

Phase 7a of ADR-025 was a full end-to-end smoke test against the real
docker-compose stack running the real `Qwen3.5-35B-A3B` model. Full
findings in `docs/sessions/2026-04-11-phase7-smoke-test.md`. Every
piece of the multi-user pipeline was verified live:

- Sentinel `user_id` (`00000000-0000-0000-0000-000000000001`) threads
  through `_compute_session_id`, lands in `interactions.user_id`, and
  lands in `tool_calls.user_id`
- `ToolCall` audit rows write with the correct `interaction_id` FK,
  `granted_scope`, `agent_type`, `duration_ms`
- Redis `ToolResultCache` populates under `sovereign:tool-result:*`
  and cache hits correctly skip the audit row
- `memory_mode=tools` end-to-end: model called `recall_decisions`,
  proxy dispatched via the loop, real ADR-009 content flowed from the
  filesystem through the handler into the model's final answer
- The `sovereign-redis` container was **verified for the first time**
  — a papercut (`.env` was missing `SOVEREIGN_REDIS_PASSWORD`) was
  found and fixed during the smoke test. See the session doc.

**Phase 4 live verification** (2026-04-11 evening, after ADR-025 Phase
7a) is the second piece proving the multi-user pipeline end-to-end,
this time at the infrastructure layer:

- Alembic migration `005_enable_rls_policies` applied cleanly at
  container restart (`Running upgrade e6f8a0c2d4e6 → a8b0c2d4e6f8`).
- `pg_class.relrowsecurity` + `relforcerowsecurity` confirmed true
  on all three tables (`interactions`, `sessions`, `tool_calls`).
- `pg_policies` shows `tenant_isolation_<table>` for each.
- Non-superuser `sovereign_app` role created via
  `scripts/init-sovereign-app-role.sh`, ownership of the three
  tables + `interactions_id_seq` transferred from `sovereign`.
- Memory-server restarted with the new `sovereign_app` connection
  URL; chat completion (`POST /v1/chat/completions`) returned 200
  and wrote interaction row #945 under `project=phase4-smoke`.
- **3-way RLS proof against the live DB:**
  - `sovereign_app` with no GUC set → `SELECT COUNT(*) FROM interactions` → **0 rows**
  - `sovereign_app` with GUC=SENTINEL_SUBJECT + `project='phase4-smoke'` → **1 row**
  - `sovereign` (superuser, bypasses RLS) → **945 rows**

The 0-row result with no GUC proves RLS is biting — without it,
the non-superuser would have seen all rows it owns. The 1-row
result with GUC matches the single row the chat completion just
wrote. The 945-row superuser count confirms the historical data is
still there, just filtered out of sovereign_app's view. Every
chat request now routes through a fully defense-in-depth multi-user
pipeline: Phase 2 service-layer filter + Phase 4 Postgres RLS +
Phase 4 ChromaDB wrapper.

### 16.3 What's NOT done yet

**Everything on the Option B path is done as of the end of
2026-04-11.** The follow-ups below are all Phase 8+ — nice-to-have
but not blocking the Accepted status of this ADR.

**Phase 8+ — OAuth2 device flow for human auth:**
- Realm config for a public device-flow client so OpenCode can
  prompt a human at first use and stash the resulting token
- Integration with Keycloak's `device_authorization_endpoint`
  (already exposed by the realm's OIDC discovery)
- OpenCode token configuration documentation once the human path
  is ready for daily use

This is the "real" multi-user — a human typing credentials into
Keycloak once and getting a JWT that sovereign-memory-server
validates on every subsequent OpenCode invocation. The Phase 7
dev client path (service-account via client-credentials grant) is
enough for curl + Bruno + ad-hoc dogfooding; real humans using
the stack daily want the device flow. Deferred as operator work.

**Phase 8+ — Continue / Roo Code / VSCode native auth:**
- Same JWT shape, different acquisition flow per client.
- VSCode native auth provider is its own ADR when it lands.

**Phase 8+ — JWE + introspection:**
- Opaque tokens + token introspection for higher-security deployments.
- Behind config flags, no architectural change.

**Phase 8+ — Admin UI for token management:**
- Self-service token issuance / rotation / revocation.
- Keycloak's admin console covers 80% of this out of the box;
  a custom UI is only needed if we want non-operator users to
  manage their own tokens.

**Async persistence of audit rows** (unrelated to multi-user but
tracked in brainstorm §12):
- `_persist_interaction` and `_flush_pending_tool_calls` are
  synchronous today; a DB hiccup blocks the chat response tail.
- Separate ADR when it becomes a real problem.

### 16.4 Pre-existing papercuts surfaced during the session

Not blocking, but documented so they don't come back and bite:

1. **`scripts/setup-secrets.sh` only prints env lines to stdout**
   instead of appending them to `.env`. This is why `sovereign-redis`
   was running `unhealthy` for days — nobody copy-pasted the line.
   Follow-up: make the script idempotently append to `.env`.
2. **OTel exporter to Langfuse via `host.docker.internal:3000`** is
   refused from inside the memory-server container. Cross-compose-
   network issue between `sovereign-ai-net` and `langfuse_default`.
   Not blocking the multi-user path but worth fixing before canary.
3. **`POSTGRES_USER` creates a Postgres superuser by default.**
   Discovered during Phase 4: the `sovereign` role in the docker-
   compose stack is a superuser, which means it bypasses RLS
   regardless of FORCE ROW LEVEL SECURITY. Fixed by introducing
   `sovereign_app` as a non-superuser role (commit `f0f88ce`) and
   connecting the memory-server as that role. This is a general
   Phase 4 finding relevant to anyone deploying a Postgres app
   that relies on RLS.

### 16.5 What actually shipped (Option B complete)

**Option B from the afternoon conversation landed in full on
2026-04-11 evening.** The original "Phase 4 → Phase 5 → Phase 7 →
Phase 6" sequence was executed with one small reordering (Phase 5
split into 5a test-consolidation first and 5b flag-flip last, so
Phase 7 could slot in between without blocking). Final sequence:

1. **Phase 4 — RLS + ChromaDB wrapper** (`fb005a0`, `f0f88ce`) —
   Postgres RLS policies, SQLAlchemy listener, UserScopedSemantic-
   Service thin wrapper, non-superuser sovereign_app role.
2. **Phase 4 follow-up — per-request wrapper wiring** (`ca0f58b`) —
   `get_context_builder(user)` returns a per-request builder with
   the semantic layer bound to the caller's UserContext.
3. **Phase 5a — consolidated `TestCrossUserIsolation`** (`e745b32`) —
   10 tests covering every layer end-to-end with alice + bob.
4. **Phase 7 — Keycloak dev client + mint-dev-jwt.sh** (`c0243e0`) —
   new `sovereign-memory-dev` client with client-secret auth +
   audience mapper, realm JSON mirrored, helper script wired for
   the `docker exec` pattern.
5. **Phase 5b — flip `SOVEREIGN_AUTH_REQUIRED=true`** (`c0243e0`) —
   merged into the Phase 7 commit because the two changes were
   coupled (flag flip requires the JWT source). Python test suite
   unaffected (conftest env-wipe keeps bypass mode for tests).
6. **Phase 6 — ADR-026 promotion** (this commit) — doc rename,
   status flip to Accepted, 19-file cross-ref update.

**One bug uncovered and fixed along the way** (captured as a
follow-up risk for future session starts):
`@log_call`'s `span.record_exception(e)` crashed with
`AttributeError` on `LangfuseSpan` objects and masked every
`HTTPException` as a 500. Invisible in bypass mode because
`require_user` never raised. Phase 7's first real "no JWT"
request tripped it. Fixed defensively with `hasattr` + try/except
in `logging_config.py`.

**Three independent live-verification moments** across the session
(the `feedback_live_verification.md` rule validated three times):

- ADR-025 Phase 7a: real Qwen3.5 model called `recall_decisions`,
  real ADR-009 content came back, audit row landed with correct
  FK and scope fields
- DESIGN §16 Phase 4 verification: 3-way RLS proof (no-GUC = 0
  rows, GUC=sentinel = 1 row, superuser = 945 rows) against
  the running sovereign-postgres
- DESIGN §16 Phase 7+5b verification: no-auth = 401, real
  Keycloak JWT = 200, interaction persists with Keycloak service
  account `sub` (not sentinel) in `interactions.user_id`

**Sequenced rationale** (for future readers who ask "why this order"):

1. **Phase 4 follow-up — per-request wrapper wiring** first, because
   it's small and closes the ChromaDB half of Phase 4 fully.
2. **Phase 5a — consolidated `TestCrossUserIsolation` class** next,
   because it's independent of Keycloak and gives us one authoritative
   test that every layer is isolated end-to-end.
3. **Phase 7 — Keycloak operator setup** before Phase 5b because
   the flag flip breaks existing workflows without a real JWT source.
4. **Phase 5b — flip `SOVEREIGN_AUTH_REQUIRED=true`** with the JWT
   minting helper from Phase 7 in hand.
5. **Phase 6 — promote this doc to ADR-026** once all of the above
   are green.

---

*End of §16 status snapshot. Everything above this section is either
original design text (§§1-13), the §14 transition log, the §15
Keycloak rewrite, or §15.10's phase table showing the journey. This
section is the one to read first when coming back to this doc after
any gap.*
