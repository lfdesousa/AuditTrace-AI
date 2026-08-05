"""Deterministic Memory Curator runner (SDLC-ADR-002, tier-aware rebuild).

Mirrors :mod:`scripts.deploy.runner`: given a reachable memory server the runner
always performs the SAME ordered phases and reports what it DID. There is no
wall-clock branching in the control flow — timestamps are stamped into evidence
only, never read back to decide anything.

REWORK NOTICE (2026-08-05). The v1 runner (2026-08-03) FAILed independent review:
it invented a ``lessons`` collection and a ``CURATED_LAYERS`` list that conflated
physical S3 LAYERS (``episodic``/``procedural``) with ChromaDB vector COLLECTIONS
(``decisions``/``skills``/``semantic``), and it "verified retrievability" by hitting
``GET /memory/semantic?collection=<layer>&query=<marker>`` — a raw, unscoped LIST
endpoint that silently ignores an unsupported ``query`` param (verified against
``routes/memory.py::list_semantic`` — no ``query`` param exists there at all). This
rebuild is anchored on the ratified spec's two corrections:

* **2026-08-03 §CORRECTION** — curate the recall COLLECTIONS (``decisions``/
  ``skills``/``semantic``, :data:`scripts.curator.RECALL_COLLECTIONS`), never
  physical layers; ``lessons`` does not exist anywhere in ``src/`` and is dropped.
* **2026-08-05 AMENDMENT (§A-G)** — the substrate is now tier-aware (ADR-062 Phase
  B): every recall collection splits into a PRIVATE physical collection
  (``<name>_v2``, per-user) and a CORPUS physical collection (``<name>_corpus_v2``,
  shared, scope-gated). The Curator's tag now ALSO drives private→corpus
  promotion eligibility (§B), tenancy is token-derived NEVER metadata-derived
  (§C.1), dedup never merges across a tier boundary (§C.3), and
  verify-retrievability must prove WHO can recall a record and in WHICH tier (§D).

**Why "verify-retrievability" is a REST point-read, not a literal
``query=<marker>`` vector search (a documented, reasoned deviation from the
correction's literal wording — verified against the ACTUAL shipped code, per the
meta-lesson in [[CURATOR-MEMORY-MODEL-CORRECTION-20260803]]: "a reviewer's fix
framing is a claim, not a verdict — verify it against the code").** Grepping the
live routes confirms recall_decisions/recall_skills/recall_semantic are NOT a
standalone REST endpoint — ``ChromaSemanticService.search_page`` is reachable only
from INSIDE the LLM tool-call loop (``routes/_memory_tool_loop.py``), triggered by
a real model decision inside ``POST /v1/chat/completions``. The Bruno collection's
own helper for this (``memory/semantic/02-recall-paged.bru``) says so explicitly:
"Recall tools ... are NOT a standalone REST endpoint — they are invoked by the LLM
inside the /v1/chat/completions tool-call loop." Forcing every curated record
through a real LLM call would make this "deterministic, no wall-clock branching"
runner's core pipeline depend on live model latency (~75s per call, measured
2026-08-05) — unacceptable for a per-record gate. So verify-retrievability here is
a HYBRID:

1. **Structural, deterministic, tier-aware (every record)** — ``GET
   /memory/semantic/{collection}/{document_id}``. This exercises the EXACT SAME
   authorization predicate the recall path enforces
   (``ChromaSemanticService._tier_authorized`` / ``_require_corpus_scope`` —
   owner-or-corpus, scope-gated) via the service's ``get_document``, which
   ``read_semantic`` wraps. A 200 means "this caller's identity is authorized to
   retrieve this record" — the same authorization ``search()``/``search_page()``
   would apply; 404 (private, wrong owner) / 403 (corpus, no scope) means it is
   NOT retrievable for that caller. This is what proves tier-awareness (§D): the
   SAME URL probed with a DIFFERENT caller's token must flip from 200 to 404 for a
   private record (the isolation regression gate), and a corpus record must
   resolve 200 for ANY caller holding the corpus-read scope, not just its creator.
2. **Real recall-path proof, once per run (C8 self-log)** — a genuine
   ``POST /v1/chat/completions`` call with ``tool_choice`` forced to
   ``recall_decisions``, checked for the run's own marker in the model's final
   answer. This is the literal "same recall path the LLM uses" proof + satisfies
   [[feedback_e2e_includes_llm_call]] ("every E2E must include an LLM call") on the
   Curator's own dogfood record, without paying the LLM-latency cost per curated
   record.

Ordered phases (each logged; each declares the evidence it captures):

* **C0 Session-auth**  — the HARD, FIRST precondition (SDLC-ADR-003 §0.8, EU AI Act
  Art 14). A valid token AND a live human session (``scripts/audittrace-login
  --show``) must exist. The token is captured into memory and NEVER printed or
  logged; its ``exp`` claim is decoded to prove it is unexpired. Absent/expired →
  the WHOLE run ABORTS before any curation touches the store.
* **C1 Preflight-version** — amendment §E: ``GET /health`` must report
  ``version >= 1.19.2`` (the #426 tier-aware ``/memory/index`` fix) — pre-fix, a
  private-tier self-log re-index 400s. Absent/older → the WHOLE run ABORTS.
* **C2 Intake**        — enumerate each recall COLLECTION via ``GET
  /memory/semantic?collection=<name>`` (manifest-tracked rows only — see
  :func:`intake_records` for why ChromaDB-``discovered`` rows are skipped) since a
  watermark, then fetch each row's content via ``GET
  /memory/semantic/{collection}/{document_id}``.
* **C3 Tag**           — tag sensitivity with the SHARED taxonomy vocabulary
  (:mod:`scripts.curator`), so tags and the egress guards can never drift.
* **C4 Normalize**     — normalise to the memory-record schema (marker, ISO-
  absolute dates, ``[[links]]``, required fields). A record that cannot be
  normalised is FLAGGED, never silently dropped.
* **C5 Dedup**         — merge/supersede near-duplicates WITHIN a tier only
  (amendment §C.3) — a private record and its promoted corpus copy are never
  merged away.
* **C6 Tier-placement** — amendment §B, the new load-bearing job: a ``public-safe``
  PRIVATE record is ELIGIBLE for promotion; ANY sensitive flag means the record
  REFUSES promotion outright (never even attempts the HTTP call). Promotion is an
  explicit ``POST /memory/semantic`` write gated by
  ``memory:corpus:<collection>:write`` — a 403 is handled gracefully (denied, not
  fatal); tenancy for the promoted copy is stamped by the SERVER from the token,
  never read back from the record's own text (amendment §C.1).
* **C7 Verify-retrievability** — the DEFINING property, now TIER-AWARE (§D). See
  the hybrid-mechanism note above. A record that 200s on write but is NOT
  retrievable by its rightful caller is a FAIL; a PRIVATE record retrievable by a
  DIFFERENT subject is an ISOLATION REGRESSION — the sharpest possible FAIL.
* **C8 Self-log**      — log the curation run back to the memory server (ADR-059),
  prove it structurally retrievable, AND prove it recallable through the real
  ``recall_decisions`` tool path (the one genuine LLM call this runner makes).

**The runner NEVER self-certifies curation quality.** It reports what it did and
mechanically FAILS on the gates it owns (session-auth, preflight-version,
verify-retrievability, isolation); the quality verdict belongs to the independent
reviewer. ``certified: null`` makes the hand-off explicit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import ssl
import subprocess  # noqa: S404 - the runner shells out to the audittrace-login helper only
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from scripts.curator import (
    FLAG_COUNTERPARTY_NONPUBLIC,
    FLAG_INTERNAL_ID,
    FLAG_PII,
    FLAG_PRICING,
    FLAG_PUBLIC_SAFE,
    FLAG_SECURITY_FINGERPRINT,
    RECALL_COLLECTIONS,
    SENSITIVE_FLAGS,
)

logger = logging.getLogger("audittrace.curator.runner")

# Ordered phase identifiers — the determinism contract fixes this sequence.
PHASES = (
    "C0-session-auth",
    "C1-preflight-version",
    "C2-intake",
    "C3-tag",
    "C4-normalize",
    "C5-dedup",
    "C6-tier-placement",
    "C7-verify-retrievability",
    "C8-self-log",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_SCRIPT = REPO_ROOT / "scripts" / "audittrace-login"

# Front door (cert SAN); NOT audittrace.allaboutdata.eu on the laptop reference rig
# (portability invariant — parameterized per target, laptop default here).
DEFAULT_FRONT_DOOR = "https://audittrace.local"

# Amendment §E — the #426 fix (accept private-tier keys in single-file
# /memory/index) is required; pre-fix a private self-log re-index 400s.
MIN_SERVER_VERSION: tuple[int, int, int] = (1, 19, 2)

DEFAULT_LIST_LIMIT = 100
DEFAULT_DEDUP_THRESHOLD = 0.85

# The runner never certifies curation QUALITY. Stamped verbatim into the report so
# a reader (and the independent reviewer) sees the hand-off unambiguously.
VERIFICATION_DEFERRED = (
    "curation-quality verdict deferred to the independent reviewer; this runner "
    "reports what it did and mechanically fails only on session-auth, "
    "preflight-version, verify-retrievability, and isolation regressions"
)

# Exit codes (distinct so a failure mode is legible from the shell).
EXIT_OK = 0
EXIT_SESSION_ABORT = 2
EXIT_VERSION_ABORT = 3
EXIT_ISOLATION_REGRESSION = 4
EXIT_LOG_FAILED = 5
EXIT_UNRETRIEVABLE = 6


# ── errors ────────────────────────────────────────────────────────────────────


class SessionAuthError(RuntimeError):
    """Raised by C0 when there is no valid, unexpired human session.

    Aborts the WHOLE run before any curation touches the store — the Curator's
    hardest precondition (SDLC-ADR-003 §0.8, EU AI Act Art 14).
    """


class ServerVersionError(RuntimeError):
    """Raised by C1 when the deployed server is older than
    :data:`MIN_SERVER_VERSION` (amendment §E — the #426 tier-aware
    ``/memory/index`` fix is required). Aborts the WHOLE run — a private-tier
    self-log re-index would 400 on an unpatched server."""


class NormalizationError(ValueError):
    """Raised when a raw record cannot be normalised to the record schema.

    Caught per-record in C4 so ONE malformed write is FLAGGED, never silently
    dropped and never allowed to crash the whole curation run.
    """


class CurationLogError(RuntimeError):
    """Raised when C8 fails to log the curation run (the ADR-059 audit contract)."""

    def __init__(self, step: str, status: int, detail: str) -> None:
        super().__init__(f"curation self-log {step} failed (HTTP {status}): {detail}")
        self.step = step
        self.status = status


# ── external-effect seams (monkeypatched in tests; no live I/O in the suite) ──


def _login_show(login_script: Path) -> str:  # pragma: no cover - subprocess boundary
    """Run ``audittrace-login --show`` and return its stdout (the access token).

    The SOLE session-auth egress. Monkeypatched in tests so no subprocess runs. A
    non-zero exit becomes an empty return so C0 treats it as "no session"; the
    stdout (which carries the token) is handed straight to C0 and is NEVER logged.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(login_script), "--show"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _http_request(  # pragma: no cover - thin urllib egress boundary; monkeypatched
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    context: ssl.SSLContext | None = None,
) -> tuple[int, bytes]:
    """Perform an HTTP request; return ``(status, body)``.

    The SOLE memory-server egress point (front door). Monkeypatched in tests so no
    socket is opened. A transport failure returns status ``0`` so callers read it
    as a hard failure rather than an exception (mirrors ``scripts.deploy.memory``).
    """
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(  # noqa: S310 - https front door
            request, timeout=timeout, context=context
        ) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except OSError:
        return 0, b""


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into control flow."""
    return datetime.now(UTC).isoformat()


def _now_epoch() -> int:
    """Current epoch seconds — used only to compare against a token ``exp`` claim."""
    return int(datetime.now(UTC).timestamp())


# ── config + records ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CuratorConfig:
    front_door: str = DEFAULT_FRONT_DOOR
    since: str | None = None  # ISO watermark; None -> intake the server's newest page
    collections: tuple[str, ...] = RECALL_COLLECTIONS
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD
    list_limit: int = DEFAULT_LIST_LIMIT
    insecure: bool = False  # explicit dev opt-in for the self-signed laptop front door
    timeout: int = 30
    dry_run: bool = False
    out_dir: Path = REPO_ROOT / "scripts" / "curator" / "runs"
    # Amendment §D / §F(e) — an OPTIONAL second identity's token for the isolation
    # regression probe (the EV-13 two-identity shape). The Curator never fabricates
    # a second Keycloak identity itself (least-privilege — SDLC-ADR-003); an
    # operator supplies one (e.g. a second ``AUDITTRACE_TOKENS_DIR`` login) when
    # available. Absent -> the isolation probe is SKIPPED and flagged in the
    # report, never silently treated as "passed".
    isolation_probe_token: str | None = None
    min_server_version: tuple[int, int, int] = MIN_SERVER_VERSION
    skip_llm_recall_proof: bool = False  # test/CI escape hatch; live runs keep it on


@dataclass
class RawRecord:
    """A raw fleet write as intake sees it (dates/fields not yet normalised).

    ``tier`` is ALWAYS sourced from the list/read endpoint's own ``tier`` field
    (server-derived from which physical ChromaDB collection the row lives in —
    ``ChromaSemanticService._physical`` vs ``_physical_corpus``), NEVER parsed out
    of ``text``. This is the amendment §C.1 anti-forgery invariant: tenancy/tier is
    token-/store-derived, never caller- or content-supplied.
    """

    collection: str
    document_id: str
    text: str
    tier: str
    created_at_ms: int
    title: str = ""
    links: list[str] = field(default_factory=list)


@dataclass
class CuratedRecord:
    """A normalised, tagged, deduped, tier-placed record."""

    collection: str
    document_id: str
    marker: str
    text: str
    tier: str
    created_at: str
    links: list[str]
    tags: list[str]
    superseded: list[str] = field(default_factory=list)
    promotion_status: str = ""  # see phase_tier_placement for the closed vocabulary
    verified: bool = False
    verify_detail: str = ""
    isolation_regression: bool = False


@dataclass
class MergeEvent:
    """A logged dedup action — never a silent drop. ``tier`` records which tier
    partition the merge happened WITHIN (amendment §C.3 — dedup never crosses a
    tier boundary, so this is always the shared tier of both records)."""

    kept: str
    superseded: str
    similarity: float
    tier: str


@dataclass
class PhaseRecord:
    name: str
    status: str  # ok | skipped | planned | flagged | aborted
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


# ── C0 session-auth helpers (pure) ────────────────────────────────────────────


def decode_jwt_exp(token: str) -> int | None:
    """Return the ``exp`` (epoch seconds) claim from a JWT WITHOUT verifying it.

    Signature verification is the memory server's job on every request; C0 only
    needs to know the local token is well-formed and unexpired before it bothers
    the server. Returns ``None`` when the token is not a decodable three-part JWT
    or has no numeric ``exp``. NEVER logs the token.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return (
        int(exp)
        if isinstance(exp, (int, float)) and not isinstance(exp, bool)
        else None
    )


