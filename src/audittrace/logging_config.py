"""Logging configuration for audittrace-server.

Inspired by FluentPython (Luciano Ramalho) - logging as an aspect.
All logs go to stdout, no file logging.

The @log_call decorator is a unified observability aspect: for every
decorated call it emits a DEBUG log line (INPUT/OUTPUT/DURATION), opens
an OpenTelemetry span, and records a histogram metric. It works for
both sync and async functions.

SEC-JWT-IN-LANGFUSE-SPAN (2026-10-08): ``render_value``/``render_payload``/
``scrub_text`` below are the ONE serialiser shared by every emission site
(DEBUG log lines, span input/output, the structured-log formatter) that
used to call ``json.dumps(..., default=str)`` or bare ``str(...)``. That
escape hatch let a bearer JWT (or any object's ``__str__``) ride straight
into Tempo/Langfuse and the DEBUG log line unredacted. The structural
allowlist (D2) + request-credential registry (D3) + shape net (D4) are
documented in full on ``render_value``.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID

from starlette.exceptions import HTTPException as StarletteHTTPException

from audittrace import telemetry

# SCRUB-EVENT-LOOP-STALL (v1.29.4, R1 — ratified ADDENDUM A1 §9) —
# ``_FRAGMENT_WINDOW_LEN`` and the window-index builder moved to
# ``audittrace.identity`` (pure string code, no imports there) so
# ``register_request_secret`` can build the index ONCE, at registration,
# instead of ``_scrub_secret_fragments``/``secret_leaks_into`` rebuilding it
# on every scrubbed string leaf (the v1.29.3 event-loop stall). Both names
# are re-exported here UNCHANGED — this module's own code below and every
# existing test import keep working against the ORIGINAL names.
# ``_secret_window_index`` is not called from this module's own production
# code any more (the hot path reads ``request_secret_index`` instead) — it
# stays imported/re-exported anyway (G-P1, build contract) because it is
# one of the TWO bindings a regression could rebuild through.
from audittrace.identity import _FRAGMENT_WINDOW_LEN, request_secret_index
from audittrace.identity import (
    build_secret_window_index as _secret_window_index,  # noqa: F401
)


def _is_expected_client_error(exc: BaseException) -> bool:
    """True for an ``HTTPException`` carrying a 4xx status — an *expected*
    client error (bad/expired token, missing scope, not found, validation)
    that ``@log_call`` should log concisely at WARNING, NOT as an ERROR with a
    full stack trace.

    Reserving ERROR+traceback for 5xx / unexpected exceptions keeps attacker-
    controllable 4xx (e.g. malformed-token spam) from flooding ERROR logs with
    tracebacks that disclose internal paths and pollute error-rate alerting
    (pentest finding F-L1, ADR-049 / OWASP A09). FastAPI's ``HTTPException``
    subclasses Starlette's, so this isinstance check covers both.
    """
    return (
        isinstance(exc, StarletteHTTPException)
        and 400 <= getattr(exc, "status_code", 0) < 500
    )


# ═══════════════════════ SEC-JWT-IN-LANGFUSE-SPAN redaction ═══════════════
#
# D4 — the shape net (secondary, enumeration-class layer). Matches a JWT's
# three dot-separated base64url segments, or a ``Bearer``/``Basic`` auth
# header value, wherever they appear in rendered text — even when the
# value was never registered via D3 (a future ``HTTPBearer()``, a token
# read from a header by hand, an ``alg:none`` token). This is enumeration
# and is ruled secondary to D3's equality pin; its truth is measured by
# the suite-wide net (tests/conftest.py), not asserted here.
_JWT_SHAPE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_AUTHZ_SHAPE_RE = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}")

# ADDENDUM C, gate REJECT round 1 BLOCKER 1 — a third party can TRUNCATE a
# registered secret before it ever reaches ``scrub_text`` (measured case:
# SQLAlchemy's ``StatementError`` repr elides the middle of a long bound
# parameter: "<head> ... (N characters truncated) ... <tail>"). Both <head>
# and <tail> are genuine, UNALTERED substrings of the secret — the exact-
# match branch below misses them (the full string is never present as one
# run) and the D4 shape net also misses them (a bare fragment is no longer
# JWT-shaped: no second ``.``). The window length below matches
# ``audittrace.identity._MIN_REGISTERED_SECRET_LEN``.
#
# Gate REJECT round 2 BLOCKER L1 — the fragment scan used to run only as
# the ``else`` of the exact-match check in ``scrub_text``, so a string
# carrying BOTH the full secret AND a separately-truncated fragment of it
# (e.g. a log line interpolating the full value, glued to a SQLAlchemy
# param repr of the same secret) left the fragment unredacted. The scan
# now always runs after the exact replace too — measured by
# ``test_l1_full_value_and_truncated_fragment_in_one_string_both_redacted``
# below, over a single-pass sink (no second, independent scrub pass).
#
# ``_FRAGMENT_WINDOW_LEN`` and ``_secret_window_index`` are imported from
# ``audittrace.identity`` at the top of this module (SCRUB-EVENT-LOOP-STALL
# v1.29.4 R1) — kept here only as this comment anchor; see that import.


def _extend_match(
    text: str, text_pos: int, secret: str, secret_pos: int
) -> tuple[int, int]:
    """Given ``text[text_pos:text_pos+W] == secret[secret_pos:secret_pos+W]``
    (``W`` = ``_FRAGMENT_WINDOW_LEN``), extend FORWARD while the
    characters keep matching. Returns the maximal matching run's
    ``(start, end)`` bounds in ``text`` (``start`` is always ``text_pos``
    — see below for why).

    Forward-only is not a simplification taken on faith: ``_scrub_secret_fragments``
    below always calls this at the LEFTMOST ``text_pos`` where any window
    matches, scanning strictly left to right from the start of ``text``
    (or from the end of the previous replacement). If a matching run of
    length >= ``_FRAGMENT_WINDOW_LEN`` started earlier than ``text_pos``,
    the window AT that earlier position would itself be a slice of the
    same run and would therefore already have matched — so the scan
    would have stopped there instead. A backward-extension loop here
    could never fire given that calling convention; keeping it would be
    dead code asserting a guarantee nothing exercises.
    """
    end = text_pos + _FRAGMENT_WINDOW_LEN
    s_end = secret_pos + _FRAGMENT_WINDOW_LEN
    while end < len(text) and s_end < len(secret) and text[end] == secret[s_end]:
        end += 1
        s_end += 1
    return text_pos, end


def _scrub_secret_fragments(
    text: str, secret: str, *, index: Mapping[str, tuple[int, ...]] | None = None
) -> str:
    """Redact every maximal run in ``text`` that matches a contiguous
    fragment of ``secret``, even when a third party truncated the middle
    out of the full value before it ever reached this function.

    O(len(text)) for a realistic secret: the scan advances one character
    at a time with a single dict lookup per position
    (``_FRAGMENT_WINDOW_LEN``-char slice, constant cost for a secret with
    few internal repeats); each match's extension costs at most
    O(len(secret)), but the scan pointer jumps to the end of each
    replaced run, so matches are never re-scanned.
    ``test_d3_fragment_scrub_terminates_promptly_on_large_text`` is the
    committed guard for this: a 50KB-noise / realistic-bearer-fragment
    text must scrub in under 2.0s — the only timing bound this module
    claims (gate REJECT round 2, rule 7 — prose may state only what a
    committed test measures; no specific millisecond figure here is
    backed by a committed assertion).

    Gate REJECT round 1 fix1 re-review (should-fix) — this bound is NOT
    universal: a PATHOLOGICALLY repetitive secret (every character the
    same, e.g. ``"A" * 900``) makes every window collide in the index,
    so a non-matching scan degrades toward O(len(text) * len(secret)).
    A real bearer JWT/opaque token has no such internal repeat
    structure, so this is a theoretical worst case, not a shape this
    module expects to see in practice — documented rather than hidden,
    not independently timed here.

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R1/B2 — ratified ADDENDUM A1 §3) —
    ``index`` is keyword-only and optional so the pre-fix 2-positional-arg
    call (``tests/test_log_call_redaction.py:706``, unmodified) keeps
    working unchanged: when omitted, the window index is resolved via
    ``audittrace.identity.request_secret_index`` (O(1) for a registered
    secret — built once at registration — or an uncached build for an
    unregistered one), rather than rebuilt here on every call as before.
    """
    if len(secret) < _FRAGMENT_WINDOW_LEN:
        return text
    if index is None:
        index = request_secret_index(secret)
    out: list[str] = []
    n = len(text)
    i = 0
    last = 0
    while i <= n - _FRAGMENT_WINDOW_LEN:
        offsets = index.get(text[i : i + _FRAGMENT_WINDOW_LEN])
        if not offsets:
            i += 1
            continue
        best_start, best_end = i, i + _FRAGMENT_WINDOW_LEN
        for off in offsets:
            start, end = _extend_match(text, i, secret, off)
            if end - start > best_end - best_start:
                best_start, best_end = start, end
        out.append(text[last:best_start])
        out.append("[REDACTED:bearer]")
        last = best_end
        i = best_end
    out.append(text[last:])
    return "".join(out)


def secret_leaks_into(text: str, secret: str) -> bool:
    """True if ``text`` contains ``secret`` verbatim OR a
    ``_FRAGMENT_WINDOW_LEN``-char-or-longer truncation-surviving fragment
    of it.

    Exposed for the suite-wide redaction net's registered-VALUE leg
    (``tests/conftest.py``) — a DIFFERENT check from the net's shape leg,
    which is blind to a fragment that is no longer JWT-shaped (gate REJECT
    round 1 BLOCKER 1). Shares the same windowed-match logic ``scrub_text``
    uses so the net's oracle and the production redaction agree on what
    counts as "still identifying."

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R1/B2) — keeps its original 2-argument
    public signature unchanged (the conftest net calls it that way,
    ``tests/conftest.py:1010``); the window index is resolved via
    ``audittrace.identity.request_secret_index`` rather than rebuilt here.
    """
    if not secret or not text:
        return False
    if secret in text:
        return True
    if len(secret) < _FRAGMENT_WINDOW_LEN:
        return False
    index = request_secret_index(secret)
    n = len(text)
    for i in range(0, n - _FRAGMENT_WINDOW_LEN + 1):
        if text[i : i + _FRAGMENT_WINDOW_LEN] in index:
            return True
    return False


def scrub_text(value: str) -> str:
    """Redact bearer-shaped secrets from a string leaf.

    D3 (primary) — every value registered this request via
    ``audittrace.identity.register_request_secret`` is replaced by exact
    value equality, LONGEST FIRST (so a shorter registered value can never
    leave a dangling, still-identifying suffix of a longer one
    unredacted). The exact-match replace and the fragment scan are NOT
    either/or (gate REJECT round 2, BLOCKER L1 — orchestrator ruling
    2026-10-11): the fragment scan ALWAYS runs afterwards too, because a
    single string can carry BOTH the full value (e.g. in an f-string
    message) AND a separately-truncated fragment of the SAME secret
    (e.g. a SQLAlchemy param repr glued onto that message, or a head/tail
    pair from ``str(kwargs)[:100]`` alongside the full value elsewhere in
    the same text). Running the fragment scan only when the exact match
    missed left that second, co-occurring fragment completely unredacted
    — the exact-match branch "wins" and the text is returned without ever
    reaching ``_scrub_secret_fragments``. Running the fragment scan
    unconditionally after the exact replace is safe and idempotent: the
    text already has the full value replaced by the literal
    ``[REDACTED:bearer]`` marker, which contains no 16-char run of the
    secret, so re-scanning it for fragments of that SAME secret can only
    ever match genuine, still-present fragments elsewhere in the string.
    D4 (secondary) — a JWT-shaped or ``Bearer``/``Basic``-prefixed
    substring is replaced even when unregistered.

    Never raises: ``request_secrets()`` degrades to "nothing registered"
    on any import/lookup failure (mirrors ``_extract_user_id``'s defensive
    local-import pattern below) so a telemetry-layer bug can never turn
    into a request failure.
    """
    if not value:
        return value
    try:
        from audittrace.identity import request_secrets

        secrets = request_secrets()
    except Exception:  # pragma: no cover - defensive
        secrets = ()
    for secret in sorted(secrets, key=len, reverse=True):
        if not secret:
            continue
        if secret in value:
            value = value.replace(secret, "[REDACTED:bearer]")
        # ALWAYS run the fragment scan too (BLOCKER L1) — not an ``else``
        # branch of the exact-match check above. A truncated fragment of
        # THIS secret may still be present even after the full value was
        # just replaced (full value + fragment co-occurring in one text).
        value = _scrub_secret_fragments(value, secret)
    value = _JWT_SHAPE_RE.sub("[REDACTED:jwt]", value)
    value = _AUTHZ_SHAPE_RE.sub("[REDACTED:authz]", value)
    return value


# D2/N3 — the structural allowlist. Everything NOT named here renders as
# its bare type name (``<TypeName>``) — this is what closes the
# ``default=str`` escape hatch (spec fact F5: a ``Settings`` object with 7
# plain-``str`` secret fields rendered its secrets in full through
# ``default=str``) and removes the old self-detection heuristic's
# accidental dependency on it.
#
# Hand-maintained NAMES, not "every model found by walking a module" —
# ``tests/test_log_call_redaction.py`` (G4c) independently re-derives the
# true set via an AST walk of every ``@log_call`` target's parameter AND
# return annotations (ADDENDUM B N3) plus import resolution, and asserts
# equality against the sets below. A class added to a decorated signature
# without being added here reddens THAT test, not this one — the
# allowlist fails closed (unlisted => ``<TypeName>``) even while out of
# sync, so the equality gap is an observability-quality bug, never a
# redaction-safety one.
_RENDER_MODEL_NAMES: tuple[str, ...] = (
    "AssessmentIngestRequest",
    "ConsoleAclBatchPermissionsRequest",
    "ConsoleAclBatchPermissionsResponse",
    "ConsoleAclBulkRequest",
    "ConsoleAclBulkResponse",
    "ConsoleAclEffectivePermissionsResponse",
    "ConsoleAclEntryItem",
    "ConsoleAclExpireRequest",
    "ConsoleAclExpireResponse",
    "ConsoleAclGrantRequest",
    "ConsoleAclHasPermissionResponse",
    "ConsoleAclModifyRequest",
    "ConsoleAclResourceIdsResponse",
    "ConsoleAgentBatchGetRequest",
    "ConsoleAgentBatchGetResponse",
    "ConsoleAgentItem",
    "ConsoleAgentListResponse",
    "ConsoleAgentUpsertRequest",
    "ConsoleChatProjectItem",
    "ConsoleChatProjectListResponse",
    "ConsoleChatProjectUpsertRequest",
    "ConsoleConversationItem",
    "ConsoleConversationListResponse",
    "ConsoleConversationTagItem",
    "ConsoleConversationTagListResponse",
    "ConsoleConversationTagUpsertRequest",
    "ConsoleConversationTitleUpdateRequest",
    "ConsoleConversationUpsertRequest",
    "ConsoleFileBatchGetRequest",
    "ConsoleFileBatchGetResponse",
    "ConsoleFileItem",
    "ConsoleFileListResponse",
    "ConsoleFileUpsertRequest",
    "ConsoleMessageEditRequest",
    "ConsoleMessageItem",
    "ConsoleMessageListResponse",
    "ConsoleMessageUpsertRequest",
    "ConsolePresetItem",
    "ConsolePresetListResponse",
    "ConsolePresetUpsertRequest",
    "ConsolePromptGroupItem",
    "ConsolePromptGroupListResponse",
    "ConsolePromptGroupUpsertRequest",
    "ConsolePromptGroupWithVersionsItem",
    "ConsolePromptSetProductionRequest",
    "ConsolePromptVersionItem",
    "ConsolePromptVersionUpsertRequest",
    "ConsoleToolFavoriteAddRequest",
    "ConsoleToolFavoriteItem",
    "ConsoleToolFavoriteListResponse",
    "ContextBuildResponse",
    "ContextRequest",
    "HealthResponse",
    # The pydantic AUDIT-RECORD schema in audittrace.models (distinct
    # from the SQLAlchemy ORM class of the same name in
    # audittrace.db.models, which has its own, separate entry in
    # _RENDER_ORM_IMPORTS below) — both resolve against their own
    # defining module, so the name collision is harmless.
    "InteractionRecord",
    "MetricsResponse",
    "SessionSaveRequest",
    "SessionSaveResponse",
    "SessionSummaryRequest",
    "SessionSummaryResponse",
)

# Pydantic models that live outside ``audittrace.models`` (importing their
# defining module at this module's top level would be circular — see the
# dataclass note below) — resolved lazily, same as everything else here.
_RENDER_MODEL_EXTRA_IMPORTS: tuple[tuple[str, str], ...] = (
    ("audittrace.services.episodic", "Document"),
)

# Plain dataclasses (not pydantic) that appear as @log_call parameter or
# return annotations. Imports are LOCAL/LAZY (never at this module's top
# level): ``services/memory_manifest.py``, ``services/semantic.py`` and
# ``services/trust_store.py`` all import ``log_call`` FROM this module, so
# importing them back here at import time would be circular. By the time
# any @log_call target actually runs, the whole app has finished
# importing — a lazy, cached resolution (``_get_render_allowlists``) is
# safe. Mirrors ``_extract_user_id``'s existing local-import pattern.
_RENDER_DATACLASS_IMPORTS: tuple[tuple[str, str], ...] = (
    ("audittrace.services.console_acl", "AclGrantOp"),
    ("audittrace.services.memory_manifest", "IndexStatusSummary"),
    ("audittrace.services.memory_manifest", "ManifestEntry"),
    ("audittrace.services.semantic", "SearchPage"),
    ("audittrace.services.trust_store", "TrustStoreBundle"),
    ("audittrace.services.trust_store", "TrustStoreMetadata"),
)

# SQLAlchemy ORM rows (not pydantic, not a stdlib dataclass) that appear as
# a @log_call return annotation. Rendered via the mapper's column_attrs
# (spec fact A8: ``_persist_interaction``'s 14 named fields carry no
# token and no UserContext — safe to render in full).
_RENDER_ORM_IMPORTS: tuple[tuple[str, str], ...] = (
    ("audittrace.db.models", "InteractionRecord"),
)

_render_allowlist_cache: dict[str, frozenset[type]] | None = None


def _get_render_allowlists() -> tuple[
    frozenset[type], frozenset[type], frozenset[type]
]:
    """Lazily resolve + cache the three structural-allowlist class sets.

    Returns ``(models, dataclasses, orm_models)``. See the module import
    comments above for why these imports must be lazy, not top-level.
    """
    global _render_allowlist_cache
    if _render_allowlist_cache is not None:
        return (
            _render_allowlist_cache["models"],
            _render_allowlist_cache["dataclasses"],
            _render_allowlist_cache["orm"],
        )

    models: set[type] = set()
    try:
        from audittrace import models as _models_mod

        for name in _RENDER_MODEL_NAMES:
            cls = getattr(_models_mod, name, None)
            if isinstance(cls, type):
                models.add(cls)
    except Exception:  # pragma: no cover - defensive
        pass
    for module_name, cls_name in _RENDER_MODEL_EXTRA_IMPORTS:
        try:
            import importlib

            cls = getattr(importlib.import_module(module_name), cls_name, None)
            if isinstance(cls, type):
                models.add(cls)
        except Exception:  # pragma: no cover - defensive
            pass

    dataclasses_set: set[type] = set()
    for module_name, cls_name in _RENDER_DATACLASS_IMPORTS:
        try:
            import importlib

            cls = getattr(importlib.import_module(module_name), cls_name, None)
            if isinstance(cls, type):
                dataclasses_set.add(cls)
        except Exception:  # pragma: no cover - defensive
            pass

    orm_set: set[type] = set()
    for module_name, cls_name in _RENDER_ORM_IMPORTS:
        try:
            import importlib

            cls = getattr(importlib.import_module(module_name), cls_name, None)
            if isinstance(cls, type):
                orm_set.add(cls)
        except Exception:  # pragma: no cover - defensive
            pass

    _render_allowlist_cache = {
        "models": frozenset(models),
        "dataclasses": frozenset(dataclasses_set),
        "orm": frozenset(orm_set),
    }
    return (
        _render_allowlist_cache["models"],
        _render_allowlist_cache["dataclasses"],
        _render_allowlist_cache["orm"],
    )


def _reset_render_allowlist_cache_for_tests() -> None:
    """Drop the cached allowlist sets. Test-only."""
    global _render_allowlist_cache
    _render_allowlist_cache = None


_RENDER_MAX_DEPTH = 6

# Scalar types rendered by SHAPE, never by the ``default=str`` escape
# hatch (ADDENDUM B N4). Checked as an isinstance tuple so subclasses
# (e.g. a custom ``Enum``) are caught too.
_SAFE_SCALAR_TYPES = (datetime, date, time_of_day, UUID, Decimal, Path, Enum)


def _render_safe_scalar(value: Any) -> Any:
    """Render one of ``_SAFE_SCALAR_TYPES`` to a JSON-safe value."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat()
    return str(value)  # UUID, Decimal, Path


