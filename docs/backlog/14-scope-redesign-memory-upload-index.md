---
title: "auth: scope redesign for `/memory/upload` + `/memory/index` (admin → per-layer write)"
labels: ["auth", "scopes", "design", "m3", "deferred"]
priority: P3
---

## Context

`POST /memory/upload` (`src/audittrace/routes/memory.py:177`) and
`POST /memory/index` (`src/audittrace/routes/memory.py:821`) both
gate on `audittrace:admin`:

```python
_auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
```

Every other write route in the file uses the per-layer scope
(`memory:episodic:write`, `memory:procedural:write`,
`memory:semantic:write`). The `:admin` requirement on the two
file-ingestion endpoints is a leftover from when these routes were
operator-only.

### Why this matters

The natural M3 LibreChat user-flow is:

1. End user logs into LibreChat → gets a token with
   `memory:episodic:write` (per-user, narrow).
2. End user uploads a PDF through the UI →
   `POST /memory/upload?layer=episodic`.
3. Backend triggers indexing →
   `POST /memory/index?file=episodic/<key>`.

Both calls fail today with `403 Forbidden` unless the UI session
holds an admin token — which is the wrong design (broad blast radius
on every user session) and which `feedback_no_unauthorized_testing_paths`
explicitly rules out as a workaround.

## Why this is deferred (not P1/P2)

Per `project_pickup_20260508`: M3 LibreChat Day-1 is backend
plumbing only and ADR-048 (Proposed) blocks external user PDF uploads
entirely until the v1 scanner pod ships. Until M3 LibreChat
integration actually wires UI → `/memory/upload`, the admin-only
posture is the right default (least privilege over feature surface).

This issue captures the redesign so it is filed and not lost, but
should not be picked up before:

- M3 LibreChat Day-1 has merged, AND
- ADR-048 v1 (content-control gate + scanner pod) is in review or
  shipped, AND
- The exact UI flow is decided (does LibreChat call
  `/memory/upload` then `/memory/index`, or a single fused
  endpoint?). The fused-endpoint option may be the right answer
  and would change this issue's scope materially.

## Fix sketch

### Primary — accept `memory:<layer>:write` matching the layer query param

Per route handler, validate that the JWT carries
`memory:{layer}:write` for the layer being uploaded to or indexed:

```python
# Inside upload_memory_file, after parsing `layer`:
required = f"memory:{layer.value}:write"
if required not in user.scopes and not user.is_admin:
    raise HTTPException(status_code=403, detail=f"missing scope {required}")
```

Drop the static `Security(..., scopes=["audittrace:admin"])` decorator
because FastAPI's `Security` resolves before the layer is parsed. The
scope check has to be runtime against the parsed query param.

`audittrace:admin` continues to bypass per-layer checks (it already
does in the per-layer endpoints — see lines 1239, 1380, 1628). That
preserves the operator's bulk-rebuild path through `/memory/index`
without `?file=...`.

### Secondary — split `/memory/index` modes by scope

Two distinct operations have collapsed under one endpoint:

- **Bulk delete-and-rebuild** (no `?file=` param) — destructive,
  whole-collection. Keep `audittrace:admin`.
- **Single-file upsert** (`?file=<key>`) — additive, per-document.
  Per-layer write is sufficient.

The route already branches on `file is None` for behaviour; have the
scope check branch too.

### Tertiary — fused upload-then-index endpoint

Per `project_m3_librechat_split.md`, the M3 Day-1 contract may want
a single `POST /memory/upload?layer=episodic&index=true` that runs
upload + index in one call (atomic from the UI's perspective; both
fail or both succeed). If that is the chosen UX, this issue
collapses to "the fused endpoint requires `memory:{layer}:write`,
period." Worth deciding before doing the split-route work.

## Acceptance

- A token with **only** `memory:episodic:write` can call
  `POST /memory/upload?layer=episodic` and
  `POST /memory/index?file=episodic/<key>` end-to-end.
- A token with `memory:episodic:write` cannot call
  `POST /memory/index` (bulk mode, no `?file=`) — that path stays
  admin-only.
- A token with `memory:procedural:write` cannot upload to
  `?layer=episodic` (cross-layer denied).
- An admin token retains today's behaviour (any layer, any mode).
- Live evidence: capture all four cases against the deployed image,
  per `feedback_test_and_evidence`. ChromaDB row + audit-row reference
  in PR body.
- AGENTS.md and `docs/guides/deployment-runbook.md` updated to show
  the per-layer scope as the recommended flow.

## Cross-references

- `project_pre_ui_critical_inventory.md §1` — flagged this as a
  hard prereq for end-user UI uploads.
- `project_m3_librechat_split.md` — Day-1 / Day-2 split that will
  drive the final UX choice (separate vs fused endpoints).
- `feedback_no_unauthorized_testing_paths.md` — closes the door on
  "use admin token to make it work" workarounds during M3
  development; this issue's existence is the proper answer.
- `docs/ADR-048-…md` (Proposed) — content-control gate; this scope
  redesign is meaningless until ADR-048 v1 lands.
- ADR-032 — the per-layer scope model these routes should align to.
- `src/audittrace/routes/memory.py:182, :835` — the two `audittrace:admin`
  decorators this issue targets.