def decode_jwt_sub(token: str) -> str | None:
    """Return the ``sub`` claim from a JWT WITHOUT verifying it (evidence only —
    e.g. the self-log's own filename; never used as a security decision)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    sub = claims.get("sub") if isinstance(claims, dict) else None
    return sub if isinstance(sub, str) and sub else None


# ── C1 preflight-version helpers (pure) ───────────────────────────────────────


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse ``"1.19.2"`` -> ``(1, 19, 2)``. Ignores a leading ``v`` and any
    ``-suffix``/``+build`` metadata. Returns ``None`` on anything unparsable so
    the caller treats an odd version string as failing the floor, not crashing."""
    text = version[1:] if version.startswith("v") else version
    text = text.split("-", 1)[0].split("+", 1)[0]
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    a, b, c = (int(p) for p in parts)
    return a, b, c


def version_at_least(version: str, minimum: tuple[int, int, int]) -> bool:
    """``True`` iff *version* parses AND is ``>= minimum`` (tuple comparison)."""
    parsed = parse_semver(version)
    return parsed is not None and parsed >= minimum


# ── C3 tag helpers (pure) ─────────────────────────────────────────────────────

# One compiled pattern set PER sensitive flag. The key set MUST equal
# :data:`scripts.curator.SENSITIVE_FLAGS` (enforced by ``tagger_flag_coverage`` and
# a falsifiable test) so the tagger and the shared taxonomy can never drift apart.
_TAG_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    FLAG_PII: (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),),
    FLAG_PRICING: (
        re.compile(r"(?i)\b(?:chf|eur|usd|gbp)\s?\d"),
        re.compile(r"[$€£]\s?\d"),
        re.compile(r"(?i)\b(?:pricing|price|quote|invoice|discount)\b"),
    ),
    FLAG_COUNTERPARTY_NONPUBLIC: (
        re.compile(r"(?i)\b(?:counterparty|non-public|nonpublic)\b"),
        re.compile(r"\[\[private[-\w]*\]\]"),
    ),
    FLAG_INTERNAL_ID: (
        re.compile(r"#\d+"),
        re.compile(r"(?i)\bADR-\d+"),
        re.compile(r"(?i)\bmigration\s+\d+"),
    ),
    FLAG_SECURITY_FINGERPRINT: (
        re.compile(r"sha256:[0-9a-f]{16,}"),
        re.compile(r"-----BEGIN [A-Z ]+-----"),
        re.compile(r"(?:[0-9A-Fa-f]{2}:){7,}[0-9A-Fa-f]{2}"),
    ),
}