class _RenderBudget:
    """SCRUB-EVENT-LOOP-STALL (v1.29.4, R2/B1 — ratified ADDENDUM A1 §2).

    Bounds the work ``_render_value_inner`` does by a running LOWER BOUND
    on the JSON length of what has been rendered so far, rather than by a
    count of entries/leaves — a shape-agnostic budget. ``used`` only ever
    UNDER-counts the true rendered length (G-P2b: ``used <= true length``
    always), so stopping early can only ever happen once the TRUE text is
    already at or past ``limit`` — the cut in ``render_payload`` then
    always lands inside text that was already fully, individually
    scrubbed (scrub-before-truncate, #385 A7/G2f — unchanged by this WU).

    ``stopped_early`` is a FLAG, read by callers after the walk, never an
    exception (R-d, ratified ADDENDUM A1 advisory): a budget stop must
    never look like the per-node ``try/except`` catching a real rendering
    failure (N4) — those two situations stay distinguishable.
    """

    __slots__ = ("limit", "used", "stopped_early")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.stopped_early = False

    def exhausted(self) -> bool:
        """True once ``used`` has reached ``limit`` — also flips
        ``stopped_early`` for the caller to read after the whole walk."""
        if self.used >= self.limit:
            self.stopped_early = True
            return True
        return False

    def charge(self, n: int) -> None:
        self.used += n


