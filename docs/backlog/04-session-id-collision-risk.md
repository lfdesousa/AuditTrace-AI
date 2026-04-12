---
title: "feat(chat): widen _compute_session_id hash to mitigate collision risk"
labels: ["enhancement", "tech-debt"]
priority: P4
---

## Context

`src/sovereign_memory/routes/chat.py:_compute_session_id` truncates a
SHA-256 digest to its first 16 hex characters (= 64 bits of entropy):

```python
h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
return f"{source}-{today}-{h}"
```

64 bits gives birthday-paradox collision probability of ~1 in 4 billion at
1k sessions/day, but the chance climbs steeply at high volumes. Across the
medium-term audit table this is unlikely to bite, but it's not zero, and a
collision silently merges two unrelated conversations into one
`session_id` — a confusing failure mode in Langfuse and Postgres.

## Fix options

1. **Widen to 32 hex chars (128 bits)** — trivially safe, doubles the column
   width but still well under Postgres TEXT limits.
2. **Use `uuid4()` instead of a hash** — non-deterministic, breaks the
   "same source + same day + same first message → same session" property
   that lets re-tries reattach to the same conversation.
3. **Keep 64 bits + add a uniqueness constraint** with a fallback re-hash
   on collision. Most complex, least gain.

Recommendation: option 1. Determinism is the load-bearing property and
collision resistance is the only thing being traded off.

## Acceptance criteria

- `_compute_session_id` returns hashes ≥ 32 hex chars
- Existing `TestSessionId` tests updated for the new length
- No migration needed — old session IDs remain valid (they're TEXT)