def tagger_flag_coverage() -> bool:
    """True iff the tagger covers EXACTLY the shared taxonomy's sensitive flags.

    The anti-drift assertion: if a flag is added to the taxonomy but not the
    tagger (or vice versa) this returns False. A falsifiable test pins it True.
    """
    return set(_TAG_PATTERNS) == set(SENSITIVE_FLAGS)


def tag_sensitivity(text: str) -> list[str]:
    """Tag ``text`` with sensitivity flags, in canonical order.

    Multi-label: a record can be both ``internal-id`` and ``security-fingerprint``.
    Returns ``["public-safe"]`` (the default) only when NO sensitive pattern
    matched.
    """
    matched = [
        flag
        for flag in SENSITIVE_FLAGS
        if any(pattern.search(text) for pattern in _TAG_PATTERNS[flag])
    ]
    return matched or [FLAG_PUBLIC_SAFE]


def merge_tags(*groups: list[str]) -> list[str]:
    """Union tag groups in canonical order; drop ``public-safe`` if anything else
    is present (a record that is sensitive in either half is sensitive)."""
    union = set().union(*groups)
    sensitive = [flag for flag in SENSITIVE_FLAGS if flag in union]
    return sensitive or [FLAG_PUBLIC_SAFE]


# ── C4 normalize helpers (pure) ───────────────────────────────────────────────

_LINK_RE = re.compile(r"\[\[[^\[\]]+\]\]")
_MARKER_RE = re.compile(r"^marker:\s*(\S+)", re.MULTILINE)


def normalize_iso_date(value_ms: int | None) -> str:
    """Convert an epoch-millisecond value to an ISO-8601 UTC string (absolute).

    ``None``/``0``/negative stamps NOW (evidence-only, matches "date unknown" —
    the manifest doesn't always carry ``created_at_ms`` for discovered rows).
    """
    if not value_ms or value_ms <= 0:
        return _now_iso()
    return datetime.fromtimestamp(value_ms / 1000, tz=UTC).isoformat()


def extract_marker(text: str, document_id: str) -> str:
    """Pull a ``marker: XYZ`` line out of *text* (the fleet's own writing
    convention — see ``_build_run_record`` below); falls back to the
    document_id when the record carries no explicit marker line."""
    m = _MARKER_RE.search(text)
    return m.group(1) if m else document_id


def extract_links(text: str, extra: list[str]) -> list[str]:
    """Collect ``[[wiki-style]]`` links from ``text`` plus any explicit ``extra``,
    de-duplicated, order-preserving."""
    ordered: list[str] = []
    for link in _LINK_RE.findall(text) + list(extra):
        if link not in ordered:
            ordered.append(link)
    return ordered