def render_value(
    value: Any, *, _depth: int = 0, budget: _RenderBudget | None = None
) -> Any:
    """Recursively render a value for a span attribute / log field.

    Structural allowlist (D2, N3): scalars render as themselves; ``str``
    leaves pass through ``scrub_text``; containers (list/tuple/set/dict)
    recurse; an allowlisted pydantic model / dataclass / ORM row renders
    its fields (``model_dump()`` / its fields, then recursion so a nested
    secret string still gets scrubbed); ``UserContext`` renders a FIXED
    projection (``user_id``/``username``/``agent_type``/``scopes``/
    ``token_id``/``is_admin`` — never ``extra``, which carries
    claim-derived PII); ``starlette.requests.Request`` renders
    ``method``+``path`` only (never headers/cookies/query params, which
    is where the bearer itself and session cookies live).

    **Everything else renders as its bare type name** (``<TypeName>``) —
    this is the fail-closed default that replaces ``json.dumps(...,
    default=str)``.

    Never raises (N4): any failure anywhere in this function (an
    unrenderable ``__repr__``, a surprising shape, an allowlist import
    failure) degrades to ``f"<{type(value).__name__}>"`` so one odd value
    can never break the logging/tracing path for the whole process — a
    raising renderer would silently DROP the log line or span attribute
    instead (fail-open), which is strictly worse than an opaque name.

    ``budget`` (SCRUB-EVENT-LOOP-STALL v1.29.4, R2/B1 — ratified ADDENDUM
    A1 §2): ``None`` (the default) is the ORIGINAL, unbudgeted contract —
    every node is visited, nothing is skipped — used by the ``extra=``
    formatter path (``StructuredFormatter.format``) and by anything that
    calls this function directly. When a ``_RenderBudget`` is supplied
    (``render_payload`` below), the SAME walker — one walker, not a
    second one (gate REJECT round 1 BLOCKER B1) — stops visiting further
    children once the budget is exhausted; every node visited before that
    point is rendered and scrubbed exactly as it would be unbudgeted
    (G-P7: the two must agree byte-for-byte up to the cut).
    """
    try:
        if budget is None:
            return _render_value_inner(value, _depth)
        return _render_value_inner(value, _depth, budget)
    except Exception:
        fallback = f"<{type(value).__name__}>"
        if budget is not None:
            budget.charge(len(json.dumps(fallback, ensure_ascii=False)))
        return fallback


