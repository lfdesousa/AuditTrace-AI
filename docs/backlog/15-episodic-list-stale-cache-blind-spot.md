# 15 — Episodic/procedural list serves a stale load-cache: a doc readable by key is invisible in the list (+ list ergonomics: sort/order/limit)

> Status: SPEC (2026-07-25). Public-repo change → goes through the loop
> (spec → builder → independent reviewer) + full `make test` + the ADR-049
> evidence gate. Found while logging the #373 decision docs to the memory server:
> `POST /memory/upload` returned 200 and `GET /memory/episodic/{fn}` returned 200,
> but the doc did **not** appear in `GET /memory/episodic` (list). "I do not like
> blind spots" — Luis, 2026-07-25.

## Symptom

Upload six docs via `POST /memory/upload?layer=episodic` → all 200. Each is then
retrievable by key: `GET /memory/episodic/decision-2026-07-24-...md` → 200. But
`GET /memory/episodic` returns `total: 143` and **none of the six**. A record you
can write and read by key is invisible to enumeration — an audit product must be
able to faithfully list what it holds, so this is thematically load-bearing, not
just a UI nit.

## Root cause — THREE distinct defects (traced in code, not guessed)

`GET /memory/episodic` (`routes/memory.py::list_episodic`) builds its list from
`_merge_layer_items_with_s3("episodic", manifest_rows, user)`, which starts from
manifest rows and appends "discovered" S3 objects (those with no manifest row) by
walking `S3EpisodicService.load(user)`. Three separate defects converge here; the
symptom (uploaded `decision-*.md` readable by key but absent from the list) is
caused primarily by **Defect A**, and any fix that ignores **Defect B** is a FALSE
fix in the deployed topology.

**Defect A — the bulk loader hard-filters to `ADR-*.md` (the dominant cause).**
`S3EpisodicService._load_from_s3` (`services/episodic.py` ~line 133):
```python
if not filename.startswith("ADR-") or not filename.endswith(".md"):
    continue
```
So `load()` — which backs the list's S3-discovery — only ever returns objects
named `ADR-*.md`. Our `decision-2026-07-24-*.md` (and any non-ADR episodic object
uploaded via `/memory/upload`) is skipped ENTIRELY, cache-fresh or not. Meanwhile:
- `read()` (`services/episodic.py` ~185) fetches by exact key with NO prefix filter
  → single-key GET works for `decision-*.md`.
- the `/memory/index` seed path lists the whole `episodic/` prefix via
  `_list_objects_from_minio(..., "episodic/")` with NO `ADR-` filter → the doc IS
  embedded into ChromaDB and is recallable.
Result: THREE code paths give THREE different views of the same bucket
(read-by-key: all; ChromaDB index: all; list/`load()`: `ADR-*` only). An audit
product that cannot consistently enumerate its own store across its own read paths
is the real defect. (The single `decision-2026-07-23` doc that DID appear in the
list is present via a **manifest row** from a prior session — the manifest path
bypasses this filter.)

**Defect B — the cache is PER-PROCESS and the deployment runs 3 replicas
(invalidate-on-write is insufficient).** The `self._cache` is an instance field on
the per-process singleton `S3EpisodicService` (`services/episodic.py:122`,
`dependencies.py:284`); its docstring says *"Cache is per-process lifetime."* The
chart runs **`replicaCount: 3`** (`charts/audittrace/values.yaml:57`). So a cache
invalidation triggered by a write hits ONLY the pod that served the write; the
other two pods keep serving a stale `load()` until they restart. A list request
load-balanced to a different pod still misses the doc. **Therefore
invalidate-on-write alone does NOT fix this at replicaCount=3** — the list path
must be coherent across pods by construction (read fresh from the shared S3), not
by hoping every replica's local cache was invalidated. Cross-pod invalidation would
need a shared signal (Redis pub/sub) we do not have and should not add for a cold
enumeration path.

**Defect C — `/memory/upload` violates the cache-invalidation contract.** The
upload route writes S3 via the MinIO client but never calls
`episodic_service.invalidate_cache()`, violating the ABC contract
(`services/episodic.py`: *"Implementations that mutate S3 outside the cache MUST
call invalidate_cache() … Required after any write/delete so changes propagate
without a pod restart."*). Real, but only bites `ADR-*.md` uploads (Defect A hides
it for everything else), and even fixing it is insufficient alone (Defect B).

**Ergonomics defect — no timestamp on discovered entries.** Discovered entries are
emitted with `created_at_ms: None` / `modified_at_ms: None` (only manifest rows
carry timestamps). So the list cannot be recency-sorted and any sort over the
merged set trips on `None` — which is why Luis's sort-direction ask needs
per-object timestamps first.

## Requirements

R1. **Fix Defect A — enumeration must reflect ALL episodic objects, not just
    `ADR-*.md` (the primary fix).** The layer holds decisions, session docs, papers,
    and ADRs — not only ADRs. The bulk loader / discovery path that backs the list
    must include every `.md` object under the layer prefix (decide the exact rule;
    at minimum drop the `startswith("ADR-")` gate). The invariant to establish and
    TEST: the set of objects visible to `read()` (by key), to the `/memory/index`
    prefix walk, and to `GET /memory/{layer}` (list) must be THE SAME set — no code
    path may hold a stricter view of "what an episodic object is" than another.
    Consider extracting a single shared "list layer objects" helper so the three
    paths cannot diverge again.