def normalize_record(raw: RawRecord, tags: list[str]) -> CuratedRecord:
    """Normalise a raw write to the curated record schema.

    Required fields (``document_id``, ``text``) and a valid ``collection`` are
    enforced; violations raise :class:`NormalizationError`, which C4 flags
    per-record. ``tier`` is copied through UNCHANGED from the raw record — see
    :class:`RawRecord`'s docstring for why this must never be re-derived from
    ``text``.
    """
    if not raw.document_id or not raw.document_id.strip():
        raise NormalizationError("record missing required field 'document_id'")
    if not raw.text or not raw.text.strip():
        raise NormalizationError(
            f"record {raw.document_id!r} missing required field 'text'"
        )
    if raw.collection not in RECALL_COLLECTIONS:
        raise NormalizationError(
            f"record {raw.document_id!r} has unknown collection {raw.collection!r} "
            f"(expected one of {RECALL_COLLECTIONS})"
        )
    return CuratedRecord(
        collection=raw.collection,
        document_id=raw.document_id,
        marker=extract_marker(raw.text, raw.document_id),
        text=raw.text.strip(),
        tier=raw.tier,
        created_at=normalize_iso_date(raw.created_at_ms),
        links=extract_links(raw.text, raw.links),
        tags=list(tags),
    )


# ── C5 dedup helpers (pure) ───────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _signature(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets (1.0 for two empty sets)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _merge_links(primary: list[str], other: list[str]) -> list[str]:
    ordered = list(primary)
    for link in other:
        if link not in ordered:
            ordered.append(link)
    return ordered


def dedup_records(
    records: list[CuratedRecord], threshold: float
) -> tuple[list[CuratedRecord], list[MergeEvent]]:
    """Merge/supersede near-duplicates WITHIN one tier partition; NEVER silently
    drop. Callers MUST pre-partition by tier (:func:`dedup_within_tiers`) — this
    function has no tier awareness of its own, by design, so a caller cannot
    accidentally feed it a cross-tier batch without going through the partitioning
    step that keeps amendment §C.3 true.

    Two records collide when they share a ``document_id`` OR their token-set
    Jaccard similarity is ``>= threshold``. The survivor is the one with the later
    ``created_at``; the older id is recorded on the survivor's ``superseded`` list
    and emitted as a :class:`MergeEvent`. Tags and links are unioned.
    """
    survivors: list[CuratedRecord] = []
    merges: list[MergeEvent] = []
    for record in records:
        signature = _signature(record.text)
        match_index = -1
        similarity = 0.0
        for index, survivor in enumerate(survivors):
            if survivor.document_id == record.document_id:
                match_index, similarity = index, 1.0
                break
            score = jaccard(signature, _signature(survivor.text))
            if score >= threshold:
                match_index, similarity = index, score
                break
        if match_index < 0:
            survivors.append(record)
            continue
        survivor = survivors[match_index]
        older, newer = sorted((survivor, record), key=lambda r: r.created_at)
        newer.superseded = sorted(
            set(newer.superseded) | set(older.superseded) | {older.document_id}
        )
        newer.links = _merge_links(newer.links, older.links)
        newer.tags = merge_tags(newer.tags, older.tags)
        survivors[match_index] = newer
        merges.append(
            MergeEvent(
                kept=newer.document_id,
                superseded=older.document_id,
                similarity=similarity,
                tier=newer.tier,
            )
        )
    return survivors, merges


def dedup_within_tiers(
    records: list[CuratedRecord], threshold: float
) -> tuple[list[CuratedRecord], list[MergeEvent]]:
    """Partition *records* by ``tier`` and dedup EACH partition independently
    (amendment §C.3): "a private record and its promoted corpus copy are NOT
    duplicates to be merged away; dedup runs WITHIN a tier." A private draft and
    its corpus-promoted twin therefore both survive, un-merged, even though their
    text is near-identical."""
    by_tier: dict[str, list[CuratedRecord]] = {}
    for record in records:
        by_tier.setdefault(record.tier, []).append(record)
    survivors: list[CuratedRecord] = []
    merges: list[MergeEvent] = []
    for tier in sorted(by_tier):
        tier_survivors, tier_merges = dedup_records(by_tier[tier], threshold)
        survivors.extend(tier_survivors)
        merges.extend(tier_merges)
    return survivors, merges


# ── C6 tier-placement helpers (pure) ──────────────────────────────────────────


def promotion_eligible(tags: list[str]) -> bool:
    """Amendment §B: ``public-safe`` (and ONLY ``public-safe`` — no sensitive
    flag riding alongside it) is eligible for private→corpus promotion. ANY of
    ``pii``/``pricing``/``counterparty-nonpublic``/``internal-id``/
    ``security-fingerprint`` means the record MUST STAY PRIVATE — never promoted.
    This is a pure, client-side REFUSAL: an ineligible record never even reaches
    the HTTP promote call (falsifiability gate (c))."""
    return tags == [FLAG_PUBLIC_SAFE]


# ── memory-server I/O (built on the monkeypatchable ``_http_request`` seam) ────

_PROMOTION_NOT_ELIGIBLE = "refused-sensitive"
_PROMOTION_ALREADY_CORPUS = "n/a-already-corpus"
_PROMOTION_PROMOTED = "promoted"
_PROMOTION_DENIED_SCOPE = "denied-scope"
_PROMOTION_ERROR = "promotion-error"