def _render_value_inner(
    value: Any, depth: int, budget: _RenderBudget | None = None
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        if budget is not None:
            budget.charge(len(json.dumps(value, ensure_ascii=False)))
        return value
    if isinstance(value, str):
        scrubbed = scrub_text(value)
        if budget is not None:
            # +2 — the JSON string's surrounding quotes.
            budget.charge(len(scrubbed) + 2)
        return scrubbed
    if isinstance(value, bytes):
        rendered_bytes = f"<bytes:{len(value)}>"
        if budget is not None:
            budget.charge(len(json.dumps(rendered_bytes, ensure_ascii=False)))
        return rendered_bytes
    if isinstance(value, _SAFE_SCALAR_TYPES):
        rendered_scalar = _render_safe_scalar(value)
        if budget is not None:
            budget.charge(len(json.dumps(rendered_scalar, ensure_ascii=False)))
        return rendered_scalar

    if depth >= _RENDER_MAX_DEPTH:
        rendered_depth_cap = f"<{type(value).__name__}>"
        if budget is not None:
            budget.charge(len(json.dumps(rendered_depth_cap, ensure_ascii=False)))
        return rendered_depth_cap

    # UserContext — fixed projection. Checked before the generic dataclass
    # branch below because UserContext IS a dataclass but gets a narrower,
    # named shape rather than every field (``extra`` is PII, never a log
    # leaf). Local import — avoids a circular dependency with
    # ``audittrace.identity`` (which itself calls ``scrub_text`` from this
    # module's registry lookup).
    try:
        from audittrace.identity import UserContext

        if isinstance(value, UserContext):
            projection = {
                "user_id": value.user_id,
                "username": scrub_text(value.username),
                "agent_type": value.agent_type,
                "scopes": list(value.scopes),
                "token_id": value.token_id,
                "is_admin": value.is_admin,
            }
            if budget is not None:
                budget.charge(len(json.dumps(projection, ensure_ascii=False)))
            return projection
    except Exception:  # pragma: no cover - defensive
        pass

    # starlette.requests.Request — method + path only. Checked BEFORE the
    # Mapping branch below (gate REJECT round 1 BLOCKER B1, ratified
    # ADDENDUM A1 §2): ``starlette.requests.Request`` IS a
    # ``collections.abc.Mapping`` (it implements the ASGI scope mapping
    # protocol), so a budgeted walker that checked ``Mapping`` first would
    # render the raw ASGI scope — client IP, path params, headers —
    # instead of this fixed, PII-free projection. The branch ORDER here is
    # exactly the export's pre-fix order; G-P7 pins it with a differential
    # test against the unbudgeted path.
    try:
        from starlette.requests import Request as StarletteRequest

        if isinstance(value, StarletteRequest):
            projection = {"method": value.method, "path": value.url.path}
            if budget is not None:
                budget.charge(len(json.dumps(projection, ensure_ascii=False)))
            return projection
    except Exception:  # pragma: no cover - defensive
        pass

    if isinstance(value, Mapping):
        # F1 (gate REJECT round 1) — a non-str key (e.g. a UUID or a tuple,
        # both valid dict keys in Python but not in JSON) used to pass
        # through unchanged here, so the final ``json.dumps`` downstream
        # raised ``TypeError: keys must be str...`` and the WHOLE emission
        # — not just this one field — was lost (N4's fail-closed promise
        # broken for dict-keyed payloads). Route every key through
        # ``render_value`` too, then stringify the rendered result so it
        # is always JSON-key-safe: a UUID key's render is already a
        # scrubbed ``str``; a tuple key's render is a JSON-safe list,
        # stringified for use as a key (JSON itself has no tuple-as-key
        # concept either).
        #
        # SCRUB-EVENT-LOOP-STALL (v1.29.4, R2 — ratified ADDENDUM A1 §2,
        # A1-F10) — a non-str key is rendered UNBUDGETED (``budget=None``)
        # so it is still fully scrubbed, then charged ONCE as
        # ``len(rendered_key) + 4``; a str key is scrubbed directly and
        # charged the same way. Charging it twice (once inside an
        # unbudgeted-but-still-counted recursive call, once again here)
        # would overcount — not a safety problem (G-P2b only requires the
        # estimate never EXCEED the true length) but was a real bug this
        # WU's own corpus caught (A1-F10) before this fix: charging a
        # non-str key's render AND its own `len + 4` both.
        rendered: dict[str, Any] = {}
        for k, v in value.items():
            if budget is not None and budget.exhausted():
                break
            if isinstance(k, str):
                rendered_key = scrub_text(k)
            else:
                rendered_key = render_value(k, _depth=depth + 1)
                if not isinstance(rendered_key, str):
                    # RE-REVIEW 2026-10-14 advisory — ``ensure_ascii=False``
                    # here is not only a CHARGE-accuracy change: it also
                    # changes the EMITTED text for a non-ASCII tuple or
                    # UUID key (e.g. a tuple containing a non-ASCII
                    # string), which now renders with the literal
                    # character instead of a ``\uXXXX`` escape. Stated
                    # explicitly because it is an observable output
                    # change, not merely an internal estimate change.
                    rendered_key = json.dumps(
                        rendered_key, default=str, ensure_ascii=False
                    )
            if budget is not None:
                budget.charge(len(rendered_key) + 4)
            rendered[rendered_key] = render_value(v, _depth=depth + 1, budget=budget)
        return rendered
    if isinstance(value, (list, tuple, set, frozenset)):
        # R2/B1 estimator (ratified ADDENDUM A1 §2): "+2 per sequence
        # item" — one `", "` separator (2 chars) plus the closing
        # bracket's own contribution, charged per item rather than
        # tracked by position. RE-REVIEW 2026-10-14 R4 (orchestrator
        # ruling) — fix round 1 replaced this with a position-aware
        # "+1 for every item but the first", on the theory that the
        # flat "+2" double-counted against the leaf charge's own quote
        # overhead and was a real G-P2b violation. That theory was
        # WRONG and is retracted here: measured over a 20,000-value
        # random grid (nested str/list/dict, escapes, non-ASCII,
        # limits 1-300), BOTH "+2" (0 violations, 9603 early stops) and
        # "+1 non-first" (0 violations, 9593 early stops) are valid
        # lower bounds. `json.dumps`'s real item separator is `", "` —
        # 2 chars, not 1 — and the closing bracket is still charged
        # even on an early stop, so "+2" was never an over-count.
        # Reverted to the ratified "+2"; see
        # ``test_g_p2d_suffix_fires_on_stopped_early_even_at_exact_limit``
        # for the ADDENDUM A1 fixture this restores and
        # ``test_g_p2b_n08_neuter_increasing_the_list_charge_reddens``
        # for the guard that still catches a GENUINE increase (e.g. to
        # "+3") over this baseline.
        rendered_list: list[Any] = []
        for item in value:
            if budget is not None and budget.exhausted():
                break
            if budget is not None:
                budget.charge(2)  # separator/bracket overhead per item
            rendered_list.append(render_value(item, _depth=depth + 1, budget=budget))
        return rendered_list

    render_models, render_dataclasses, render_orm = _get_render_allowlists()

    try:
        from pydantic import BaseModel

        if isinstance(value, BaseModel):
            if type(value) in render_models:
                return render_value(
                    value.model_dump(mode="json"), _depth=depth + 1, budget=budget
                )
            rendered_model = f"<{type(value).__name__}>"
            if budget is not None:
                budget.charge(len(json.dumps(rendered_model, ensure_ascii=False)))
            return rendered_model
    except Exception:  # pragma: no cover - defensive
        pass

    if is_dataclass(value) and not isinstance(value, type):
        if type(value) in render_dataclasses:
            return render_value(
                {f.name: getattr(value, f.name) for f in dataclass_fields(value)},
                _depth=depth + 1,
                budget=budget,
            )
        rendered_dc = f"<{type(value).__name__}>"
        if budget is not None:
            budget.charge(len(json.dumps(rendered_dc, ensure_ascii=False)))
        return rendered_dc

    if type(value) in render_orm:
        try:
            from sqlalchemy import inspect as sa_inspect

            mapper = sa_inspect(type(value))
            return render_value(
                {
                    attr.key: getattr(value, attr.key, None)
                    for attr in mapper.column_attrs
                },
                _depth=depth + 1,
                budget=budget,
            )
        except Exception:  # pragma: no cover - defensive
            rendered_orm = f"<{type(value).__name__}>"
            if budget is not None:
                budget.charge(len(json.dumps(rendered_orm, ensure_ascii=False)))
            return rendered_orm

    rendered_fallback = f"<{type(value).__name__}>"
    if budget is not None:
        budget.charge(len(json.dumps(rendered_fallback, ensure_ascii=False)))
    return rendered_fallback


def render_payload(value: Any, *, limit: int) -> str:
    """Serialise a value for a span attribute / DEBUG log field.

    D1 — the single serialiser that replaces every ``json.dumps(...,
    default=str)`` / bare ``str(...)`` call site (spec fact F6: four emit
    sites in this module plus the structured-log formatter's own
    ``json.dumps``). No ``default=`` — ``render_value`` already made the
    value JSON-safe, so a ``TypeError`` here would mean the allowlist
    machinery itself is broken, not that some odd input needs rescuing.

    Scrub happens INSIDE ``render_value`` (every ``str`` leaf, even one
    discovered deep inside a budgeted walk — see below); truncation
    happens AFTER encoding. Order matters (G2f): scrub-before-truncate.
    Truncating first can cut a token in half, leaving an unscrubbed prefix
    that is no longer JWT-shaped and skips the registry match too (spec
    fact A7 — the escape this ordering closes).

    SCRUB-EVENT-LOOP-STALL (v1.29.4, R2/B3 — ratified ADDENDUM A1 §4) —
    ``render_value`` is called with a ``_RenderBudget(limit)`` so the walk
    stops visiting further children once the running (lower-bound)
    estimate of the rendered length reaches ``limit`` — independent of how
    many entries/leaves the value contains (the v1.29.3 event-loop stall:
    a ``list[ManifestEntry]`` of 3000 rows cost seconds because every leaf
    was scrubbed AND its secret-window index rebuilt from scratch, even
    though only the first few entries ever survive the cut below). Every
    leaf that IS visited is still scrubbed in full before the cut — the
    budget bounds leaf COUNT, not leaf SIZE (named residual JWT-RESID-S8).

    The truncation suffix fires when the walk stopped early OR the
    encoded text is over ``limit`` — NOT only the latter (gate REJECT
    round 1 BLOCKER B3): the budget's estimate is a LOWER bound, so a walk
    that stopped early can, for sufficiently simple values (plain ASCII
    leaves, no escaping), produce text whose exact length equals ``limit``
    — ``len(text) > limit`` alone would then be False and an item would be
    silently dropped from the JSON with no marker showing anything was
    cut. Checking ``stopped_early`` as well closes that edge.
    """
    budget = _RenderBudget(limit)
    text = json.dumps(render_value(value, budget=budget), ensure_ascii=False)
    if budget.stopped_early or len(text) > limit:
        text = text[:limit] + f"...[truncated {len(text)} chars]"
    return text


# Per-trace step counter (backlog #02). A process-global counter interleaved
# step numbers across concurrent requests. The ContextVar scopes the counter
# to the current async context (one per chat request).
_LANGGRAPH_STEP: contextvars.ContextVar[itertools.count[int]] = contextvars.ContextVar(
    "langgraph_step"
)


def _next_step() -> int:
    """Return the next langgraph_step for the current trace context."""
    try:
        counter = _LANGGRAPH_STEP.get()
    except LookupError:
        counter = itertools.count(1)
        _LANGGRAPH_STEP.set(counter)
    return next(counter)


def reset_langgraph_step() -> None:
    """Reset the per-trace step counter. Call at the start of each request."""
    _LANGGRAPH_STEP.set(itertools.count(1))


# Standard LogRecord attributes (CPython) + derived + already-renamed OTel fields —
# excluded when merging user-supplied ``extra=`` keys into the structured log (SPEC #457).
_RESERVED_LOGRECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
        "otelTraceID",
        "otelSpanID",
        "otelServiceName",
    }
)


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging to stdout.

    Field names map to Loki's ``| json | field="..."`` filter so the
    reconstructibility walkthrough (docs/reconstructibility-walkthrough.md
    Hop 5) works directly. ``trace_id`` is the canonical top-level
    name; OTel's ``LoggingInstrumentor`` attaches it as ``otelTraceID``
    on each ``LogRecord`` and we rename it here so operators do not
    have to remember the SDK's internal attr name.
    """

    # Map OTel LoggingInstrumentor attribute name → doc-canonical
    # top-level JSON field. Centralised so an OTel SDK rename only
    # changes this dict.
    _OTEL_FIELD_RENAMES: dict[str, str] = {
        "otelTraceID": "trace_id",
        "otelSpanID": "span_id",
        "otelServiceName": "service",
    }

    def format(self, record: logging.LogRecord) -> str:
        # SEC-JWT-IN-LANGFUSE-SPAN: message/exception/stack_info pass through
        # scrub_text (D3+D4); every extra= value passes through render_value
        # so it is JSON-safe BEFORE the final json.dumps below runs with no
        # ``default=`` — closing the same ``default=str`` escape hatch the
        # span-attribute sites close (spec fact A2's last sentence: a
        # ``Settings`` passed as a log ``extra`` used to render its secret
        # through exactly this ``json.dumps(default=str)`` call).
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(record.getMessage()),
        }
        for src, dst in self._OTEL_FIELD_RENAMES.items():
            value = getattr(record, src, None)
            if value:
                log_data[dst] = value
        for attr, value in record.__dict__.items():
            if attr not in _RESERVED_LOGRECORD_ATTRS and attr not in log_data:
                log_data[attr] = render_value(value)
        # Preserve exception tracebacks. logger.exception()/exc_info=True sets
        # record.exc_info; without this the JSON line carried only the message
        # and the stack was lost — which masked the summariser poison-pill root
        # cause (a background worker whose only signal is its log).
        if record.exc_info:
            log_data["exception"] = scrub_text(self.formatException(record.exc_info))
        if record.stack_info:
            log_data["stack_info"] = scrub_text(self.formatStack(record.stack_info))
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    structured: bool = False,
) -> None:
    """Configure root logging to stdout only.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        structured: If True, emit JSON lines; otherwise plain format.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper()))

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)


