# ADR-060: Record memory recall by durable id, including cache hits

- Status: Accepted
- Date: 2026-07-26
- Deciders: Luis de Sousa
- Supersedes on one point: ADR-025 §Decision.8 (cache hits are now audited)

## Context

The memory-recall tools (`recall_semantic`, `recall_decisions`, `recall_skills`)
returned matches identified only by their **content** (title + snippet). The recorded
`tool_calls.result_summary` therefore could not identify **which** memory shaped an
answer — only string-match its text. For an audit layer whose job is reconstructability,
"the memory that shaped this answer cannot be identified" is a real gap.

Two surfaces were affected:

1. **Vector recall** (`recall_semantic` → ChromaDB): `ChromaSemanticService.search`
   dropped the ChromaDB id on read even though it is the stable `document_id` supplied at
   upsert.
2. **Cache hits**: `_memory_tool_loop` deliberately recorded **no** audit row on a cache
   hit (ADR-025 §Decision.8, "no write-side side effects"). Correct for write-auditing,
   but it meant a cache-served recall shaped an interaction's answer while that
   interaction's record showed nothing — reconstructability broken via the cache path.

## Decision

1. Thread the ChromaDB `chunk_id` (= the upsert `document_id`) and the query `distance`
   through `search` into each recalled `Document`'s metadata. Surface, on every recall
   match, a durable identity: `id` (chunk id, or the `file` for the S3 keyword layers,
   never blank), a stable `source_ref` (`file`→`source`→`title`), `sha256` when present,
   and `distance` (raw ChromaDB distance, lower = closer; honestly named, not a
   similarity).
2. Record a **cache hit** as a lightweight read-audit `tool_calls` row too — carrying the
   served ids and flagged `cache_hit: true` inside `result_summary` (no schema migration;
   the tool_calls table has no cache_hit column). The ADR-025 no-write-side-effects claim
   still holds; only per-interaction reconstructability now requires the row.

Additive only: the `/v1/chat/completions` default shape and the ChromaDB write path are
untouched; the per-user `where` isolation is preserved.

## Consequences

- Given any interaction, its recorded recall rows now name the exact memory (by id +
  stable source_ref) that shaped the answer, with a relevance distance, and truthfully
  mark whether the read was fresh or cache-served.
- Reconstruction resolves: `interaction → tool_calls (recall_*) → recorded id`.

## Evidence

Live, front-door (scoped JWT), on the deployed branch build:
- **Vector recall recorded by id** — interaction 770 `recall_semantic` rows carry
  `id`/`source_ref`/`distance` (e.g. `dab221b4d57e9d43`, ADR-053…, distance 1.0).
- **Cache hit now recorded** — a cold call (interaction 772) records the row with no
  flag (duration 132 ms); the identical warm call (interaction 773) records the row
  flagged `cache_hit: true` carrying the **same** id `177f5e3e44c58502`
  (ADR-047-server-side-embedding.md, duration 2 ms). Before this change the warm call
  recorded zero tool-calls.

Verification: `make test` 1652 passed, per-file coverage gate PASS (lines + branches),
zero-skip. Two independent adversarial reviews (ACCEPT).
