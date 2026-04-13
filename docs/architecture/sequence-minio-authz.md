# Sequence Diagram: MinIO AuthZ — Shared vs Private Content (ADR-027)

Two distinct access paths based on content tier. The memory-server mediates
all MinIO access — end-user JWTs never reach MinIO. Per-user isolation is
enforced by path prefix derived from the validated JWT `sub` claim.

## Shared Content (memory-shared bucket)

ADRs and skills are organisation-wide — any authenticated user can read.
No user_id prefix applied.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Proxy as memory-server
    participant KC as Keycloak
    participant MinIO as MinIO (memory-shared)

    Agent->>Proxy: "what ADRs exist?" (Bearer JWT)

    Proxy->>KC: validate JWT (JWKS cached)
    KC-->>Proxy: UserContext(user_id="kc-luis-001", is_admin=true)

    Note over Proxy: Shared content path -<br/>no user_id prefix

    Proxy->>MinIO: list_objects("memory-shared", prefix="episodic/ADR-")
    MinIO-->>Proxy: [episodic/ADR-014.md, episodic/ADR-018.md, ...]

    Proxy->>MinIO: get_object("memory-shared", "episodic/ADR-018.md")
    MinIO-->>Proxy: ADR content (decrypted from SSE-S3)

    Proxy-->>Agent: recall_decisions result
```

## Private Content (memory-private bucket)

Research papers and coursework are per-user. Path prefix enforced
from JWT `sub` claim — user John cannot read Maria's objects.

```mermaid
sequenceDiagram
    participant Agent as Coding Agent
    participant Proxy as memory-server
    participant KC as Keycloak
    participant MinIO as MinIO (memory-private)

    Agent->>Proxy: "search my research papers" (Bearer JWT)

    Proxy->>KC: validate JWT (JWKS cached)
    KC-->>Proxy: UserContext(user_id="kc-luis-001", is_admin=false)

    Note over Proxy: Private content path -<br/>prefix = user_context.user_id

    Proxy->>MinIO: list_objects("memory-private", prefix="kc-luis-001/ai-research/")
    MinIO-->>Proxy: [kc-luis-001/ai-research/main.pdf, ...]

    Note over Proxy: User kc-maria-002 would get<br/>prefix="kc-maria-002/ai-research/"<br/>and see only her own files

    Proxy-->>Agent: recall_semantic result (user-scoped)
```

## Admin Access

Admins bypass the prefix constraint — consistent with PostgreSQL RLS
admin behaviour and `UserScopedSemanticService`.

```mermaid
sequenceDiagram
    participant Admin as Admin Agent
    participant Proxy as memory-server
    participant KC as Keycloak
    participant MinIO as MinIO (memory-private)

    Admin->>Proxy: "list all users' research" (Bearer JWT)

    Proxy->>KC: validate JWT (JWKS cached)
    KC-->>Proxy: UserContext(user_id="kc-admin", is_admin=true)

    Note over Proxy: Admin path -<br/>no prefix restriction

    Proxy->>MinIO: list_objects("memory-private", prefix="")
    MinIO-->>Proxy: [kc-luis-001/..., kc-maria-002/..., ...]

    Proxy-->>Admin: all users' content visible
```

## Isolation Model Comparison

| Layer | Service | Shared | Per-user mechanism |
|-------|---------|--------|-------------------|
| PostgreSQL | RLS | N/A | `set_config('app.current_user_id', sub)` |
| ChromaDB | SemanticService | `decisions`, `skills` | `where={"user_id": ...}` |
| MinIO | S3*Service | `memory-shared` bucket | `{user_id}/` path prefix |