def _operation_name(func: Callable[..., Any], args: tuple[Any, ...]) -> str:
    if args and hasattr(args[0], "__class__") and not isinstance(args[0], type):
        cls = args[0].__class__.__name__
        if cls not in ("str", "int", "dict", "list", "tuple"):
            return f"{cls}.{func.__name__}"
    return f"{func.__module__}.{func.__name__}"


# Max chars stored in span input/output attributes — keeps Langfuse traces
# readable without bloating ClickHouse storage with full ADR documents.
_SPAN_ATTR_MAX_LEN = 4000


def _serialize_for_span(value: Any) -> str:
    """Serialise call args/result for span attributes (truncated, redacted).

    Delegates to ``render_payload`` (D1 — the one serialiser shared by
    every emission site) rather than ``json.dumps(..., default=str)``.
    """
    return render_payload(value, limit=_SPAN_ATTR_MAX_LEN)


# Map operation name patterns to logical components for Langfuse dashboards.
# Each span gets a `sovereign.component` attribute so charts can group by it
# instead of raw function names.
_COMPONENT_MAP: dict[str, str] = {
    "EpisodicService": "memory.episodic",
    "ProceduralService": "memory.procedural",
    "ConversationalService": "memory.conversational",
    "SemanticService": "memory.semantic",
    "ContextBuilder": "memory.builder",
    "PostgresFactory": "db.postgres",
    "ChromaDBFactory": "db.chromadb",
    "URLPostgres": "db.postgres",
    "InMemoryPostgres": "db.postgres",
    "HTTPChromaDB": "db.chromadb",
    "MockPostgres": "db.postgres",
    "MockChromaDB": "db.chromadb",
    "DependencyContainer": "core.di",
    "routes.chat": "route.chat",
    "routes.context": "route.context",
    "routes.audit": "route.audit",
    "routes.session": "route.session",
    "routes.health": "route.health",
    "routes.admin": "route.admin",
    "auth": "core.auth",
    "telemetry": "core.telemetry",
    # ADR-052 — PAdES trust store
    "TrustStoreProvider": "trust-store.provider",
    "TrustStoreBuilder": "trust-store.builder",
    "S3TrustStoreProvider": "trust-store.provider",
    "MockTrustStoreProvider": "trust-store.provider",
    "EuLotlTrustStoreBuilder": "trust-store.builder",
    "StaticTrustStoreBuilder": "trust-store.builder",
    # ADR-053 — Swiss federal TSL + composite
    "SwissTslTrustStoreBuilder": "trust-store.builder",
    "CompositeTrustStoreBuilder": "trust-store.builder",
}


