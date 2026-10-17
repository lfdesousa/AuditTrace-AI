# ADR-062: Five-Layer Memory Model — Four Per-User Isolated Layers + One Shared Corpus, with Audited Access

Date: 2026-08-04

## Status

Accepted (ratified 2026-08-04). Current-state section rewritten 2026-08-04
with verbatim code quotes (operator directive: "this is way too important").

Supersedes the layer-to-tier binding in **ADR-027 §6** and refines the
tiering in **ADR-027 §1–§2**. Extends **ADR-018** (four-layer memory) to a
named five-layer model. Builds on **ADR-025** (memory-as-tools), **ADR-026**
(multi-user identity + RLS) and **ADR-058** (owner-scoped audit rows).
Relates to SDLC-ADR-002 (Memory Curator) and the "corporate corpus" note.

Implementation is specified separately and gated:
`~/work/audittrace-private/specs/2026-08-04-SPEC-ADR-062-five-layer-memory.md`.
**No code lands until that spec passes the builder → reviewer loop.**

## Context

ADR-018 ported a four-layer memory model (`episodic`, `procedural`,
`conversational`, `semantic`). ADR-026 added per-user RLS. ADR-027 put the
object-storage layers on MinIO with a **two-tier** design (`memory-shared` +
`memory-private/{user_id}/`) and stated the intent that per-user isolation be
enforced by the mechanism native to each store. The design intent was that
the four layers be **per-user isolated**.

A 2026-08-04 review of the running code found the implementation diverged from
that intent in ways that matter for an audit-grade product, and that the
"shared knowledge" behaviour the fleet relies on is not a designed layer but
an emergent side effect. The current state is specified below, with code.

## Current-state specification (evidenced)

### CS-1 — Production runs `tools` mode

The chart forces tools mode in production, while the code default is the
legacy `inject` path:

```yaml
# charts/audittrace/values.yaml:174
    AUDITTRACE_MEMORY_MODE: "tools"
```
```python
# src/audittrace/config.py:190
    memory_mode: str = "inject"  # "inject" | "tools"
```

So in production, recall runs through the `recall_*` tools over the ChromaDB
vector collections. The shared episodic/procedural keyword-search path is used
only by the `inject`/`POST /context` aggregator, which does not run in prod.

### CS-2 — Vector recall is strictly per-user (this is #383)

`recall_decisions` / `recall_skills` / `recall_semantic` call
`ChromaSemanticService.search_page`, which builds the filter:

```python
# src/audittrace/services/semantic.py:508-510
        where: dict[str, Any] | None = None
        if not user_context.is_admin:
            where = {"user_id": user_context.user_id}
```

The identical construction guards the non-paginated `search()`
(`semantic.py:299-301`). **There is no shared / `$or` / global escape** in the
filter (grepped across `semantic.py`, `tools/`, `_memory_tool_loop.py`). A
non-admin recalls only rows stamped with its own `user_id`; an admin caller
gets `where = None` and reads all rows. `is_admin` is scope-driven
(`audittrace:admin` / `memory:admin` / `admin:*` — `identity.py:109-134`).

### CS-3 — The object-storage layers are shared; "sharing" today is an admin bypass

Episodic/procedural services read the shared bucket and drop the user context:

```python
# src/audittrace/services/episodic.py:224 (load), also :255, :289, :320
        del user_context  # shared content — not per-user scoped
```

The `decisions`/`skills` vectors are stamped with the **indexing principal's**
`user_id`, and that reindex is admin-gated:

```python
# src/audittrace/routes/memory.py:526  (metadata stamped per chunk)
                "user_id": user_id,
```
```python
# src/audittrace/routes/memory.py:613-614
    if file is None:
        _require_admin(user, "bulk /memory/index rebuild")
```

