#!/usr/bin/env python3
"""Recall@k harness for WU-1 of ``2026-09-17-SPEC-memory-retrieval-and-
learning-loop-telemetry`` (closes D15) — **the acceptance-criterion
measurement, committed** (WU-1 fix-round-2, F3).

The spec's own words: *"Report recall@k BEFORE the change and AFTER, same
set, same queries. The before-number is part of the deliverable — without
it we cannot claim improvement, only assert it."* Fix-round-1 measured
this but did not commit the harness or the labelled set, so the headline
numbers rested on the builder's word alone — exactly what that criterion
exists to prevent (round-1 reviewer finding F3). This script is the fix:
re-running it against the live front door reproduces the numbers from
first principles, with no builder-supplied input beyond the labelled set
below.

**What "query" means here.** WU-2 (ranking) is explicitly out of scope for
WU-1 — `scripts/deploy/memory.py`'s own docstring says the ``query``
argument is accepted and discarded; `/memory/semantic` orders results by
recency only. So a "query" in the labelled set below is a human-readable
description of the dispatch intent (for traceability — which real lesson
was a brief trying to recall), NOT a string matched against document
content: the actual recall@k check is purely positional — "is the
EXPECTED document among the first k rows of the recency-ordered list?" —
identical to the methodology both the round-1 builder and the round-1
reviewer independently used and cross-checked (round-1 REJECT verdict,
F3: "the mechanism claim is fully reproduced").

**BEFORE vs AFTER, precisely (WU-1 fix-round-3, F18 correction).** Both
views are computed from the SAME `chunk_rows` AFTER
``dedupe_semantic_chunks`` (the cross-path physical-id dedup) has already
been applied once, in :func:`run`, below — that dedup is itself a WU-1
addition, so BEFORE here is **"the raw per-chunk view with the new
cross-path dedup applied, but WITHOUT document-grouping"**, not a literal
byte-for-byte replay of what the pre-WU-1 server's HTTP response
contained. Applying the SAME first-stage dedup to both sides is
deliberate — it isolates the one variable this WU changed (grouping),
rather than conflating it with a second, unrelated defect this WU also
happened to fix — but it means the BEFORE column is not exactly "the
pre-WU-1 shape" and this file no longer claims that it is.

* **BEFORE** — the deduped per-chunk view (`granularity=chunk`), first
  *k* CHUNK rows in recency order. A document "hits" at *k* if ANY of its
  chunks appears in the first *k* chunk rows.
* **AFTER** — the SAME deduped rows run through the shipped
  ``group_semantic_chunks_by_document`` (the real function ``list_semantic``
  calls, imported here — not reimplemented), first *k* DOCUMENT rows in
  recency order.

**AFTER is at least as good as BEFORE at every measured k, not
"strictly" better at every k** (WU-1 fix-round-3, F19 correction): at
k=4, both are typically 0.00% (too small a window for either view to
surface a labelled document) — the mechanism only shows a measurable
advantage from around k=25 upward, where document-grouping stops
"spending" the window on multiple chunks of the same few large records.
Report the actual numbers; do not round that up to "dominates at every
k".

**The labelled set** (34 pairs, >= the spec's "at least 30") is drawn from
real, indexed decisions-layer documents from the last ~10 days of fleet
history (lessons + three ratified addenda) — "actual dispatch briefs" per
the spec. Presence in the live corpus is verified before scoring; a pair
whose expected title is not found in the fetched corpus is reported as
``MISSING`` (excluded from the recall denominator) rather than silently
scored as a miss, since an absent document is a corpus-drift artefact,
not a retrieval defect.

**Egress discipline (SDLC-ADR-004):** reuses
``scripts.deploy.memory``'s single monkeypatchable HTTP seam and
in-process token resolution — the SAME mechanism ``recall_deploy_lessons``
uses. No bearer token ever touches argv, a file, or stdout.

Usage::

    AUDITTRACE_FRONT_DOOR=https://audittrace.local .venv/bin/python \\
        scripts/eval-recall-at-k.py [--collection decisions] [--insecure]

Deliberately NOT part of the coverage-gated ``scripts/`` subset (see
``pyproject.toml``'s ``[tool.coverage.run] source`` comment for the
enumerated list) — a one-off/rerunnable measurement tool in the same
spirit as ``scripts/eval-memory-modes.py``, not shipped decision logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

# This file is a hyphenated top-level entry point (repo convention, see
# pyproject.toml's ruff per-file-ignores comment) run directly
# (``.venv/bin/python scripts/eval-recall-at-k.py``), never via
# ``python -m`` (hyphens are not valid module-path segments). Running a
# script directly puts only ITS OWN directory (``scripts/``) on
# ``sys.path``, not the repo root, so ``scripts.deploy.memory`` (a
# dotted-package import) would not resolve without this. ``audittrace``
# itself does not need this — it is pip-installed editable — but the
# sibling ``scripts`` package is not.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audittrace.routes.memory_semantic_documents import (  # noqa: E402
    dedupe_semantic_chunks,
    group_semantic_chunks_by_document,
)
from scripts.deploy.memory import (  # noqa: E402
    _bearer,
    _fleet_headers,
    _http_request,
    _normalize_front_door,
    _resolve_token,
    ssl_context,
)

# The K values the spec's own "limit=25 -> 4 documents" framing and the
# round-1 build record/review both reported at — kept identical so this
# script's output is directly comparable to the round-1 numbers.
K_VALUES = (4, 25, 100, 300)

# 34 (query intent -> expected document title) pairs, >= the spec's
# "at least 30 pairs, drawn from actual dispatch briefs" floor. All 31
# real lessons in the decisions-layer corpus as of 2026-09-17, plus 3
# ratified spec addenda — real fleet history, not synthesised fixtures.
LABELLED_PAIRS: tuple[tuple[str, str], ...] = (
    (
        "gate piped through tee hides a non-zero exit code",
        "lesson-20260813-never-trust-piped-exit-code-for-gate.md",
    ),
    (
        "helm adoption and kubeconfig default hiccups",
        "lesson-20260814-helm-adoption-and-kubeconfig-hiccups.md",
    ),
    (
        "an abstraction must close the escape hatch it opens",
        "lesson-abstraction-must-close-the-escape-hatch-20260913.md",
    ),
    (
        "mirror the ADR-049 evidence gate locally before pushing",
        "lesson-adr049-mirror-gate-before-push-20260815.md",
    ),
    (
        "an aggregate query must be scoped by user or it leaks cross-tenant",
        "lesson-aggregate-queries-must-be-user-scoped-20260913.md",
    ),
    (
        "APU unified memory hard reset under concurrent big-model load",
        "lesson-apu-unified-memory-hard-reset-20260912.md",
    ),
    (
        "coverage percentage alone is a floor, not proof a guard is real",
        "lesson-coverage-is-a-floor-not-a-proof-20260917.md",
    ),
    (
        "EICAR test string showing up inside a committed evidence package",
        "lesson-eicar-in-evidence-packages-20260815.md",
    ),
    (
        "grounding a claim in the real system reveals a misframed issue",
        "lesson-grounding-reveals-misframed-issue-20260817.md",
    ),
    (
        "a guard must fail closed and cannot validate itself",
        "lesson-guards-fail-closed-and-cannot-self-validate-20260916.md",
    ),
    (
        "frozen dataclass immutability is field-level, not class-level",
        "lesson-immutability-primitives-are-field-level-20260917.md",
    ),
    (
        "jest --maxWorkers=1 is an in-band V8 heap cap, not real parallelism",
        "lesson-jest-maxworkers1-is-in-band-v8-heap-cap-20260913.md",
    ),
    (
        "keepalive settings validated alongside a swap-flag change",
        "lesson-keepalive-validated-and-swap-flags-20260816.md",
    ),
    (
        "neuter guards individually, never as one batched run",
        "lesson-neuter-guards-individually-20260913.md",
    ),
    (
        "OpenCode picks up a stale per-repo config token after rotation",
        "lesson-opencode-stale-project-config-token-20260817.md",
    ),
    (
        "a premium builder tier did not pay off versus Sonnet",
        "lesson-premium-builder-tier-did-not-pay-off-20260913.md",
    ),
    (
        "Qwen 441 dogfood milestone reached",
        "lesson-qwen-441-dogfood-milestone-20260817.md",
    ),
    (
        "Qwen shows analysis paralysis on subtle logic",
        "lesson-qwen-analysis-paralysis-subtle-logic-20260818.md",
    ),
    (
        "recall's limit parameter counts chunks, not documents",
        "lesson-recall-limit-counts-chunks-not-documents-20260913.md",
    ),
    (
        "a release PR ran E2E against an image that was never published",
        "lesson-release-pr-e2e-unpublished-image-20260823.md",
    ),
    (
        "sealing the mutation of a name is not the same as sealing its value",
        "lesson-sealing-mutation-is-not-sealing-value-20260917.md",
    ),
    (
        "the sovereign adapter's read-discipline lesson",
        "lesson-sovereign-adapter-read-discipline-20260913.md",
    ),
    (
        "SSL_CERT_FILE shadows the system CA trust store",
        "lesson-ssl-cert-file-shadows-system-ca-20260816.md",
    ),
    (
        "check your own techniques before calling a claim unpinnable",
        "lesson-unpinnable-claim-check-your-own-techniques-20260915.md",
    ),
    (
        "a vacuous negative-control test proves nothing",
        "lesson-vacuous-negative-control-tests-20260808.md",
    ),
    (
        "the one root cause behind a run of vacuous neuters",
        "lesson-vacuous-neuter-THE-ONE-ROOT-20260823.md",
    ),
    (
        "a dependency-override bypass makes a neuter vacuous",
        "lesson-vacuous-neuter-dependency-override-bypass-20260823.md",
    ),
    (
        "a neuter's fixture must actually model the side effect",
        "lesson-vacuous-neuter-fixture-must-model-side-effect-20260823.md",
    ),
    (
        "overlapping guards can hide a vacuous neuter",
        "lesson-vacuous-neuter-overlapping-guards-20260823.md",
    ),
    (
        "WU-4 cross-user document-id hijack finding",
        "lesson-wu4-cross-user-docid-hijack-20260905.md",
    ),
    (
        "WU-4 pass 2 must test through the real HTTP route",
        "lesson-wu4-pass2-real-http-route-20260905.md",
    ),
    (
        "ConsoleStoreBase addendum: close the escape hatch",
        "2026-09-13-SPEC-ADDENDUM-A-consolestorebase-close-the-escape-hatch.md",
    ),
    (
        "trailer guard addendum: no exceptions",
        "2026-09-13-SPEC-ADDENDUM-trailer-guard-no-exceptions.md",
    ),
    (
        "WU-B proof standard addendum",
        "2026-09-15-SPEC-ADDENDUM-B-wu-b-proof-standard.md",
    ),
)

# WU-1 fix-round-3 (F11): the three titles below were indexed by THIS
# session (fix-round-1/round-2, same 2026-09-17 date), hours before this
# harness runs against the live corpus. A recency-ordered recall@k
# necessarily ranks the newest documents highest, so scoring against
# same-session writes measures "did I just index this minutes ago", not
# "does the fix generalise" — a recency artefact, not cherry-picking (the
# selection rule that produced LABELLED_PAIRS is otherwise honest and
# exhaustive: every real lesson doc in the corpus, per the round-2
# evidence). Reported SEPARATELY as excl-today below rather than silently
# dropped from LABELLED_PAIRS, so both numbers stay reproducible from one
# run and the artefact is visible, not hidden.
SAME_SESSION_TITLES: frozenset[str] = frozenset(
    {
        "lesson-coverage-is-a-floor-not-a-proof-20260917.md",
        "lesson-immutability-primitives-are-field-level-20260917.md",
        "lesson-sealing-mutation-is-not-sealing-value-20260917.md",
    }
)


def _fetch_all_chunk_rows(
    front_door: str, token: str, collection: str, insecure: bool
) -> list[dict]:
    """Paginate the FULL live corpus as raw per-chunk rows.

    Always requests ``granularity=chunk`` explicitly, so the fetch is the
    same raw shape regardless of which server version is deployed (a
    pre-WU-1 server ignores the unknown query param and already returns
    chunk rows; a post-WU-1 server honours it) — the client-side
    ``dedupe_semantic_chunks``/``group_semantic_chunks_by_document`` calls
    below are what compute AFTER, not the server's own default.
    """
    ctx = ssl_context(insecure)
    headers = {**_bearer(token), **_fleet_headers()}
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "collection": collection,
            "sort": "created_at",
            "order": "desc",
            "granularity": "chunk",
            "limit": 500,
            "offset": offset,
        }
        url = f"{front_door}/memory/semantic?{urlencode(params)}"
        status, _headers, body = _http_request("GET", url, headers=headers, context=ctx)
        if status != 200:
            raise RuntimeError(
                f"GET /memory/semantic failed: HTTP {status}: {body[:300]!r}"
            )
        payload = json.loads(body)
        page = payload["items"]
        rows.extend(page)
        total = payload["total"]
        offset += len(page)
        if not page or offset >= total:
            break
    return rows


def _recall_at_k(
    ranked_titles: list[str], expected_titles: set[str], k: int
) -> tuple[int, int]:
    """Return ``(hits, scoreable)`` for one k: how many of ``expected_titles``
    (present in the corpus at all) appear within the first *k* rows of
    ``ranked_titles``."""
    window = set(ranked_titles[:k])
    hits = sum(1 for t in expected_titles if t in window)
    return hits, len(expected_titles)


def _corpus_pin(chunk_rows: list[dict]) -> str:
    """WU-1 fix-round-3 (F13): a short, deterministic fingerprint of the
    FETCHED corpus (every row's stable chunk identity, sorted, then
    hashed) so a later re-run can tell — WITHOUT re-fetching or trusting a
    printed row count alone — whether it saw the SAME corpus snapshot or
    a drifted one. Row count alone is not enough: the corpus can gain N
    rows and lose N different ones between two runs and still print the
    same count."""
    identities = sorted(str(r.get("key") or "") for r in chunk_rows)
    digest = hashlib.sha256("\n".join(identities).encode()).hexdigest()
    return digest[:16]


def run(front_door: str, collection: str, insecure: bool) -> int:
    token = _resolve_token(None)
    if not token:
        print(
            "no scoped JWT available (~/.config/audittrace/tokens.json or "
            "AUDITTRACE_TOKEN) — run scripts/audittrace-login first",
            file=sys.stderr,
        )
        return 2

    run_started_at = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"run started: {run_started_at}")

    chunk_rows = _fetch_all_chunk_rows(front_door, token, collection, insecure)
    chunk_rows = dedupe_semantic_chunks(
        chunk_rows
    )  # cross-path dedup, both views share it
    corpus_pin = _corpus_pin(chunk_rows)
    print(
        f"fetched {len(chunk_rows)} de-duplicated chunk rows from {collection!r} "
        f"(corpus pin: {corpus_pin})"
    )

    before_titles = [r.get("title") or "" for r in chunk_rows]
    documents = group_semantic_chunks_by_document(chunk_rows)
    after_titles = [d.get("title") or "" for d in documents]
    print(f"grouped into {len(documents)} documents")

    corpus_titles = set(before_titles)
    scored_pairs = [
        (query, title) for query, title in LABELLED_PAIRS if title in corpus_titles
    ]
    missing = [title for _q, title in LABELLED_PAIRS if title not in corpus_titles]
    for title in missing:
        print(f"MISSING from live corpus, excluded from scoring: {title}")

    expected = {title for _q, title in scored_pairs}
    print(
        f"\nlabelled set: {len(LABELLED_PAIRS)} pairs, {len(scored_pairs)} scoreable "
        f"(>= 30 required by spec: {'OK' if len(LABELLED_PAIRS) >= 30 else 'SHORT'})\n"
    )

    # WU-1 fix-round-3 (F11): report excl-today FIRST — same-session writes
    # (see SAME_SESSION_TITLES) are recency artefacts, not signal about
    # whether the fix generalises. incl-today is reported alongside so the
    # magnitude of the artefact stays visible rather than hidden.
    excluded_today = expected & SAME_SESSION_TITLES
    expected_excl_today = expected - SAME_SESSION_TITLES
    if excluded_today:
        print(
            f"same-session writes excluded from the excl-today row below "
            f"({len(excluded_today)}): {sorted(excluded_today)}\n"
        )

    print(
        f"{'k':>5} | {'excl-today AFTER':>17} | {'incl-today BEFORE':>18} "
        f"| {'incl-today AFTER':>17}"
    )
    print("-" * 68)
    for k in K_VALUES:
        excl_after_hits, excl_denom = _recall_at_k(after_titles, expected_excl_today, k)
        before_hits, denom = _recall_at_k(before_titles, expected, k)
        after_hits, _denom = _recall_at_k(after_titles, expected, k)
        excl_after_pct = 100.0 * excl_after_hits / excl_denom if excl_denom else 0.0
        before_pct = 100.0 * before_hits / denom if denom else 0.0
        after_pct = 100.0 * after_hits / denom if denom else 0.0
        print(
            f"{k:>5} | {excl_after_pct:>16.2f}% | {before_pct:>17.2f}% "
            f"| {after_pct:>16.2f}%"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default="decisions")
    parser.add_argument(
        "--front-door",
        default=None,
        help="Overrides AUDITTRACE_FRONT_DOOR (laptop default https://audittrace.local)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (self-signed laptop front door)",
    )
    args = parser.parse_args()

    front_door = _normalize_front_door(
        args.front_door
        or os.environ.get("AUDITTRACE_FRONT_DOOR", "https://audittrace.local")
    )
    return run(front_door, args.collection, args.insecure)


if __name__ == "__main__":
    raise SystemExit(main())