def _classify_component(op: str) -> str:
    """Map an operation name to a logical component label."""
    for marker, component in _COMPONENT_MAP.items():
        if marker in op:
            return component
    return "other"


# Friendly kebab-case span names so Langfuse's trace graph view renders a
# clean flow chart on the left side of the trace detail page. Order matters:
# more specific markers must come first.
_SPAN_NAME_MAP: list[tuple[str, str]] = [
    # Routes
    ("routes.chat.list_models", "models-list"),
    ("routes.chat.chat_completions", "chat-completions"),
    ("routes.chat._extract_query", "chat-extract-query"),
    ("routes.chat._merge_system_message", "chat-merge-system"),
    ("routes.context.get_context", "context-build"),
    ("routes.audit.list_interactions", "audit-list"),
    ("routes.audit.create_interaction", "audit-create"),
    ("routes.session.save_session", "session-save"),
    ("routes.health.health_check", "health-check"),
    ("routes.health.metrics", "metrics-read"),
    # Memory layer services
    ("S3EpisodicService.load", "memory-episodic-load"),
    ("S3EpisodicService.search", "memory-episodic-search"),
    ("S3EpisodicService.as_context", "memory-episodic-context"),
    ("S3EpisodicService.read", "memory-episodic-read"),
    ("S3ProceduralService.load", "memory-procedural-load"),
    ("S3ProceduralService.search", "memory-procedural-search"),
    ("S3ProceduralService.as_context", "memory-procedural-context"),
    ("S3ProceduralService.read", "memory-procedural-read"),
    ("PostgresConversationalService.load_sessions", "memory-conversational-load"),
    ("PostgresConversationalService.save_session", "memory-conversational-save"),
    ("PostgresConversationalService.as_context", "memory-conversational-context"),
    ("ChromaSemanticService.search", "memory-semantic-search"),
    ("ChromaSemanticService.available_collections", "memory-semantic-collections"),
    # PAdES trust store (ADR-052)
    ("S3TrustStoreProvider.load", "trust-store-load"),
    ("S3TrustStoreProvider.store", "trust-store-store"),
    ("S3TrustStoreProvider.metadata", "trust-store-metadata"),
    ("MockTrustStoreProvider.load", "trust-store-load"),
    ("MockTrustStoreProvider.store", "trust-store-store"),
    ("MockTrustStoreProvider.metadata", "trust-store-metadata"),
    ("EuLotlTrustStoreBuilder.build", "trust-store-build-eu-lotl"),
    ("StaticTrustStoreBuilder.build", "trust-store-build-static"),
    ("SwissTslTrustStoreBuilder.build", "trust-store-build-swiss-tsl"),
    ("CompositeTrustStoreBuilder.build", "trust-store-build-composite"),
    ("get_trust_store_provider", "di-trust-store-provider"),
    ("get_trust_store_builder", "di-trust-store-builder"),
    ("refresh_trust_store", "admin-trust-store-refresh"),
    ("get_trust_store_metadata", "admin-trust-store-metadata"),
    # Context builder
    (
        "DefaultContextBuilder.build_system_context_with_stats",
        "memory-context-build-stats",
    ),
    ("DefaultContextBuilder.build_system_context", "memory-context-build"),
    # DB factories
    ("URLPostgresFactory.get_engine", "db-postgres-engine"),
    ("URLPostgresFactory.get_session_factory", "db-postgres-session-factory"),
    ("InMemoryPostgresFactory.get_engine", "db-postgres-engine"),
    ("InMemoryPostgresFactory.get_session_factory", "db-postgres-session-factory"),
    ("HTTPChromaDBFactory.get_client", "db-chromadb-client"),
    # Core
    ("DependencyContainer.register_factory", "di-register-factory"),
    ("DependencyContainer.get_factory", "di-get-factory"),
    ("DependencyContainer.create_instance", "di-create-instance"),
    ("DependencyContainer.get_instance", "di-get-instance"),
    ("register_default_dependencies", "di-bootstrap"),
    ("get_context_builder", "di-context-builder"),
    ("get_postgres_factory", "di-postgres-factory"),
    ("get_chromadb_factory", "di-chromadb-factory"),
    ("get_chromadb", "di-chromadb"),
    ("get_episodic_service", "di-episodic-service"),
    ("get_conversational_service", "di-conversational-service"),
]


