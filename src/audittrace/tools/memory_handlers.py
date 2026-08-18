"""Memory tool handlers (ADR-025 §Decision.1).

The four tools exposed to the LLM by the proxy-internal tool-call loop.
Each handler is a thin adapter from the canonical recall-tool result
schema to the underlying memory service. All four handlers share the same
shape so the LLM sees a stable result schema regardless of which layer
answered.

Handler contract (observed by every handler in this file):

  - First positional argument is ``user_context: UserContext`` —
    always threaded into the underlying service's per-user filter.
  - Second positional argument is ``args: dict`` — the JSON payload
    the LLM produced for this tool call.
  - Returns a dict. On success the dict is::

        {
            "matches": [...],
            "total": N,        # true candidate count, bounded by
                                # MAX_RECALL_WINDOW — NOT len(matches)
            "limit": k,
            "offset": offset,
            "sort": "relevance" | "recency" | "id",
            "order": "asc" | "desc",
            "has_more": bool,  # offset + limit < total
            "truncated": bool, # DEPRECATED alias of has_more — see
                                # CHANGELOG note below. Removed one
                                # release after 2026-08-03.
        }

    On known bad input (e.g. missing required argument) the dict is
    ``{"error": "..."}``. Unexpected exceptions propagate to
    ``tools.invoke_tool`` which wraps them as ``{"error": ...}``.

CHANGELOG (SPEC #374 RESHAPE, 2026-08-18): the ``corpus_status`` gate is
reshaped from "``total == 0`` AND any accessible un-indexed doc" to
"``summary.matched`` non-empty" — i.e. the block attaches iff at least one
accessible doc whose key/title matches a query token is un-indexed or
dead-lettered, INDEPENDENT of the recall result. ChromaDB ANN never
returns an empty page, so the old gate could never fire (the bug the
reshape fixes), and a non-empty recall with a matched un-indexed doc
still deserves the signal (the doc exists but cannot be found by search).
DELIBERATE BEHAVIOR CHANGE: an empty recall with only UNRELATED
pending/dead-lettered docs now stays SILENT (the old gate fired on ANY
pending doc). The enrichment stays best-effort (``_maybe_corpus_status``
never breaks recall on failure) and tenancy-scoped.

CHANGELOG (SPEC #374 / recall-not-indexed-signal, 2026-08-16):
``recall_decisions``/``recall_skills``/``recall_semantic`` (the three
ChromaDB-backed recall tools — NOT ``recall_recent_sessions``, which is
Postgres-session-backed and index-status-irrelevant) now attach an
OPTIONAL ``corpus_status`` block when, and only when, ``total == 0`` AND
the caller has at least one accessible un-indexed/dead-lettered document
(``_maybe_corpus_status`` — best-effort, never breaks recall on failure).
It closes the gap where an empty result was indistinguishable from "this
topic doesn't exist" even when a matching document exists but was never
indexed or was dead-lettered (#450). Purely additive to the existing
empty-result shape above; a caller that never sees a non-empty
``pending_index + dead_lettered`` accessible to it observes byte-identical
behaviour to before this change. (Superseded by the RESHAPE entry above.)

CHANGELOG (backlog #15 residual, #375 / RECALL-PAGINATION-20260803,
2026-08-03): recall_decisions/recall_skills/recall_semantic/
recall_recent_sessions gained ``offset`` (all four) and ``sort``/``order``
(the three ChromaDB-backed ones) so a caller can page past a relevance
cut-off instead of being stuck re-issuing the same query. ``total`` used
to be ``len(matches)`` (a lie — it was always <= the requested page size,
so a caller could never tell how much more existed); it is now the true
candidate count up to ``MAX_RECALL_WINDOW`` (services/semantic.py). A call
with none of the new params returns the exact same top-k as before this
change (backward-compat, R2/R6). ``truncated`` is kept as a DEPRECATED
alias of ``has_more`` for one release; new callers should read
``has_more``.

The handlers do NOT check the scope gate — that is enforced at tool
advertisement time by ``tools_visible_to``. By the time the tool-call
loop dispatches here, the user is already authorised.

Importing this module has side effects: the four ``@register_memory_tool``
decorators run and populate ``MEMORY_TOOL_REGISTRY``. Production startup
imports this module from ``server.py``; tests that need the handlers
registered import it explicitly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from audittrace.dependencies import (
    get_conversational_service,
    get_episodic_service,
    get_memory_manifest_service,
    get_procedural_service,
    get_semantic_service,
)
from audittrace.identity import UserContext
from audittrace.services.recall_telemetry import emit_recall_telemetry
from audittrace.services.semantic import MAX_RECALL_WINDOW, SearchPage
from audittrace.tools import register_memory_tool

logger = logging.getLogger(__name__)


# Snippet length for the discovery tools (recall_decisions / recall_skills).
# Discovery results return short previews so the LLM can pick a candidate, then
# fetch the full document via read_decision / read_skill on a follow-up call —
# those reads still resolve the full ``.md`` from S3 by filename, so the preview
# stays deliberately short even though the underlying store is now the ChromaDB
# vector collection (#383). recall_semantic does NOT use this — its results are
# vector-store chunks that are already bounded by the chunker, so truncating
# them again was hiding useful context with no benefit.
_SNIPPET_LIMIT = 400

# Top-k for the discovery tools. The keyword layer they replaced returned every
# substring hit; a vector store needs a bounded top-k. Kept small because these
# are discovery previews (pick a candidate → read the full .md), not answers.
_DISCOVERY_K = 5

# Pagination (backlog #15 residual, #375 / RECALL-PAGINATION-20260803).
_VALID_SORT = {"relevance", "recency", "id"}
_VALID_ORDER = {"asc", "desc"}

_PAGE_DOC = (
    "Page forward when has_more is true by re-calling with a larger "
    "offset (offset += limit) — do NOT re-issue the same query to find "
    "more; the response's top-k already contains everything reachable "
    "without paging."
)
_SORT_DOC = (
    "Result order. 'relevance' (default) = closest semantic match first. "
    "'recency' = newest first — turns recall into a pageable enumeration "
    "over the whole collection, useful when you know roughly what you're "
    "looking for but relevance ranking keeps missing it. 'id' = stable "
    "lexicographic order, useful for exhaustive paging."
)
_ORDER_DOC = (
    "Sort direction. Defaults to 'asc' for relevance/id and 'desc' "
    "(newest-first) for recency."
)


def _parse_offset(args: dict[str, Any], tool_name: str) -> int | dict[str, Any]:
    """Parse+validate the optional ``offset`` arg. Returns the int (>=0) on
    success, or an ``{"error": ...}`` dict on bad input — the same
    convention every other arg parser in this module follows."""
    offset_raw = args.get("offset", 0)
    try:
        offset = int(offset_raw)
    except (TypeError, ValueError):
        return {"error": f"{tool_name}: 'offset' must be an integer"}
    if offset < 0:
        return {"error": f"{tool_name}: 'offset' must be >= 0"}
    return offset


def _parse_sort_order(
    args: dict[str, Any], tool_name: str
) -> tuple[str, str | None] | dict[str, Any]:
    """Parse+validate the optional ``sort``/``order`` args. Returns
    ``(sort, order)`` on success (``order`` may be ``None`` — the service
    layer applies the per-sort default) or an ``{"error": ...}`` dict."""
    sort = args.get("sort", "relevance")
    if sort not in _VALID_SORT:
        return {"error": f"{tool_name}: 'sort' must be one of {sorted(_VALID_SORT)}"}
    order = args.get("order")
    if order is not None and order not in _VALID_ORDER:
        return {"error": f"{tool_name}: 'order' must be one of {sorted(_VALID_ORDER)}"}
    return sort, order


def _page_fields(page: SearchPage) -> dict[str, Any]:
    """The pagination tail shared by every recall tool's response —
    ``total``/``limit``/``offset``/``sort``/``order``/``has_more`` plus the
    DEPRECATED ``truncated`` alias (R2, one-release compat window)."""
    return {
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "sort": page.sort,
        "order": page.order,
        "has_more": page.has_more,
        "truncated": page.has_more,
    }


# ── SPEC #374 — recall corpus-incompleteness signal ──────────────────────
# A recall on ChromaDB alone is blind to un-indexed / dead-lettered docs
# by definition (no vectors were ever written for them), so "no match" is
# indistinguishable from "the topic genuinely doesn't exist".
# ``_maybe_corpus_status`` closes that gap with a best-effort health signal
# (see the module docstring's callers below); ``_doc_tokens`` is its pure
# token-extraction half (sub-decision #5). Reshaped 2026-08-18: the signal
# fires on a query-matched UN-INDEXED doc, independent of the recall
# result (ANN never returns empty, so the old empty-only gate never fired).

# Doc-like token: a letter-led run of 3+ letters/digits/hyphens — catches
# identifiers such as "ADR-058" or "SPEC-374" without matching short noise
# words. Ratified regex, sub-decision #5.
_DOC_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
_QUOTED_PHRASE_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')
# Bounds the number of ILIKE clauses ``index_status_summary`` builds per
# call — a best-effort name-match input, not an exhaustive index.
_MAX_DOC_TOKENS = 8


def _doc_tokens(query: str) -> list[str]:
    """Extract doc-like tokens from a recall query (SPEC #374, sub-decision
    #5): every quoted phrase (taken whole), plus every ``_DOC_TOKEN_RE``
    match. Lower-cased and de-duplicated (order-preserving; quoted phrases
    win the earlier slot when a phrase and one of its own words would
    otherwise collide), bounded to :data:`_MAX_DOC_TOKENS`.

    Pure function — deterministic, no I/O. The SQL ``%token%`` ILIKE
    wrapping happens in ``MemoryManifestService.index_status_summary``
    (WU-1); this function only decides WHICH substrings are worth
    matching on, keeping the extraction RULE and the query MECHANICS in
    their own single-responsibility homes.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        token = raw.strip().lower()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for match in _QUOTED_PHRASE_RE.finditer(query):
        _add(match.group(1) or match.group(2) or "")
    for match in _DOC_TOKEN_RE.finditer(query):
        _add(match.group(0))

    return tokens[:_MAX_DOC_TOKENS]


async def _maybe_corpus_status(
    user_context: UserContext, query: str
) -> dict[str, Any] | None:
    """Best-effort ``corpus_status`` enrichment for recall (SPEC #374,
    sub-decisions #2/#3/#6; RESHAPED 2026-08-18) — the single choke point
    wired into all three ChromaDB-backed recall tools (sub-decision #3)
    so a query-matched UN-INDEXED doc carries an honest signal that the
    corpus may be incomplete instead of silently reading as "this topic
    doesn't exist".

    Unified rule: the enrichment attaches iff ``summary.matched`` is
    non-empty — at least one accessible doc whose key/title matches a
    query token is un-indexed or dead-lettered. Recall emptiness is
    IRRELEVANT: ChromaDB ANN never returns an empty page, so the old
    ``page.total == 0`` gate could not fire at all (the bug the reshape
    fixes), and a NON-empty recall with a matched un-indexed doc still
    deserves the signal (the doc exists but cannot be found by search).
    Only-UNRELATED pending/dead-lettered docs (``matched`` empty) stay
    SILENT — sub-decision #2's "don't cry wolf", now keyed on the match
    instead of the corpus-wide total (a deliberate behavior change: the
    old gate fired on ANY pending doc).

    Returns ``None`` (attach nothing) when:
      - no query-matched un-indexed doc is accessible to the caller
        (``summary.matched`` empty — sub-decision #2, "don't cry wolf":
        a fully-indexed corpus, or unrelated pending noise, is the
        CORRECT answer and adding it would erode trust in the signal), or
      - the lookup itself fails for any reason (sub-decision #6 — this
        enrichment must NEVER break recall; the caller falls back to its
        normal page).

    Tenancy: ``index_status_summary`` is threaded the caller's
    ``user_context`` and applies the owner-or-corpus predicate itself —
    see that method's docstring for why (``memory_items`` carries no
    Postgres RLS policy). This function never re-derives or overrides
    that scoping.
    """
    try:
        manifest = get_memory_manifest_service()
        tokens = _doc_tokens(query)
        # skip_counts_if_no_match: this runs on EVERY recall now (the reshape dropped
        # the page.total==0 gate); when nothing matched we return None below without
        # reading the counts, so skip the two COUNT queries on that hot path.
        summary = await manifest.index_status_summary(
            user_context, tokens, skip_counts_if_no_match=True
        )
    except Exception:
        logger.warning(
            "corpus_status enrichment failed; omitting from recall result",
            exc_info=True,
        )
        return None

    if not summary.matched:
        return None

    return {
        "pending_index": summary.pending,
        "dead_lettered": summary.dead_lettered,
        "matched_unindexed": [
            {"key": m.key, "state": m.state, "code": m.index_failure_code}
            for m in summary.matched
        ],
        "note": (
            f"{len(summary.matched)} document(s) matching your query are "
            "not yet indexed and cannot be found by search; do not "
            "conclude the topic is absent."
        ),
    }


def _recall_identity_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """Durable-identity fields for a recall match (#372 / ADR-060).

    Records BOTH a durable identifier and a stable source pointer so a
    ``tool_calls.result_summary`` row names the exact memory that shaped an
    answer — not just its text — and a reconstruction query can fetch that
    passage back:

    - ``id`` — the ChromaDB ``chunk_id`` (== the ``document_id`` supplied at
      upsert) when the match came from the vector store. All three vector
      recall tools carry a chunk id: ``recall_semantic`` and — since #383 —
      ``recall_decisions`` / ``recall_skills``, which now query the ChromaDB
      ``decisions`` / ``skills`` collections too. When a match carries no chunk
      id (a distance-less / degraded response), ``id`` falls back to the stable
      source pointer — itself the durable ``.md`` filename ``read_decision`` /
      ``read_skill`` resolve by. Never blank.
    - ``source_ref`` — stable pointer that survives a re-index (D1):
      ``file`` → ``source`` → ``title`` → ``chunk_id`` → ``collection``. Never
      blank. This is the audit-grade answer to "does that identifier still
      resolve after you re-index?" — a re-mint of ``chunk_id`` leaves the
      earlier (file/source/title) pointer untouched.
    - ``sha256`` — ``document_sha256`` (markdown / manifest key) or
      ``document_hash`` (the key the PDF pipeline writes), else ``None``.
    - ``distance`` — the RAW ChromaDB distance (D2), lower = closer. Recorded
      honestly named, not converted to a similarity score. Non-null for every
      vector-store match (all three recall tools post-#383); ``None`` only for a
      distance-less / degraded response.
    """
    source_ref = (
        metadata.get("file")
        or metadata.get("source")
        or metadata.get("title")
        or metadata.get("chunk_id")
        or metadata.get("collection")
        or ""
    )
    chunk_id = metadata.get("chunk_id")
    match_id = chunk_id if chunk_id else source_ref
    sha256 = metadata.get("document_sha256") or metadata.get("document_hash")
    distance = metadata.get("distance")
    return {
        "id": match_id,
        "source_ref": source_ref,
        "sha256": sha256,
        "distance": distance,
    }


# ───────────────────────────── recall_decisions ─────────────────────────────


@register_memory_tool(
    name="recall_decisions",
    description=(
        "Recall past architectural decisions (ADRs) relevant to a topic. "
        "Use when the user asks about architectural history, design trade-offs, "
        "or wants to know what was decided and why."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The topic or keywords to search for, e.g. "
                    "'KV cache compression' or 'OAuth2 token validation'."
                ),
            },
            "offset": {
                "type": "integer",
                "description": _PAGE_DOC,
                "default": 0,
                "minimum": 0,
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "recency", "id"],
                "description": _SORT_DOC,
                "default": "relevance",
            },
            "order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": _ORDER_DOC,
            },
        },
        "required": ["query"],
    },
    # Scope UNCHANGED (#383). The DATA DOMAIN is still "decisions" (ADRs /
    # episodic memory): recall_decisions discovers them and read_decision reads
    # the full ``.md`` from S3 — both under ``memory:episodic:read``. Only the
    # discovery STORE moved (S3 keyword → ChromaDB ``decisions`` vector
    # collection). The semantic service enforces the per-user ``where`` from the
    # threaded ``user_context``; it needs no scope of its own (the handler does
    # not gate scope — ``tools_visible_to`` does). Retargeting this to
    # ``memory:semantic:read`` would decouple recall from read and conflate the
    # decisions domain with the general RAG corpus, so it stays as-is.
    required_scope="memory:episodic:read",
)
async def recall_decisions(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Discover decisions (ADRs) via the ChromaDB ``decisions`` vector
    collection (#383), shaped into the canonical tool-result schema.

    Repointed from the S3 keyword layer to ``ChromaSemanticService.search_page``
    with ``collections=["decisions"]`` and the per-user ``where`` filter (the
    service derives it from ``user_context``). The match's ``source`` field
    stays the ``.md`` filename the indexer stamps (``metadata["source"]``), so
    the model can chain recall → ``read_decision`` unchanged. Pagination
    (offset/sort/order) — see the module CHANGELOG note.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_decisions: 'query' is required and must be a string"}

    offset = _parse_offset(args, "recall_decisions")
    if isinstance(offset, dict):
        return offset
    sort_order = _parse_sort_order(args, "recall_decisions")
    if isinstance(sort_order, dict):
        return sort_order
    sort, order = sort_order

    semantic = get_semantic_service()
    page = await semantic.search_page(
        user_context,
        query,
        k=_DISCOVERY_K,
        collections=["decisions"],
        offset=offset,
        sort=sort,
        order=order,
    )
    emit_recall_telemetry(
        "tool",
        "decisions",
        page.total,
        cache="miss",
        best_match_distance=page.matches[0].metadata.get("distance")
        if page.matches
        else None,
    )
    result: dict[str, Any] = {
        "matches": [
            {
                **_recall_identity_fields(d.metadata),
                # No ``title`` is stamped for .md vector docs; fall back to the
                # filename so the preview still names its artefact.
                "title": (
                    d.metadata.get("title")
                    or d.metadata.get("source")
                    or d.metadata.get("file")
                    or "ADR"
                ),
                "snippet": d.page_content[:_SNIPPET_LIMIT],
                # READ-FOLLOW-UP CONTRACT: the indexer stamps
                # ``metadata["source"] = filename`` (routes/memory.py), so
                # ``source`` carries the ``.md`` filename read_decision resolves
                # by — never the chunk id. ``file`` is a legacy fallback.
                "source": d.metadata.get("source") or d.metadata.get("file", ""),
            }
            for d in page.matches
        ],
        **_page_fields(page),
    }
    # SPEC #374 (reshaped 2026-08-18): always evaluate — the enrichment
    # fires on a query-matched UN-INDEXED doc, independent of the recall
    # result (ChromaDB ANN never returns empty, so the old
    # ``page.total == 0`` gate never fired); ``_maybe_corpus_status``
    # returns None when nothing matched.
    corpus_status = await _maybe_corpus_status(user_context, query)
    if corpus_status is not None:
        result["corpus_status"] = corpus_status
    return result


# ─────────────────────────────── recall_skills ──────────────────────────────


@register_memory_tool(
    name="recall_skills",
    description=(
        "Recall relevant skill documents for a topic or technique. "
        "Use when the user asks how to do something, or when the answer "
        "depends on a specific methodology, framework, or practice captured "
        "in the skill library (e.g. IAM, C4 architecture, writing style)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The skill area or technique to search for, e.g. "
                    "'OAuth2 BFF pattern' or 'C4 model Structurizr DSL'."
                ),
            },
            "offset": {
                "type": "integer",
                "description": _PAGE_DOC,
                "default": 0,
                "minimum": 0,
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "recency", "id"],
                "description": _SORT_DOC,
                "default": "relevance",
            },
            "order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": _ORDER_DOC,
            },
        },
        "required": ["query"],
    },
    # Scope UNCHANGED (#383). Same reasoning as recall_decisions: the domain is
    # still "skills" (procedural memory) — recall_skills discovers, read_skill
    # reads the full ``.md`` from S3, both under ``memory:procedural:read``.
    # Only the discovery store moved to the ChromaDB ``skills`` vector
    # collection; the semantic service applies the per-user ``where`` from the
    # threaded ``user_context`` and needs no scope of its own.
    required_scope="memory:procedural:read",
)
async def recall_skills(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Discover skills via the ChromaDB ``skills`` vector collection (#383),
    shaped into the canonical tool-result schema.

    Repointed from the S3 keyword layer to ``ChromaSemanticService.search_page``
    with ``collections=["skills"]`` and the per-user ``where`` filter. The
    match's ``source`` field stays the ``.md`` filename (``metadata["source"]``)
    so the model can chain recall → ``read_skill`` unchanged. Pagination
    (offset/sort/order) — see the module CHANGELOG note.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_skills: 'query' is required and must be a string"}

    offset = _parse_offset(args, "recall_skills")
    if isinstance(offset, dict):
        return offset
    sort_order = _parse_sort_order(args, "recall_skills")
    if isinstance(sort_order, dict):
        return sort_order
    sort, order = sort_order

    semantic = get_semantic_service()
    page = await semantic.search_page(
        user_context,
        query,
        k=_DISCOVERY_K,
        collections=["skills"],
        offset=offset,
        sort=sort,
        order=order,
    )
    emit_recall_telemetry(
        "tool",
        "skills",
        page.total,
        cache="miss",
        best_match_distance=page.matches[0].metadata.get("distance")
        if page.matches
        else None,
    )
    result: dict[str, Any] = {
        "matches": [
            {
                **_recall_identity_fields(d.metadata),
                # The skills indexer stamps ``metadata["skill"] = <name>``
                # (routes/memory.py); prefer it, then the filename.
                "title": (
                    d.metadata.get("skill")
                    or d.metadata.get("source")
                    or d.metadata.get("file")
                    or "Skill"
                ),
                "snippet": d.page_content[:_SNIPPET_LIMIT],
                # READ-FOLLOW-UP CONTRACT: ``source`` is the ``.md`` filename
                # read_skill resolves by (stamped ``metadata["source"]``), never
                # the chunk id.
                "source": d.metadata.get("source") or d.metadata.get("file", ""),
            }
            for d in page.matches
        ],
        **_page_fields(page),
    }
    # SPEC #374 (reshaped 2026-08-18): always evaluate — the enrichment
    # fires on a query-matched UN-INDEXED doc, independent of the recall
    # result (ChromaDB ANN never returns empty, so the old
    # ``page.total == 0`` gate never fired); ``_maybe_corpus_status``
    # returns None when nothing matched.
    corpus_status = await _maybe_corpus_status(user_context, query)
    if corpus_status is not None:
        result["corpus_status"] = corpus_status
    return result


# ───────────────────────── recall_recent_sessions ──────────────────────────


@register_memory_tool(
    name="recall_recent_sessions",
    description=(
        "Recall the most recent conversation sessions for a project. "
        "Use when the user asks about continuity — 'what did we work on', "
        "'remind me what we decided last time', 'where did we leave off'. "
        "Scoped to the caller: only returns sessions owned by this user."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": (
                    "The project identifier to scope the session lookup. "
                    "Pass the current request's project name."
                ),
            },
            "n": {
                "type": "integer",
                "description": "Max number of sessions to return. Default 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 50,
            },
            "offset": {
                "type": "integer",
                "description": _PAGE_DOC,
                "default": 0,
                "minimum": 0,
            },
        },
        "required": ["project"],
    },
    required_scope="memory:conversational:read-own",
)
async def recall_recent_sessions(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``ConversationalService.load_sessions`` in the canonical shape.

    Session key points are appended to the ``snippet`` field so the LLM
    sees them inline without the schema needing a bespoke ``key_points``
    column.

    Sessions are ALREADY ordered by recency (most-recent-first) at the
    service layer — there is no relevance ranking to sort by here, so this
    tool advertises ``offset`` only (R2) and reports a fixed
    ``sort="recency"``/``order="desc"`` in the response rather than
    exposing a ``sort``/``order`` request param that would have no other
    valid value (a deliberate scoping choice, unlike the three ChromaDB-
    backed recall tools which mirror the LIST endpoint's full sort
    vocabulary). ``ConversationalService.load_sessions`` has no native
    ``offset`` of its own (out of scope for this change — see
    services/conversational.py), so pagination is applied HERE: request
    ``offset + n + 1`` sessions (the same "+1 probe" refinement as
    ``ChromaSemanticService.search_page``'s relevance path — see there for
    the full rationale), bounded by ``MAX_RECALL_WINDOW``, and slice
    locally. Without the +1, a fully-filled window makes
    ``total == offset + n`` exactly and ``has_more`` (``offset + n <
    total``) would incorrectly read ``False`` even when more sessions
    exist.
    """
    project = args.get("project")
    if not isinstance(project, str) or not project.strip():
        return {
            "error": "recall_recent_sessions: 'project' is required and must be a string"
        }

    n_raw = args.get("n", 5)
    try:
        n = max(1, min(int(n_raw), 50))
    except (TypeError, ValueError):
        return {"error": "recall_recent_sessions: 'n' must be an integer"}

    offset = _parse_offset(args, "recall_recent_sessions")
    if isinstance(offset, dict):
        return offset

    conversational = get_conversational_service()
    window = min(offset + n + 1, MAX_RECALL_WINDOW)
    sessions = await conversational.load_sessions(user_context, project, window)
    total = len(sessions)
    page_sessions = sessions[offset : offset + n]
    has_more = offset + n < total
    # recall_recent_sessions has no ChromaDB-backed SearchPage of its own (it
    # paginates ``load_sessions`` locally, see the docstring above) — build a
    # minimal SearchPage carrying only the page fields telemetry needs
    # (``total``); ``matches=[]`` is fine, ``emit_recall_telemetry`` never
    # reads it.
    emit_recall_telemetry("tool", "sessions", total, cache="miss")

    matches: list[dict[str, Any]] = []
    for s in page_sessions:
        snippet = s.get("summary", "")[:_SNIPPET_LIMIT]
        key_points = s.get("key_points") or []
        if key_points:
            kp_joined = "; ".join(str(kp) for kp in key_points)
            snippet = f"{snippet}\nKey points: {kp_joined}"
        # ADR-030 Part 1: flag synthetic (draft) summaries inline so the
        # LLM can treat them as lower-confidence hints rather than as
        # finalised session records.
        if s.get("synthetic"):
            snippet = f"[draft — not yet summarised] {snippet}"
        matches.append(
            {
                "title": s.get("id", "session"),
                "snippet": snippet,
                "source": s.get("date", ""),
            }
        )
    return {
        "matches": matches,
        "total": total,
        "limit": n,
        "offset": offset,
        "sort": "recency",
        "order": "desc",
        "has_more": has_more,
        "truncated": has_more,
    }


# ───────────────────────────── recall_semantic ──────────────────────────────


@register_memory_tool(
    name="recall_semantic",
    description=(
        "Semantic similarity search across the RAG knowledge base. "
        "Use when the user asks a conceptual question where keyword "
        "matching may miss the right document — this tool finds passages "
        "that are semantically related even without literal keyword overlap."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query to embed and match.",
            },
            "k": {
                "type": "integer",
                "description": "Top-k results to return. Default 4.",
                "default": 4,
                "minimum": 1,
                "maximum": 20,
            },
            "offset": {
                "type": "integer",
                "description": _PAGE_DOC,
                "default": 0,
                "minimum": 0,
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "recency", "id"],
                "description": _SORT_DOC,
                "default": "relevance",
            },
            "order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": _ORDER_DOC,
            },
        },
        "required": ["query"],
    },
    required_scope="memory:semantic:read",
)
async def recall_semantic(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``ChromaSemanticService.search_page`` in the canonical shape.
    Pagination (offset/sort/order) — see the module CHANGELOG note."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_semantic: 'query' is required and must be a string"}

    k_raw = args.get("k", 4)
    try:
        k = max(1, min(int(k_raw), 20))
    except (TypeError, ValueError):
        return {"error": "recall_semantic: 'k' must be an integer"}

    offset = _parse_offset(args, "recall_semantic")
    if isinstance(offset, dict):
        return offset
    sort_order = _parse_sort_order(args, "recall_semantic")
    if isinstance(sort_order, dict):
        return sort_order
    sort, order = sort_order

    semantic = get_semantic_service()
    page = await semantic.search_page(
        user_context, query, k=k, offset=offset, sort=sort, order=order
    )
    emit_recall_telemetry(
        "tool",
        "semantic",
        page.total,
        cache="miss",
        best_match_distance=page.matches[0].metadata.get("distance")
        if page.matches
        else None,
    )
    result: dict[str, Any] = {
        "matches": [
            {
                **_recall_identity_fields(d.metadata),
                "title": d.metadata.get("source", d.metadata.get("file", "?")),
                "snippet": d.page_content,
                "source": d.metadata.get("collection", ""),
            }
            for d in page.matches
        ],
        **_page_fields(page),
    }
    # SPEC #374 (reshaped 2026-08-18): always evaluate — the enrichment
    # fires on a query-matched UN-INDEXED doc, independent of the recall
    # result (ChromaDB ANN never returns empty, so the old
    # ``page.total == 0`` gate never fired); ``_maybe_corpus_status``
    # returns None when nothing matched.
    corpus_status = await _maybe_corpus_status(user_context, query)
    if corpus_status is not None:
        result["corpus_status"] = corpus_status
    return result


# ────────────────────────────── read_decision ──────────────────────────────


@register_memory_tool(
    name="read_decision",
    description=(
        "Fetch the full text of a single Architecture Decision Record (ADR) "
        "by exact filename. Use this AFTER recall_decisions narrows the field "
        "to a candidate — the discovery tool returns 400-char snippets, this "
        "one returns the entire document so questions like 'what does ADR-025 "
        "actually say?' can be answered."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "Exact ADR filename, e.g. 'ADR-025-memory-as-tools.md'. "
                    "Path-traversal characters are rejected."
                ),
            },
        },
        "required": ["file"],
    },
    required_scope="memory:episodic:read",
)
async def read_decision(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``EpisodicService.read`` in a full-content tool result."""
    file = args.get("file")
    if not isinstance(file, str) or not file.strip():
        return {"error": "read_decision: 'file' is required and must be a string"}

    # Self-correcting guard: read_decision serves ADRs (episodic memory). A
    # SKILL filename here is a wrong-tool category error — return an actionable
    # redirect instead of a bare not_found, which the model otherwise answers
    # by guessing another tool/filename and re-prefilling a huge context.
    if file.strip().upper().startswith("SKILL-"):
        return {
            "error": "wrong_tool",
            "file": file,
            "detail": (
                f"'{file}' is a SKILL document (procedural memory), not an ADR. "
                "Use read_skill for SKILL-*.md; read_decision only serves ADRs."
            ),
        }

    episodic = get_episodic_service()
    doc = await episodic.read(user_context, file)
    if doc is None:
        return {
            "error": "not_found",
            "file": file,
            "detail": (
                f"No ADR matching '{file}' is in episodic memory. Do not retry "
                "with filename variations — use recall_decisions to discover "
                "ADRs by topic, or read the file from the workspace directly."
            ),
        }
    return {
        "title": doc.metadata.get("title", file),
        "file": doc.metadata.get("file", file),
        "source": "episodic",
        "content": doc.page_content,
    }


# ──────────────────────────────── read_skill ────────────────────────────────


@register_memory_tool(
    name="read_skill",
    description=(
        "Fetch the full text of a single SKILL document by exact filename. "
        "Use this AFTER recall_skills narrows the field — the discovery tool "
        "returns 400-char snippets, this one returns the entire skill so "
        "questions like 'what's the IAM skill content?' can be answered."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "Exact SKILL filename, e.g. 'SKILL-IAM.md'. "
                    "Path-traversal characters are rejected."
                ),
            },
        },
        "required": ["file"],
    },
    required_scope="memory:procedural:read",
)
async def read_skill(user_context: UserContext, args: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``ProceduralService.read`` in a full-content tool result."""
    file = args.get("file")
    if not isinstance(file, str) or not file.strip():
        return {"error": "read_skill: 'file' is required and must be a string"}

    # Self-correcting guard: read_skill serves SKILLs (procedural memory). An
    # ADR filename here is a wrong-tool category error (an ADR is a decision,
    # not a skill) — redirect to read_decision rather than emit a bare
    # not_found that invites another expensive guess-and-retry round-trip.
    if file.strip().upper().startswith("ADR-"):
        return {
            "error": "wrong_tool",
            "file": file,
            "detail": (
                f"'{file}' is an Architecture Decision Record (a decision), not a "
                "skill. Use read_decision for ADRs; read_skill only serves "
                "SKILL-*.md documents."
            ),
        }

    procedural = get_procedural_service()
    doc = await procedural.read(user_context, file)
    if doc is None:
        return {
            "error": "not_found",
            "file": file,
            "detail": (
                f"No skill matching '{file}' is in procedural memory. Do not "
                "retry with filename variations — use recall_skills to discover "
                "skills by topic."
            ),
        }
    return {
        "title": doc.metadata.get("skill", file),
        "file": doc.metadata.get("file", file),
        "source": "procedural",
        "content": doc.page_content,
    }


__all__ = [
    "recall_decisions",
    "recall_skills",
    "recall_recent_sessions",
    "recall_semantic",
    "read_decision",
    "read_skill",
]