R2. **Fix Defect B — replace the per-process cache with a SHARED cache behind an
    abstract class (Luis, 2026-07-25).** The per-process `self._cache` is an
    inconsistency/bug generator at `replicaCount: 3` — each pod caches independently,
    so no local invalidation can keep the fleet coherent. Replace it with a shared
    cache accessed through a small **cache ABC** (e.g. `LayerCacheStore` with
    `get(key)` / `set(key, value, ttl)` / `invalidate(key)`), mirroring the existing
    `EpisodicService`/`ProceduralService` ABC pattern. Backends:
    - **Redis (production)** — the cache is SHARED across all 3 replicas, so an
      invalidation clears it once for the whole fleet. Redis is already deployed
      (`charts/audittrace/values.yaml:479`) and configured (`config.redis_url` /
      `redis_password`, `config.py:126`); reuse the existing Redis patterns — the
      Redis-backed token cache (DESIGN §15.4), the Redis tool-result cache
      (§Decision.8), and `services/async_persist.py` (redis.asyncio) — for client
      wiring, auth, and failure handling (fail-open to a fresh S3 read on Redis
      error; never fail the request).
    - **Fake/in-memory (tests)** — a `fakeredis`-backed (already a test dep,
      `pyproject.toml`) or trivial dict impl behind the SAME ABC, so the cache is
      unit-testable without a live Redis.
    The episodic/procedural services read/write their S3 listing THROUGH this shared
    cache. Include a **TTL** as a self-healing safety net so a missed invalidation
    cannot pin staleness forever. Result: coherent across pods AND still cached.

R3. **Invalidate the shared cache on every mutation (now fleet-wide correct).**
    `POST /memory/upload`, the `/memory/index` seed path, and `write()`/`delete()`
    all call `cache.invalidate(<layer-list-key>)` on success. Because the cache is
    shared (R2), one invalidation is seen by all replicas — so this becomes the
    PRIMARY correctness mechanism (not the defense-in-depth it would be for a
    per-process cache), honouring the ABC contract fleet-wide.

R4. **Discovered entries carry real timestamps.** Populate `created_at_ms` /
    `modified_at_ms` for discovered S3 entries from the object's storage
    `last_modified` (MinIO stat), so the merged set is uniformly sortable and never
    yields `None` timestamps. Manifest rows still take precedence (sub-second,
    authorship). `size_bytes` already comes from content.

R5. **List ergonomics — sort / order / limit / offset (Luis's ask).** Add query
    params to `GET /memory/episodic` and `GET /memory/procedural` (and, for
    consistency, the other layer lists where it is cheap):
    - `sort` — one of `created_at` (default) | `modified_at` | `key` | `size`.
    - `order` — `asc` | `desc`, **default `desc`** (most-recent-first, the common
      operator need). Sorting must be stable and total-order safe over the merged
      manifest+discovered set (no `None` comparisons — guaranteed by R4, plus a
      defensive key that never compares `None`).
    - `limit` — page size (sensible default e.g. 100, hard max e.g. 500).
    - `offset` — zero-based pagination; response keeps `total` = full count (pre-limit)
      so the client can page. Consider returning `limit`/`offset` echoed back.
    Keep the OpenAPI schema additive (new optional params only) — no breaking change
    to existing callers ([[feedback_openai_schema_inviolate]] spirit for /memory).

R6. **Blind-spot regression guards.** (a) Upload a **non-`ADR-` named** doc (e.g.
    `decision-*.md`) then assert it appears in `GET /memory/{layer}` — this is the
    exact case that regressed, and it catches Defect A. (b) A three-view consistency
    test: an object present in S3 is enumerable by the list AND fetchable by `read()`
    AND (where indexed) recallable — the views must agree. (c) `order=desc` returns
    newest first; `limit`/`offset` paging; a mixed manifest+discovered set sorts
    without error. (Cross-pod staleness can't be unit-tested with 3 pods, but R2's
    fresh-read design removes the hazard — assert the list path does NOT consult the
    cache.)

R7. **Parity for procedural + check the augmentation blast radius.** Apply R1–R6 to
    the procedural layer (same `_merge_layer_items_with_s3` + cache + prefix-filter
    pattern). AND verify whether the chat/RAG augmentation path uses episodic
    `load()`: if it does, Defect A means non-`ADR-` episodic content (decisions,
    sessions) has been silently EXCLUDED from prompt augmentation via that path
    (recall_semantic/ChromaDB is unaffected). If confirmed, note it as a linked
    finding — it changes what "the model saw" and is audit-relevant.

## Acceptance (ADR-049 gate — this is product code)

- **Verification:** `make test` green, per-file ≥90 %, zero-skip; new tests for
  coherence (R4), timestamps (R2), and sort/pagination (R3).
- **Validation:** through the Istio front door with a scoped JWT
  (`memory:episodic:read` / `:write`): upload a doc, `GET /memory/episodic`, assert
  it appears; `GET /memory/episodic?order=desc&limit=5` returns the newest five,
  newest first. Capture request/response + the deployed image tag.
- **Reconstruction:** capture the before/after list response (doc absent → present)
  and a sorted page, referenced from the PR body.
- **Discipline:** front-door scoped-JWT only; through the builder→independent-reviewer
  loop; recall + log decisions to the memory server.

## Out of scope

- Redesigning the manifest model or the upload/index scope split (that is backlog
  #14).
- The ChromaDB recall path (separate concern — recall reads its own collection and
  is not affected by this list cache).
- Auth/scope changes.

## Note

This is the audit product's own "enumeration completeness" gap — the recorder must
be able to list what it holds. Worth a sentence in the Frank/Roche completeness
thread (resonates with "queryable is not auditable": here, *stored* was not even
*enumerable*), and a candidate mention in the exaggeration-resistance / cadence
work (#337/#338).