def _friendly_span_name(op: str) -> str:
    """Map a verbose operation name to a kebab-case label for Langfuse graphs.

    Falls back to the original op name if no mapping matches so we never lose
    information for debugging.
    """
    for marker, label in _SPAN_NAME_MAP:
        if marker in op:
            return label
    return op


def _extract_user_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Resolve the calling user's id for span tagging (Phase-2 recon).

    Two sources, in precedence:
      1. A ``UserContext`` positional or keyword argument on the decorated
         call. Most memory-service methods accept it as the first positional
         argument per ADR-026, so finding it is O(1).
      2. The ``audittrace.db.rls._current_user_id`` ContextVar, which the
         auth middleware binds on every authenticated request.

    Returns ``None`` when neither yields an id (sentinel pre-auth calls,
    unit tests without a request scope). Imports are local to avoid
    a circular module dependency with ``audittrace.identity``/``db.rls``.
    """
    user_ctx_cls: Any = None
    try:
        from audittrace.identity import UserContext

        user_ctx_cls = UserContext
    except Exception:  # pragma: no cover - defensive: identity always importable
        pass

    if user_ctx_cls is not None:
        for arg in args:
            if isinstance(arg, user_ctx_cls):
                uid = getattr(arg, "user_id", None)
                if uid:
                    return str(uid)
        for value in kwargs.values():
            if isinstance(value, user_ctx_cls):
                uid = getattr(value, "user_id", None)
                if uid:
                    return str(uid)

    try:
        from audittrace.db.rls import current_user_id

        result = current_user_id()
        return str(result) if result is not None else None
    except Exception:  # pragma: no cover - defensive
        return None


# DEBUG log lines use a tighter cap than span attributes — readable in a
# terminal, not a document viewer.
_DEBUG_LOG_MAX_LEN = 500


def _record_span_error(span: Any, exc: Exception) -> None:
    """Record exactly ONE exception event on the active span (OTel or
    Langfuse), with the message/stacktrace/status description scrubbed.

    SEC-JWT-IN-LANGFUSE-SPAN (ADDENDUM A §3 B2) — this function becomes
    the SINGLE exception recorder. ``telemetry.start_span`` disables
    OTel's automatic ``record_exception``/``set_status_on_exception`` (it
    would otherwise record the exception a SECOND time, unscrubbed, when
    the ``with`` block re-raises past it — spec fact A2 measured exactly
    that: 2 exception events per failing call). The event is built here,
    scrubbed, via the public ``add_event``/``set_status`` API, which is
    also what ADDENDUM C's ``ScrubbingSpanProcessor`` re-scrubs at
    ``on_end`` as the export-time backstop — belt and suspenders, not a
    single point of failure.

    Langfuse-shaped spans (legacy — exposed ``update()`` instead of
    ``add_event``/``set_status``; kept for test/back-compat coverage) use
    the old ``update(level=..., status_message=...)`` call, also scrubbed.
    """
    if span is None:
        return
    try:
        if hasattr(span, "add_event") and hasattr(span, "set_status"):
            from opentelemetry.trace import Status, StatusCode

            stacktrace = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            span.add_event(
                "exception",
                {
                    "exception.type": type(exc).__name__,
                    "exception.message": scrub_text(str(exc)),
                    "exception.stacktrace": scrub_text(stacktrace),
                    "exception.escaped": "True",
                },
            )
            span.set_status(
                Status(StatusCode.ERROR, scrub_text(f"{type(exc).__name__}: {exc}"))
            )
        elif hasattr(span, "update"):
            span.update(
                level="ERROR",
                status_message=scrub_text(f"{type(exc).__name__}: {exc}"),
            )
    except Exception:  # pragma: no cover - defensive
        pass


def log_call(
    logger: logging.Logger | None = None,
    include_input: bool = True,
    include_output: bool = True,
    include_duration: bool = True,
) -> Callable[..., Any]:
    """Observability aspect: log + trace + meter every decorated call.

    Emits:
      - DEBUG log lines for INPUT/OUTPUT/DURATION (stdout only)
      - An OpenTelemetry span named after the operation
      - A histogram metric `sovereign.operation.duration` (seconds)

    Works for both sync and async functions.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        log = logger or logging.getLogger(func.__module__)
        is_async = asyncio.iscoroutinefunction(func)

        def _log_input(op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if include_input and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"INPUT {op}",
                    extra={
                        "operation": op,
                        "call_args": render_payload(
                            args[1:] if len(args) > 1 else (),
                            limit=_DEBUG_LOG_MAX_LEN,
                        ),
                        "call_kwargs": render_payload(kwargs, limit=_DEBUG_LOG_MAX_LEN),
                    },
                )

        def _log_output(op: str, result: Any) -> None:
            if include_output and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"OUTPUT {op}",
                    extra={
                        "operation": op,
                        "result": (
                            render_payload(result, limit=_DEBUG_LOG_MAX_LEN)
                            if result is not None
                            else None
                        ),
                    },
                )

        def _record(op: str, duration: float, error: str | None) -> None:
            if include_duration and log.isEnabledFor(logging.DEBUG):
                log.debug(
                    f"DURATION {op}: {duration:.4f}s",
                    extra={"operation": op, "duration": duration},
                )
            telemetry.record_operation(op, duration, error)

        def _set_span_input(
            span: Any, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> None:
            """Attach call arguments + component label to the span.

            Langfuse maps ``input.value`` to the trace UI's Input panel and
            renders custom attributes (``sovereign.component``) on the right
            so dashboards can group by component. ``langgraph.node`` triggers
            Langfuse's graph (flowchart) view.

            ``input.value`` is routed through ``telemetry.set_current_span_attributes``
            so the Langfuse SDK ``update_current_span(input=...)`` call lands
            on the first-class Input field of the active observation. Pre-fix
            we used ``span.set_attribute(...)`` directly, which only wrote to
            the underlying OTel attribute (nested deep in metadata.attributes)
            and left the Langfuse Input panel rendering ``undefined`` (ADR-024).
            """
            if span is None:
                return
            friendly = _friendly_span_name(op)
            try:
                span.set_attribute("sovereign.component", _classify_component(op))
                span.set_attribute("sovereign.operation", op)
                # LangGraph integration attributes — Langfuse's adapter
                # validates {langgraph_node: string, langgraph_step: number}
                # (underscores, not dots) and renders the trace as a graph
                # view in the left panel when both are present.
                span.set_attribute("langgraph_node", friendly)
                span.set_attribute("langgraph_step", _next_step())
                # Phase-2 reconstructibility: tag every span with the
                # caller's Keycloak sub when resolvable. Without this the
                # Langfuse dashboard cannot filter child observations by
                # user and the EU AI Act Art. 12 trail stops at the root.
                uid = _extract_user_id(args, kwargs)
                if uid:
                    span.set_attribute("langfuse.user.id", uid)
                    span.set_attribute("user.id", uid)
            except Exception:  # pragma: no cover
                pass
            if not include_input:
                return
            # Emit the richest payload we can so Langfuse's Input panel
            # shows real content on every span — never "undefined".
            # Method detection: args[0] is ``self`` only for BOUND methods.
            # Module-level functions (e.g. _extract_query(payload)) have
            # args[0] as the real first argument, so the old ``args[1:]``
            # dropped it silently. Detect with the same heuristic
            # _operation_name uses — args[0] is self if it's an instance
            # of a non-builtin class.
            starts_with_self = bool(
                args
                and hasattr(args[0], "__class__")
                and not isinstance(args[0], type)
                and args[0].__class__.__name__
                not in (
                    "str",
                    "int",
                    "dict",
                    "list",
                    "tuple",
                    "float",
                    "bool",
                    "NoneType",
                )
            )
            real_args = args[1:] if starts_with_self else args
            payload: dict[str, Any] = {}
            if real_args:
                payload["args"] = real_args
            if kwargs:
                payload["kwargs"] = kwargs
            if not payload:
                # No-arg call is a meaningful fact; tag it explicitly
                # instead of writing "{}" which Langfuse renders as empty.
                payload = {"called_with": "no arguments"}
            try:
                telemetry.set_current_span_attributes(
                    {"input.value": _serialize_for_span(payload)}
                )
            except Exception:  # pragma: no cover
                pass

        def _set_span_output(span: Any, result: Any) -> None:
            """Attach return value to the span as output.value attribute.

            Routed through ``telemetry.set_current_span_attributes`` so the
            Langfuse SDK Output panel populates (see ``_set_span_input`` docstring).

            ALWAYS emits output.value, including for ``result is None`` —
            returning None is a meaningful outcome (e.g. ``save()`` methods
            that mutate state and return nothing). The previous early-return
            on ``result is None`` left those spans rendering "undefined".
            """
            if span is None or not include_output:
                return
            try:
                telemetry.set_current_span_attributes(
                    {"output.value": _serialize_for_span(result)}
                )
            except Exception:  # pragma: no cover
                pass

        if is_async:

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                op = _operation_name(func, args)
                span_name = _friendly_span_name(op)
                _log_input(op, args, kwargs)
                start = time.perf_counter()
                err: str | None = None
                step_n = _next_step()
                span_metadata = {
                    "langgraph_node": span_name,
                    "langgraph_step": step_n,
                    "sovereign.component": _classify_component(op),
                    "sovereign.operation": op,
                }
                with telemetry.start_span(span_name, metadata=span_metadata) as span:
                    _set_span_input(span, op, args, kwargs)
                    try:
                        result = await func(*args, **kwargs)
                        _log_output(op, result)
                        _set_span_output(span, result)
                        return result
                    except Exception as e:
                        err = type(e).__name__
                        if _is_expected_client_error(e):
                            # Expected 4xx (auth/scope/not-found/validation):
                            # concise WARNING, no stack trace (F-L1).
                            log.warning("%s: %s", op, e, extra={"operation": op})
                        else:
                            log.error(
                                f"ERROR {op}: {e}",
                                exc_info=True,
                                extra={"operation": op},
                            )
                        _record_span_error(span, e)
                        raise
                    finally:
                        _record(op, time.perf_counter() - start, err)

            async_wrapper.__audittrace_log_call__ = True  # type: ignore[attr-defined]
            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            op = _operation_name(func, args)
            span_name = _friendly_span_name(op)
            _log_input(op, args, kwargs)
            start = time.perf_counter()
            err: str | None = None
            step_n = _next_step()
            span_metadata = {
                "langgraph_node": span_name,
                "langgraph_step": step_n,
                "sovereign.component": _classify_component(op),
                "sovereign.operation": op,
            }
            with telemetry.start_span(span_name, metadata=span_metadata) as span:
                _set_span_input(span, op, args, kwargs)
                try:
                    result = func(*args, **kwargs)
                    _log_output(op, result)
                    _set_span_output(span, result)
                    return result
                except Exception as e:
                    err = type(e).__name__
                    if _is_expected_client_error(e):
                        # Expected 4xx (auth/scope/not-found/validation):
                        # concise WARNING, no stack trace (F-L1).
                        log.warning("%s: %s", op, e, extra={"operation": op})
                    else:
                        log.error(
                            f"ERROR {op}: {e}",
                            exc_info=True,
                            extra={"operation": op},
                        )
                    _record_span_error(span, e)
                    raise
                finally:
                    _record(op, time.perf_counter() - start, err)

        sync_wrapper.__audittrace_log_call__ = True  # type: ignore[attr-defined]
        return sync_wrapper

    # Allow bare @log_call usage
    if callable(logger) and not isinstance(logger, logging.Logger):
        func, logger = logger, None
        return decorator(func)

    return decorator


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with standard configuration."""
    return logging.getLogger(name)
