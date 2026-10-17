"""AuditTrace Memory Curator — deterministic curation runner package (SDLC-ADR-002).

The Curator curates the fleet's memory-server writes AFTER the fact (curation-only;
it does not gate the write path — builders/reviewers/deployers keep writing directly,
[[project_agents_on_memory_server_self_improve]]).

**Curates the recall COLLECTIONS, not physical layers** (2026-08-03 §CORRECTION):
``decisions`` (vector index over episodic ADRs/decisions), ``skills`` (index over
procedural), ``semantic``. There is no ``lessons`` collection anywhere in ``src/`` —
a v1 phantom, dropped for good. See :mod:`scripts.curator.runner` module docstring.

The **sensitivity taxonomy lives HERE**, in the package root, as the SINGLE shared
vocabulary. Both the Curator's tagging step (:mod:`scripts.curator.runner`) and any
downstream public-egress guard import these names, so a record's sensitivity tag and
the guard that would stop it at the public boundary can never DRIFT apart
(SPEC §Taxonomy build-note). Adding a flag in two places by hand is exactly the drift
this module exists to prevent — there is one definition and everything imports it.

Post-2026-08-05 AMENDMENT, the taxonomy does DOUBLE DUTY (ADR-062 §4): it is still
the egress-guard vocabulary AND it now drives PRIVATE→CORPUS tier-placement triage
(:func:`scripts.curator.runner.promotion_eligible`) — ``public-safe`` is eligible for
promotion, any other flag means the record MUST STAY PRIVATE.
"""

from __future__ import annotations

# Sensitivity CATEGORY FLAGS — each MIRRORS an existing public-egress guard so a
# record's tag and the guard that would stop it speak ONE language (SPEC §Taxonomy).
FLAG_PII = "pii"
FLAG_PRICING = "pricing"
FLAG_COUNTERPARTY_NONPUBLIC = "counterparty-nonpublic"
FLAG_INTERNAL_ID = "internal-id"
FLAG_SECURITY_FINGERPRINT = "security-fingerprint"
FLAG_PUBLIC_SAFE = "public-safe"

# flag -> the egress guard it mirrors (``None`` == egress-clear, the default).
SENSITIVITY_GUARD: dict[str, str | None] = {
    FLAG_PII: "no-private-content-hook",
    FLAG_PRICING: "no-private-content-hook",
    FLAG_COUNTERPARTY_NONPUBLIC: "no-private-content-hook",
    FLAG_INTERNAL_ID: "forbidden-patterns.txt",
    FLAG_SECURITY_FINGERPRINT: "gitleaks",
    FLAG_PUBLIC_SAFE: None,
}

# The whole flag vocabulary, canonical order (``public-safe`` is the default, last).
SENSITIVITY_FLAGS: tuple[str, ...] = tuple(SENSITIVITY_GUARD)

# The SENSITIVE (non-default) flags, in canonical order — the tagger's key set.
SENSITIVE_FLAGS: tuple[str, ...] = tuple(
    flag for flag in SENSITIVITY_FLAGS if flag != FLAG_PUBLIC_SAFE
)

# The recall COLLECTIONS the Curator curates (2026-08-03 §CORRECTION). These are
# ChromaDB vector collections, NOT the physical S3 layers (``episodic``/
# ``procedural``) — ``decisions`` indexes episodic ADRs, ``skills`` indexes
# procedural docs, ``semantic`` is the general RAG corpus. Matches
# ``_KNOWN_COLLECTIONS`` minus the opt-in ``ai_research_papers`` PDF collection
# (routes/memory.py) and the ADR-062 §4 granular corpus-scope frozenset
# (``memory:corpus:<collection>:{read,write}``) exactly.
RECALL_COLLECTIONS: tuple[str, ...] = ("decisions", "skills", "semantic")

__all__ = [
    "FLAG_PII",
    "FLAG_PRICING",
    "FLAG_COUNTERPARTY_NONPUBLIC",
    "FLAG_INTERNAL_ID",
    "FLAG_SECURITY_FINGERPRINT",
    "FLAG_PUBLIC_SAFE",
    "SENSITIVITY_GUARD",
    "SENSITIVITY_FLAGS",
    "SENSITIVE_FLAGS",
    "RECALL_COLLECTIONS",
]
