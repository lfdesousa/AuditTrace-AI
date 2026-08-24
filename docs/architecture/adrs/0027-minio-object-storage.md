# ADR-027 — MinIO object storage (episodic + procedural layers)

**Status:** Accepted

Canonical decision record: [`docs/ADR-027-minio-object-storage.md`](../../ADR-027-minio-object-storage.md)
(this file is a naming-convention pointer only, kept for the Structurizr `!adrs` browser —
`NNNN-title.md`, four-digit — never a duplicate; edit the canonical file, not this one).

MinIO (S3-compatible) is the always-on backing store for the episodic (layer 1, ADR
decisions) and procedural (layer 2, skills) memory layers — no filesystem fallback. The
2026-08-22 retention amendment makes this **explicit no-expiry**: these objects are the
durable record, never auto-deleted, distinct from the ephemeral observability signals
(ADR-028).