def _normalize_front_door(url: str) -> str:
    """Validate + normalise the front-door base URL with an EXACT scheme/host
    check. Uses :func:`urllib.parse.urlparse` — NOT substring matching — so a
    hostile URL such as ``https://evil/?x=audittrace`` cannot slip past (the
    CodeQL ``py/incomplete-url-substring`` class). Rejects anything that is not an
    http(s) URL carrying a hostname."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"front-door must be an http(s) URL with a host: {url!r}")
    return url.rstrip("/")


def _ssl_context(insecure: bool) -> ssl.SSLContext | None:
    """STRICT TLS by default (``None`` -> system trust). ``insecure=True`` is an
    EXPLICIT dev opt-in for the self-signed laptop front door (curl -k
    equivalent)."""
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def _err_detail(body: bytes) -> str:
    parsed = _parse_json(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    text = body.decode("utf-8", "replace").strip()
    return text[:200] if text else "no response body"


def check_server_version(cfg: CuratorConfig) -> tuple[bool, str]:
    """Amendment §E: ``GET /health`` (unauthenticated) and compare its
    ``version`` field against :attr:`CuratorConfig.min_server_version`. Returns
    ``(ok, detail)`` — never raises; the caller (C1) decides whether to abort."""
    base = _normalize_front_door(cfg.front_door)
    status, resp = _http_request(
        "GET",
        f"{base}/health",
        timeout=cfg.timeout,
        context=_ssl_context(cfg.insecure),
    )
    if status != 200:
        return False, f"GET /health returned HTTP {status}"
    parsed = _parse_json(resp)
    version = parsed.get("version") if isinstance(parsed, dict) else None
    if not isinstance(version, str):
        return False, "GET /health response carried no 'version' field"
    minimum = ".".join(str(p) for p in cfg.min_server_version)
    if not version_at_least(version, cfg.min_server_version):
        return False, f"server version {version} < required {minimum}"
    return True, f"server version {version} >= required {minimum}"


def list_semantic_collection(
    cfg: CuratorConfig, token: str, collection: str
) -> list[dict[str, Any]]:
    """``GET /memory/semantic?collection=<name>`` — the manifest+ChromaDB-merged
    list (NOT a vector search — see the module docstring's §CORRECTION note).
    Best-effort: a non-200 response contributes nothing rather than aborting
    intake (the fleet keeps writing; a partial intake still curates what it
    can)."""
    base = _normalize_front_door(cfg.front_door)
    query = urlencode({"collection": collection, "limit": cfg.list_limit})
    status, body = _http_request(
        "GET",
        f"{base}/memory/semantic?{query}",
        _bearer(token),
        timeout=cfg.timeout,
        context=_ssl_context(cfg.insecure),
    )
    if status != 200:
        logger.warning(
            "intake: collection %s list returned HTTP %s; skipping", collection, status
        )
        return []
    parsed = _parse_json(body)
    items = parsed.get("items") if isinstance(parsed, dict) else None
    return [item for item in items if isinstance(item, dict)] if items else []


def read_semantic_doc(
    cfg: CuratorConfig, token: str, collection: str, document_id: str
) -> tuple[int, dict[str, Any] | None]:
    """``GET /memory/semantic/{collection}/{document_id}`` — the tier-aware,
    scope-gated point-read (:func:`ChromaSemanticService.get_document` under the
    hood). Returns ``(status, body_or_None)``. This is BOTH the intake content
    fetch AND the verify-retrievability probe (see module docstring §mechanism)."""
    base = _normalize_front_door(cfg.front_door)
    status, body = _http_request(
        "GET",
        f"{base}/memory/semantic/{collection}/{document_id}",
        _bearer(token),
        timeout=cfg.timeout,
        context=_ssl_context(cfg.insecure),
    )
    parsed = _parse_json(body) if status == 200 else None
    return status, parsed if isinstance(parsed, dict) else None


def _raw_from_list_item(
    item: dict[str, Any], collection: str
) -> tuple[str, int] | None:
    """Pull ``(document_id, created_at_ms)`` out of one list-endpoint row.

    Skips ChromaDB-``discovered`` (untracked) rows on purpose: their ``key`` is
    keyed off the PHYSICAL collection name (``<name>_v2/<id>`` — see
    ``_merge_semantic_with_chroma`` in ``routes/memory.py``), not the LOGICAL one,
    so re-deriving a clean ``document_id`` from it would require re-implementing
    that route's physical-name quirk here. Manifest-tracked rows (the common case
    for anything written through ``create_semantic``/the promote path) carry a
    clean ``<collection>/<document_id>`` key and are the only ones this pipeline
    curates — a deliberate, documented scoping choice, not an oversight.
    """
    if item.get("discovered"):
        return None
    if item.get("deleted_at_ms") is not None:
        return None
    key = str(item.get("key") or "")
    prefix = f"{collection}/"
    if not key.startswith(prefix):
        return None
    document_id = key[len(prefix) :]
    if not document_id:
        return None
    created_at_ms = item.get("created_at_ms")
    return document_id, int(created_at_ms) if isinstance(created_at_ms, int) else 0


def _since_epoch_ms(since: str | None) -> int:
    if not since:
        return 0
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def intake_records(cfg: CuratorConfig, token: str) -> list[RawRecord]:
    """Enumerate each configured recall COLLECTION and fetch full content for
    every row since the watermark. Two-step per collection (list, then read) —
    see :func:`list_semantic_collection` / :func:`read_semantic_doc`. Best-effort
    per row: a read failure (race with a concurrent delete) skips that row rather
    than aborting the whole collection."""
    since_ms = _since_epoch_ms(cfg.since)
    records: list[RawRecord] = []
    for collection in cfg.collections:
        for item in list_semantic_collection(cfg, token, collection):
            parsed = _raw_from_list_item(item, collection)
            if parsed is None:
                continue
            document_id, created_at_ms = parsed
            if since_ms and created_at_ms and created_at_ms < since_ms:
                continue
            status, doc = read_semantic_doc(cfg, token, collection, document_id)
            if status != 200 or doc is None:
                logger.warning(
                    "intake: %s/%s read returned HTTP %s; skipping",
                    collection,
                    document_id,
                    status,
                )
                continue
            content = doc.get("content")
            metadata = (
                doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            )
            if not isinstance(content, str) or not content.strip():
                continue
            records.append(
                RawRecord(
                    collection=collection,
                    document_id=document_id,
                    text=content,
                    # §C.1 anti-forgery invariant: tier comes from the API's own
                    # field (server-derived from which physical collection the
                    # row lives in), NEVER parsed out of `content`.
                    tier=str(metadata.get("tier") or "private"),
                    created_at_ms=created_at_ms,
                    title=str(item.get("title") or ""),
                )
            )
    return records


def promote_to_corpus(
    cfg: CuratorConfig, token: str, record: CuratedRecord
) -> tuple[int, dict[str, Any]]:
    """``POST /memory/semantic`` with ``tier=corpus`` — the ONE sanctioned
    private→corpus promotion path (amendment §B). Requires the caller's token to
    hold ``memory:corpus:<collection>:write`` (or admin) or the server 403s; this
    function does not pre-check the scope client-side — the server is the
    authority, and a 403 is a legitimate, gracefully-handled outcome (see
    :meth:`CuratorRunner.phase_tier_placement`), never a crash.
    """
    base = _normalize_front_door(cfg.front_door)
    payload = json.dumps(
        {
            "collection": record.collection,
            "document_id": record.document_id,
            "text": record.text,
            "tier": "corpus",
        }
    ).encode()
    status, body = _http_request(
        "POST",
        f"{base}/memory/semantic",
        {**_bearer(token), "Content-Type": "application/json"},
        payload,
        timeout=cfg.timeout,
        context=_ssl_context(cfg.insecure),
    )
    parsed = _parse_json(body)
    return status, parsed if isinstance(parsed, dict) else {"detail": _err_detail(body)}


def _multipart_body(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----audittrace-curator-{uuid.uuid4().hex}"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
    ).encode()
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    return preamble + content + epilogue, f"multipart/form-data; boundary={boundary}"


def upload_self_log(
    cfg: CuratorConfig, token: str, filename: str, text: str
) -> dict[str, Any]:
    """Upload the curation-run markdown to the caller's PRIVATE episodic tier
    (``POST /memory/upload?layer=episodic`` — ADR-062 Phase B WU-B5 default).
    Raises :class:`CurationLogError` on a non-200 (logging IS the audit
    contract)."""
    base = _normalize_front_door(cfg.front_door)
    context = _ssl_context(cfg.insecure)
    url = (
        f"{base}/memory/upload?{urlencode({'layer': 'episodic', 'filename': filename})}"
    )
    mp_body, mp_ct = _multipart_body(filename, text.encode())
    status, body = _http_request(
        "POST",
        url,
        {**_bearer(token), "Content-Type": mp_ct},
        mp_body,
        timeout=cfg.timeout,
        context=context,
    )
    if status != 200:
        raise CurationLogError("upload", status, _err_detail(body))
    parsed = _parse_json(body)
    return parsed if isinstance(parsed, dict) else {}


def index_self_log(cfg: CuratorConfig, token: str, key: str) -> dict[str, Any]:
    """``POST /memory/index?file=<key>&collections=decisions`` — single-file mode,
    which (post-#426) correctly resolves a private-tier key (``{sub}/episodic/…``)
    to the caller's PRIVATE physical ``decisions_v2`` collection. Raises
    :class:`CurationLogError` on a non-200."""
    base = _normalize_front_door(cfg.front_door)
    url = f"{base}/memory/index?{urlencode({'file': key, 'collections': 'decisions'})}"
    status, body = _http_request(
        "POST",
        url,
        _bearer(token),
        timeout=cfg.timeout,
        context=_ssl_context(cfg.insecure),
    )
    if status != 200:
        raise CurationLogError("index", status, _err_detail(body))
    parsed = _parse_json(body)
    return parsed if isinstance(parsed, dict) else {}


def self_log_document_id(filename: str) -> str:
    """Replicate ``routes/memory.py::_doc_id("decisions", filename, 0)`` so the
    runner can structurally verify its OWN self-log without a search — the
    self-log markdown is kept well under ``CHUNK_SIZE`` (1500 chars) so it is
    always a single chunk (``chunk_idx=0``)."""
    raw = f"decisions:{filename}:0"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def chat_recall_probe(
    cfg: CuratorConfig, token: str, tool_name: str, needle: str
) -> tuple[bool, str]:
    """``POST /v1/chat/completions`` with ``tool_choice`` forced to *tool_name* —
    the ONE genuine LLM-mediated recall call this runner makes (module docstring
    §mechanism, point 2). The server auto-injects every memory tool the caller is
    scoped for (``routes/chat.py::augmented_tools``), so no ``tools`` schema needs
    to travel in this request. Returns ``(found, raw_answer)`` — *found* is
    whether *needle* appears in the model's final answer text. Non-200 → not
    found (network/model failure degrades to "not proven", never to a crash)."""
    base = _normalize_front_door(cfg.front_door)
    payload = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f'Call the {tool_name} tool with query="{needle}". '
                        "Reply with ONLY the exact text of the needle if you find "
                        "it among the results, otherwise reply NOT_FOUND. No other "
                        "words."
                    ),
                }
            ],
            "stream": False,
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }
    ).encode()
    status, body = _http_request(
        "POST",
        f"{base}/v1/chat/completions",
        {**_bearer(token), "Content-Type": "application/json"},
        payload,
        timeout=max(cfg.timeout, 120),
        context=_ssl_context(cfg.insecure),
    )
    if status != 200:
        return False, f"HTTP {status}"
    parsed = _parse_json(body)
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    if not isinstance(choices, list) or not choices:
        return False, "no choices in response"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    answer = content if isinstance(content, str) else ""
    return needle in answer, answer


# ── the runner ────────────────────────────────────────────────────────────────


class CuratorRunner:
    """Executes the ordered curation phases and emits the non-self-certifying
    report."""

    def __init__(self, cfg: CuratorConfig) -> None:
        self.cfg = cfg
        self.records: list[PhaseRecord] = []
        # The scoped JWT lives ONLY here, in memory, for the run's duration. It is
        # NEVER placed in a PhaseRecord, the report, or a log line.
        self._token: str = ""
        self.raw: list[RawRecord] = []
        self.tags: dict[str, list[str]] = {}
        self.curated: list[CuratedRecord] = []
        self.merges: list[MergeEvent] = []
        self.flagged_records: list[str] = []
        self.unretrievable: list[str] = []
        self.isolation_regressions: list[str] = []
        self.isolation_probe_skipped: bool = False
        self.run_marker: str = _curation_run_marker()
        self.self_log: dict[str, Any] = {}
        self.self_log_recall_proof: dict[str, Any] = {}
        self.aborted: bool = False
        self.abort_reason: str = ""
        self.log_failed: bool = False

    def _record(self, name: str, status: str, **kw: Any) -> PhaseRecord:
        rec = PhaseRecord(name=name, status=status, **kw)
        self.records.append(rec)
        level = logging.ERROR if status in ("aborted", "flagged") else logging.INFO
        logger.log(level, "[%s] %s %s", name, status, rec.detail)
        return rec

    # -- C0 --

    def phase_session_auth(self) -> None:
        """HARD FIRST gate: a valid, unexpired human session must exist (§0.8)."""
        started = _now_iso()
        raw_output = _login_show(LOGIN_SCRIPT)
        lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
        token = lines[-1] if lines else ""
        if not token:
            self._abort(
                PHASES[0],
                "session",
                started,
                "no active human session (audittrace-login --show empty)",
            )
            raise SessionAuthError("no active human session")
        exp = decode_jwt_exp(token)
        if exp is None:
            self._abort(
                PHASES[0],
                "session",
                started,
                "session token is not a decodable JWT with an exp claim",
            )
            raise SessionAuthError("token not a decodable JWT")
        now = _now_epoch()
        if exp <= now:
            self._abort(
                PHASES[0], "session", started, f"session token expired {now - exp}s ago"
            )
            raise SessionAuthError("token expired")
        # Token valid — keep it in memory, NEVER log/print it or its length.
        self._token = token
        self._record(
            PHASES[0],
            "ok",
            detail=f"human session valid; token expires in {exp - now}s",
            evidence={"token_present": True, "expires_in_s": exp - now},
            started_at=started,
            ended_at=_now_iso(),
        )

    def _abort(self, phase: str, reason: str, started: str, detail: str) -> None:
        self._record(
            phase,
            "aborted",
            detail=f"ABORTED — {detail}",
            evidence={"reason": reason},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C1 --

    def phase_preflight_version(self) -> None:
        """Amendment §E: ``GET /health`` must report ``version >= 1.19.2``."""
        started = _now_iso()
        ok, detail = check_server_version(self.cfg)
        if not ok:
            self._abort(PHASES[1], "version", started, detail)
            raise ServerVersionError(detail)
        self._record(
            PHASES[1],
            "ok",
            detail=detail,
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C2 --

    def phase_intake(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[2],
                "planned",
                detail=f"would intake collections {self.cfg.collections} since {self.cfg.since}",
            )
            return
        started = _now_iso()
        self.raw = intake_records(self.cfg, self._token)
        self._record(
            PHASES[2],
            "ok",
            detail=f"intook {len(self.raw)} raw write(s) across {self.cfg.collections}",
            evidence={
                "count": len(self.raw),
                "collections": list(self.cfg.collections),
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C3 --

    def phase_tag(self) -> None:
        if self.cfg.dry_run:
            self._record(PHASES[3], "planned", detail="would tag sensitivity")
            return
        started = _now_iso()
        counts: dict[str, int] = {}
        for record in self.raw:
            flags = tag_sensitivity(record.text)
            self.tags[record.document_id] = flags
            for flag in flags:
                counts[flag] = counts.get(flag, 0) + 1
        self._record(
            PHASES[3],
            "ok",
            detail=f"tagged {len(self.raw)} record(s): {counts}",
            evidence={"tag_counts": counts},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C4 --

    def phase_normalize(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[4], "planned", detail="would normalize to record schema"
            )
            return
        started = _now_iso()
        normalized: list[CuratedRecord] = []
        for record in self.raw:
            tags = self.tags.get(record.document_id, [FLAG_PUBLIC_SAFE])
            try:
                normalized.append(normalize_record(record, tags))
            except NormalizationError as exc:
                self.flagged_records.append(record.document_id or "<no-id>")
                logger.warning("normalize FLAGGED a record: %s", exc)
        self.curated = normalized
        status = "flagged" if self.flagged_records else "ok"
        self._record(
            PHASES[4],
            status,
            detail=(
                f"normalized {len(self.curated)} record(s); "
                f"flagged {len(self.flagged_records)} malformed (never dropped silently)"
            ),
            evidence={"flagged": self.flagged_records},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C5 --

    def phase_dedup(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[5], "planned", detail="would dedup near-duplicates within tier"
            )
            return
        started = _now_iso()
        self.curated, self.merges = dedup_within_tiers(
            self.curated, self.cfg.dedup_threshold
        )
        self._record(
            PHASES[5],
            "ok",
            detail=f"{len(self.merges)} merge(s) WITHIN-tier; {len(self.curated)} survivor(s)",
            evidence={"merges": [asdict(m) for m in self.merges]},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C6 --

    def phase_tier_placement(self) -> None:
        """Amendment §B: sensitivity tag -> tier-placement triage. Promotion is
        the ONLY private->corpus path and is explicit, scoped, and audited
        (server-side ``_require_corpus_scope`` + ``_emit_write_audit``)."""
        if self.cfg.dry_run:
            self._record(
                PHASES[6], "planned", detail="would evaluate promotion eligibility"
            )
            return
        started = _now_iso()
        counts: dict[str, int] = {}
        for record in self.curated:
            if record.tier == "corpus":
                record.promotion_status = _PROMOTION_ALREADY_CORPUS
            elif not promotion_eligible(record.tags):
                # Falsifiability gate (c): NEVER even attempt the HTTP call for a
                # sensitive-tagged record.
                record.promotion_status = _PROMOTION_NOT_ELIGIBLE
            else:
                status, body = promote_to_corpus(self.cfg, self._token, record)
                if status == 200:
                    record.promotion_status = _PROMOTION_PROMOTED
                elif status == 403:
                    record.promotion_status = _PROMOTION_DENIED_SCOPE
                    logger.info(
                        "promotion denied (403) for %s/%s — curator lacks "
                        "memory:corpus:%s:write",
                        record.collection,
                        record.document_id,
                        record.collection,
                    )
                else:
                    record.promotion_status = _PROMOTION_ERROR
                    logger.warning(
                        "promotion of %s/%s failed: HTTP %s %s",
                        record.collection,
                        record.document_id,
                        status,
                        body,
                    )
            counts[record.promotion_status] = counts.get(record.promotion_status, 0) + 1
        self._record(
            PHASES[6],
            "ok",
            detail=f"tier-placement evaluated {len(self.curated)} record(s): {counts}",
            evidence={"promotion_counts": counts},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C7 --

    def phase_verify(self) -> None:
        """Amendment §D — tier-aware verify-retrievability. See module docstring
        §mechanism for why this is a structural point-read, not a literal
        ``query=<marker>`` vector search."""
        if self.cfg.dry_run:
            self._record(
                PHASES[7],
                "planned",
                detail="would verify tier-aware retrievability for each curated record",
            )
            return
        started = _now_iso()
        if self.cfg.isolation_probe_token is None:
            self.isolation_probe_skipped = True
        for record in self.curated:
            status, _ = read_semantic_doc(
                self.cfg, self._token, record.collection, record.document_id
            )
            found = status == 200
            record.verified = found
            record.verify_detail = (
                f"recallable (HTTP {status})"
                if found
                else f"NOT recallable (HTTP {status})"
            )
            if not found:
                self.unretrievable.append(f"{record.collection}/{record.document_id}")
                continue
            if record.tier == "private" and self.cfg.isolation_probe_token:
                probe_status, _ = read_semantic_doc(
                    self.cfg,
                    self.cfg.isolation_probe_token,
                    record.collection,
                    record.document_id,
                )
                if probe_status == 200:
                    record.isolation_regression = True
                    self.isolation_regressions.append(
                        f"{record.collection}/{record.document_id}"
                    )
        status = (
            "flagged" if (self.unretrievable or self.isolation_regressions) else "ok"
        )
        self._record(
            PHASES[7],
            status,
            detail=(
                f"verified {len(self.curated) - len(self.unretrievable)}/"
                f"{len(self.curated)} recallable"
                + (
                    f"; UNRETRIEVABLE: {self.unretrievable}"
                    if self.unretrievable
                    else ""
                )
                + (
                    f"; ISOLATION REGRESSION (hard FAIL): {self.isolation_regressions}"
                    if self.isolation_regressions
                    else ""
                )
                + (
                    "; isolation probe SKIPPED — no second identity configured"
                    if self.isolation_probe_skipped
                    else ""
                )
            ),
            evidence={
                "unretrievable": self.unretrievable,
                "isolation_regressions": self.isolation_regressions,
                "isolation_probe_skipped": self.isolation_probe_skipped,
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- C8 --

    def phase_self_log(self) -> None:
        if self.cfg.dry_run:
            self._record(
                PHASES[8],
                "planned",
                detail="would self-log the curation run and verify it recallable",
            )
            return
        started = _now_iso()
        filename = f"{self.run_marker}.md"
        text = self._build_run_record()
        upload_result = upload_self_log(self.cfg, self._token, filename, text)
        key = str(upload_result.get("key") or "")
        index_result = index_self_log(self.cfg, self._token, key)
        self.self_log = {"upload": upload_result, "index": index_result, "key": key}

        # Structural verify: the self-log must ITSELF be recallable.
        doc_id = self_log_document_id(filename)
        status, _ = read_semantic_doc(self.cfg, self._token, "decisions", doc_id)
        self_recallable = status == 200
        if not self_recallable:
            self.unretrievable.append(f"decisions/{doc_id} (self-log)")

        # The ONE genuine LLM-mediated recall proof this runner performs.
        if not self.cfg.skip_llm_recall_proof:
            found, answer = chat_recall_probe(
                self.cfg, self._token, "recall_decisions", self.run_marker
            )
            self.self_log_recall_proof = {"tool": "recall_decisions", "found": found}
            if not found:
                self.unretrievable.append(
                    f"decisions/{doc_id} (recall_decisions proof)"
                )

        self._record(
            PHASES[8],
            "ok" if self_recallable else "flagged",
            detail=(
                f"self-logged {self.run_marker}"
                + (
                    "; itself recallable"
                    if self_recallable
                    else "; SELF-LOG NOT recallable"
                )
                + (
                    f"; recall_decisions proof: {self.self_log_recall_proof.get('found')}"
                    if self.self_log_recall_proof
                    else ""
                )
            ),
            evidence={
                "self_log": self.self_log,
                "recallable": self_recallable,
                "recall_proof": self.self_log_recall_proof,
            },
            started_at=started,
            ended_at=_now_iso(),
        )

    def _build_run_record(self) -> str:
        promotions = {r.document_id: r.promotion_status for r in self.curated}
        lines = [
            f"marker: {self.run_marker}",
            "type: curation-run",
            f"generated_at: {_now_iso()}",
            "",
            "# Memory Curator run (SDLC-ADR-002, tier-aware)",
            "",
            f"- records intook: {len(self.raw)}",
            f"- records curated (survivors): {len(self.curated)}",
            f"- malformed flagged: {self.flagged_records}",
            f"- merges (within-tier): {len(self.merges)}",
            f"- promotion outcomes: {promotions}",
            f"- unretrievable (verify FAIL): {self.unretrievable}",
            f"- isolation regressions (hard FAIL): {self.isolation_regressions}",
            "",
            "[[project_agents_on_memory_server_self_improve]] [[project_memory_curator_agent]]",
        ]
        return "\n".join(lines) + "\n"

    # -- report --

    def build_report(self) -> dict[str, Any]:
        return {
            "schema_version": "2",
            "runner": "audittrace-memory-curator",
            "front_door": self.cfg.front_door,
            "dry_run": self.cfg.dry_run,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "log_failed": self.log_failed,
            "intook": len(self.raw),
            "curated": len(self.curated),
            "flagged_records": self.flagged_records,
            "merges": [asdict(m) for m in self.merges],
            "promotion_counts": _promotion_counts(self.curated),
            "unretrievable": self.unretrievable,
            "isolation_regressions": self.isolation_regressions,
            "isolation_probe_skipped": self.isolation_probe_skipped,
            "run_marker": self.run_marker,
            "self_log": self.self_log,
            "self_log_recall_proof": self.self_log_recall_proof,
            "phases": [asdict(rec) for rec in self.records],
            # ── the non-self-certifying contract ──
            "certified": None,
            "verification": VERIFICATION_DEFERRED,
            "generated_at": _now_iso(),
        }

    def phase_report(self) -> dict[str, Any]:
        out_dir = Path(self.cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")
        json_path = out_dir / f"curation-{stamp}.json"
        report = self.build_report()
        json_path.write_text(json.dumps(report, indent=2, default=str))
        (out_dir / f"curation-{stamp}.txt").write_text(self.human_summary(report))
        return report

    def human_summary(self, report: dict[str, Any]) -> str:
        lines = [
            "AuditTrace Memory Curator — report",
            "=" * 40,
            f"front door     : {report['front_door']}",
            f"dry run        : {report['dry_run']}",
            f"aborted        : {report['aborted']} ({report['abort_reason'] or 'n/a'})",
            f"intook         : {report['intook']}",
            f"curated        : {report['curated']}",
            f"merges         : {len(report['merges'])}",
            f"promotions     : {report['promotion_counts']}",
            f"unretrievable  : {report['unretrievable'] or 'none'}",
            f"isolation      : {report['isolation_regressions'] or 'none'}"
            + (" (PROBE SKIPPED)" if report["isolation_probe_skipped"] else ""),
            f"run marker     : {report['run_marker']}",
            "",
            "phases:",
        ]
        for rec in report["phases"]:
            lines.append(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
        lines += [
            "",
            f"certified     : {report['certified']}  (runner NEVER self-certifies quality)",
            f"verification  : {report['verification']}",
        ]
        return "\n".join(lines) + "\n"

    # -- orchestration --

    def run(self) -> dict[str, Any]:
        try:
            self.phase_session_auth()  # C0 — blocks the WHOLE run if it fails
            self.phase_preflight_version()  # C1 — blocks the WHOLE run if it fails
            self.phase_intake()
            self.phase_tag()
            self.phase_normalize()
            self.phase_dedup()
            self.phase_tier_placement()
            self.phase_verify()
            self.phase_self_log()
        except SessionAuthError:
            self.aborted = True
            self.abort_reason = "session"
        except ServerVersionError:
            self.aborted = True
            self.abort_reason = "version"
        except CurationLogError as exc:
            self.log_failed = True
            logger.error("curation self-log failed: %s", exc)
        return self.phase_report()


def _promotion_counts(records: list[CuratedRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        counts[r.promotion_status] = counts.get(r.promotion_status, 0) + 1
    return counts


def _curation_run_marker() -> str:
    stamp = (
        _now_iso().replace(":", "").replace("-", "").replace(".", "").replace("+", "Z")
    )
    return f"MEMORY-CURATOR-RUN-{stamp}"


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.curator.runner",
        description="Deterministic, tier-aware AuditTrace Memory Curator (does NOT self-certify).",
    )
    parser.add_argument("--front-door", default=DEFAULT_FRONT_DOOR)
    parser.add_argument("--since", default=None, help="ISO watermark for intake")
    parser.add_argument(
        "--collections",
        default=",".join(RECALL_COLLECTIONS),
        help="comma-separated recall collections to curate",
    )
    parser.add_argument(
        "--isolation-probe-token",
        default=None,
        help=(
            "an OPTIONAL second identity's bearer token for the isolation-"
            "regression probe (§D); absent -> the probe is SKIPPED and flagged"
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (self-signed laptop front door only)",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ordered plan; curate nothing"
    )
    parser.add_argument(
        "--skip-llm-recall-proof",
        action="store_true",
        help="skip the one genuine /v1/chat/completions recall proof in C8 (fast local runs)",
    )
    parser.add_argument("--out-dir", type=Path, default=CuratorConfig.out_dir)
    return parser


def config_from_args(args: argparse.Namespace) -> CuratorConfig:
    return CuratorConfig(
        front_door=args.front_door,
        since=args.since,
        collections=tuple(c.strip() for c in args.collections.split(",") if c.strip()),
        isolation_probe_token=args.isolation_probe_token,
        insecure=args.insecure,
        timeout=args.timeout,
        dry_run=args.dry_run,
        skip_llm_recall_proof=args.skip_llm_recall_proof,
        out_dir=args.out_dir,
    )


def exit_code_for(report: dict[str, Any]) -> int:
    if report["aborted"] and report["abort_reason"] == "session":
        return EXIT_SESSION_ABORT
    if report["aborted"] and report["abort_reason"] == "version":
        return EXIT_VERSION_ABORT
    if report["isolation_regressions"]:
        return EXIT_ISOLATION_REGRESSION
    if report["log_failed"]:
        return EXIT_LOG_FAILED
    if report["unretrievable"]:
        return EXIT_UNRETRIEVABLE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    runner = CuratorRunner(cfg)
    report = runner.run()
    print(runner.human_summary(report))
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(main())