The in-code comment records why this key exists and the failure it fixed
(#374):

```python
# src/audittrace/routes/memory.py:519-525
                # Before 2026-07-21 this path set NO ownership key at all and
                # the PDF path set ``ingested_by_user_id``, so the filter
                # matched nothing and EVERY non-admin recall returned zero
                # results for EVERY collection. ...
```

**Consequence:** with recall filtering on strict `user_id` equality (CS-2) and
seed/org chunks stamped with the admin operator's id (or, via the legacy
`scripts/index-chromadb.py`, no `user_id` at all), a normal non-admin user's
`recall_*` returns the shared corpus **only if their id equals the indexer's**
— in practice, **only admins discover the seed corpus** (`where = None`).
Today's "shared knowledge" is therefore an **accidental admin bypass**, not a
designed shared-read layer.

### CS-4 — The backoffice REST read path bypasses the per-user filter

The backoffice list/read endpoints return whole-layer/whole-collection content
with no per-user predicate. `GET /memory/semantic` is the sharpest case — it
has **no `require_user` dependency at all**:

```python
# src/audittrace/routes/memory.py:1469-1490
@router.get("/semantic")
async def list_semantic(
    collection: str | None = Query(...),
    ...
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:read"]),
) -> dict[str, Any]:
    ...
```

The manifest that backs the episodic/procedural lists filters by layer only:

```python
# src/audittrace/services/memory_manifest.py:299 (list_for_layer)
            q = select(MemoryItem).filter_by(layer=layer)
```

And the manifest table is deliberately not per-user:

```python
# src/audittrace/db/models.py:198-200
    No RLS on this table — the manifest is operator-global, not
    per-user content. Access is gated by the per-layer write scope
    (``memory:<layer>:write``) at the route layer.
```

`read_decision` / `read_skill` similarly apply no user filter — they fetch by
exact filename from the shared bucket. **So the recall path filters per-user,
but the backoffice list/read path does not.** Discovery is isolated;
point-read and backoffice listing are not.

### CS-5 — The read scopes are default-granted to end-user clients

```json
// keycloak/realm-audittrace.json — audittrace-opencode defaultClientScopes (:336-343); audittrace-webui (:446-453)
      "defaultClientScopes": [
        ...
        "memory:episodic:read",
        "memory:procedural:read",
        "memory:conversational:read-own",
        "memory:semantic:read"
      ]
```

Combined with CS-4, **any ordinary end-user token can list/read the entire
shared corpus through the backoffice endpoints.** This is the "endpoints where
you can see the contents of the memory collections" concern. The reserved
SC-09 `audittrace-restricted` client is the exception (only `query` / `context`
/ `conversational:read-own`, `optionalClientScopes: []` — cannot be widened).

### CS-6 — Conversational layer is per-user (RLS); the private tier is dark

RLS is enforced on exactly the audit/interaction tables — the conversational
layer's store:

```python
# src/audittrace/migrations/versions/005_enable_rls_policies.py:49, 76-79
_RLS_TABLES: tuple[str, ...] = ("interactions", "sessions", "tool_calls")
...
            CREATE POLICY tenant_isolation_{table} ON {table}
                FOR ALL
                USING (user_id = current_setting('app.current_user_id', true))
                WITH CHECK (user_id = current_setting('app.current_user_id', true))
```

The per-user object tier is configured but never read/written at request time:

```python
# src/audittrace/config.py:175-176
    minio_shared_bucket: str = "memory-shared"
    minio_private_bucket: str = "memory-private"
```

`memory-private` is referenced only by `config.py` and `scripts/seed-memory.py`
— **no live service touches it** (ADR-027 §1/§2 designed it; §6 hard-binds the
services to `memory-shared`; §8's Phase-4 bucket policy never shipped).

### CS-7 — Delete is soft, but soft-delete and object removal are independent

`DELETE /memory/{layer}/{key}` soft-deletes via the manifest
(`record_delete` → `deleted_at_ms`); hard S3/Chroma removal runs only on
`?hard=true` + admin (`memory.py` delete handlers; `episodic.py:319` `delete()`
= real `remove_object`, also `del user_context`). The two operations are
independent, so a soft-deleted item keeps live S3 bytes and live vectors until
a separate hard delete is issued.

### Code ↔ ADR contradictions (recorded)

1. `memory-private` designed (ADR-027 §1/§2) but dark; §6 binds services to
   `memory-shared` (CS-6).
2. ADR-027 §8 Phase-4 `memory-private` bucket policy never shipped (CS-6).
3. The backoffice read path bypasses the per-user filter recall enforces
   (CS-4 vs CS-2).
4. Un-audited read surface vs ADR-058's "reading the recorder is recorded":
   the `GET /memory/*` handlers emit no first-class audit event.
5. The shared corpus is an unnamed fifth layer (CS-3).
6. ADR-026 §5 scope vocabulary (`memory:read`) is superseded by the shipped
   granular `memory:<layer>:read` names (`auth.py:81-99`).

## Decision

### §1. Adopt an explicit five-layer model

- **Layers 1–4 — per-user isolated** (`episodic`, `procedural`,
  `conversational`, `semantic`): a caller sees only its own content, isolated
  by the mechanism native to each store.
- **Layer 5 — the Shared Corpus**: org-wide knowledge (seed ADRs, framework
  skills, curated papers, sanctioned fleet lessons). **Shared-read,
  operator/curator-tier write.** Recalled through a collection whose query
  **deliberately omits** the per-user `where` (the mirror image of layers
  1–4), replacing today's accidental admin-bypass (CS-3) with intentional,
  scoped sharing. This names what `memory-shared` already is.

### §2. Isolation for layers 1–4 is per storage type (restating + wiring ADR-027 §2)

| Layer | Store | Isolation | State today |
|---|---|---|---|
| Conversational (sessions) | PostgreSQL | RLS on `app.current_user_id` | **live** (CS-6) |
| Semantic (vectors) | ChromaDB | `where={"user_id": ...}` on query + `user_id` on write | recall filter **live (#383, CS-2)**; backoffice bypass to close (CS-4); per-user source-doc tier Phase B |
| Episodic (per-user ADRs/notes) | MinIO | `memory-private/{jwt.sub}/episodic/` prefix | shared today (CS-3); to wire (Phase B) |
| Procedural (per-user skills) | MinIO | `memory-private/{jwt.sub}/procedural/` prefix | shared today (CS-3); to wire (Phase B) |

The `{user_id}` prefix is always derived from the validated JWT `sub` claim,
never from a request parameter (ADR-027 §1). Admin/operator identity may read
across prefixes (ADR-027 §2 admin row).

### §3. Wire the dark per-user tier

Services read/write **both** tiers and merge on recall:
- Per-user content → `memory-private/{uid}/…` (S3) / `user_id`-tagged vectors.
- Shared content → `memory-shared/…` / the shared corpus collection (Layer 5).
- Recall returns **per-user ∪ corpus**, each result carrying a `tier`
  provenance field (`private` | `corpus`). The default **write** tier for an
  ordinary caller is `private`; writing to Layer 5 requires the corpus scope
  in §4.

### §4. Name and govern Layer 5 (the Shared Corpus)

- Give it its **own scopes, granular per recall collection** (ratified review
  point 1) — `memory:corpus:<collection>:read` /
  `memory:corpus:<collection>:write`, `<collection>` ∈ `{decisions, skills,
  semantic}` — distinct from the per-user `memory:<layer>:*` scopes, mirroring
  the existing least-privilege naming. Granularity lets a papers-ingester hold
  only `memory:corpus:semantic:write` while the Curator holds `decisions` +
  `skills` write.
- **Write is operator/curator-tier**: least privilege, client-allowlisted. The
  reserved SC-09 `audittrace-restricted` client is never granted any corpus
  write. Closes the coarse-master-key gap; the scope-drift guard must learn the
  granular corpus scopes so an undeclared holder is flagged.
- Layer 5 is the **home of the Memory Curator's output** (SDLC-ADR-002): the
  Curator curates the corpus by its recall collections; it never touches
  per-user tiers.

### §5. Audit every memory access as a first-class event

Every read / list / write / delete on **any** layer — per-user and corpus —
emits an owner-scoped, trace-linked audit event in the ADR-058 shape,
reconstructable through `/interactions`. This closes contradiction 4 and
extends "record at the recorder" to the recorder's own knowledge base.

### §6. Soft-delete is the contract for Layer 5

The manifest soft-delete (`deleted_at_ms` / `deleted_by_user_id`) is
authoritative for corpus content; a hard `remove_object` must **not** be the
delete path for Layer 5. Resolve the independence noted in CS-7 so corpus
deletions stay recoverable and audited.

### §7. OpenAPI honesty

The published spec documents the five-layer model, which tier each endpoint
touches, the audit semantics of every memory operation, and **where recall
actually happens** (the `recall_*` tool loop and `POST /context`), not only the
backoffice CRUD. The Contract Sync gate is extended to keep this true. The MCP
entry-interface work is the structural end-state of this requirement.

### §8. Backward-compatibility and freeze compliance

- **OFF the frozen `/v1` default shape.** This touches the memory
  management/backoffice surface and internal storage tiering, not
  `/v1/chat/completions`. The audit-schema freeze (additive + backward-compat)
  is respected; memory-record and audit-event schemas evolve additively and
  versioned.
- **Additive migration, no destructive move.** Existing `memory-shared`
  content **stays** as Layer 5. New per-user writes go to `memory-private`. A
  one-time triage classifies genuinely-private vs genuinely-org-wide content.
- **Triage is automated and audited** (ratified review point 4): the
  classifier uses the Curator's sensitivity category-flags (SDLC-ADR-002
  taxonomy — `pii` / `pricing` / `counterparty-nonpublic` / `internal-id` /
  `security-fingerprint` / `public-safe`) to propose `private` vs `corpus`,
  and every classification and move emits an audit event (§5). Human review
  remains available for low-confidence cases.

### §9. Phasing (reviewable, low-risk first)

- **Phase A — governance floor (no tenancy change):** audit events on all
  memory reads/writes (§5); `require_user` on `GET /memory/semantic` (CS-4);
  granular `memory:corpus:*` scopes + operator-tier corpus write (§4);
  soft-delete enforcement for Layer 5 (§6); OpenAPI honesty (§7).
- **Phase B — per-user tiering:** wire `memory-private` read/write and the
  ChromaDB `user_id` filter for the source docs (§2–§3); recall merges
  private ∪ corpus with `tier` provenance; content triage (§8).
- **Phase C — defence in depth:** the deferred ADR-027 §8 bucket policy.

## Consequences

### Positive

- The original design intent (per-user isolation for layers 1–4) is restored
  and, for the first time, actually wired.
- The shared corpus is **named, scoped, operator-tier-written, and audited** —
  fleet cross-agent recall stops depending on running as admin or on identity
  coincidence (CS-3).
- **Reading the recorder becomes recorded** — closes contradiction 4 and
  strengthens the Art 12 / ADR-058 story the product sells.
- The Memory Curator scope becomes crisp (curates Layer 5 by its collections).
  Unblocks SDLC-ADR-002.
- Reconciles the "corporate corpus / fifth layer" intent — present, not
  deferred.

### Negative

- Real, multi-PR implementation (audit events across every memory route,
  `memory-private` + Chroma `user_id` wiring, corpus scopes, soft-delete,
  OpenAPI + Contract Sync).
- Recall grows a merge step (private ∪ corpus) and a provenance field.
- One-time content triage.

### Neutral

- In today's single-operator reality, per-user isolation is latent until a
  second real tenant exists — but wiring it now stops the drift compounding.
- Cross-references: scope-drift governance (guard must learn corpus scopes),
  the MCP entry-interface, SDLC-ADR-002 (Curator), ingest reliability (must
  place content into the correct tier).

## Decisions on review points (ratified 2026-08-04)

1. **Corpus scope granularity — GRANULAR.** Per-collection scopes
   `memory:corpus:<collection>:{read,write}` (§4).
2. **Default write tier — `private` unless promoted** (§3/§8). The structural
   fix for "strategic content on the memory server": it lands private by
   default and reaches Layer 5 only by explicit, scoped promotion.
3. **Per-user semantic vectors — Phase B** (§2/§9).
4. **Triage — automated with sensitivity tags, and audited** (§8).
