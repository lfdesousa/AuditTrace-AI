"""ADR-059 memory-wiring helper for the CD program (WS4).

The small, tested seam the two deploy agents use to (a) RECALL deploy lessons
from the internal memory server before acting and (b) LOG their deploy / verify
records back after — the *recall-before / log-after* discipline of ADR-059
(agents on the memory server that improve on their own history).

Two public functions with DELIBERATELY ASYMMETRIC failure contracts:

* :func:`recall_deploy_lessons` — **best-effort**. A recall miss must NEVER block
  a deploy. On ANY failure (HTTP error, transport failure, empty result, bad
  JSON) it logs a warning and returns ``[]``; it never raises. Recall of
  newly-indexed decisions is a KNOWN-DEGRADED path (#383): the LLM-driven
  ``recall_*`` tool loop may return nothing/irrelevant, so this helper reads the
  decisions layer directly through the front door and treats an empty answer as
  normal, not as an error.

* :func:`log_deploy_record` — **reliable**. Logging IS the ADR-059 contract, so a
  failure here is loud: it raises :class:`DeployLogError` with a clear message.
  The LOG side of the memory server works reliably even while recall is degraded.

**Method discipline (shared with the runners):**

* Single monkeypatchable network-egress seam, :func:`_http_request` (mirrors
  :func:`scripts.deploy.registry._http_get`). Tests substitute it; no sockets.
* The scoped JWT is loaded in-memory from ``~/.config/audittrace/tokens.json``
  (or the ``AUDITTRACE_TOKEN`` env) when not passed, used only as a Bearer
  header, and NEVER printed or written anywhere.
* Every front-door URL is validated with :func:`urllib.parse.urlparse` EXACT
  scheme/host checks — never a substring membership test on the raw URL string
  (the CodeQL ``py/incomplete-url-substring`` class that tripped WS2; the lesson
  "never substring-match URLs" is logged in the decisions layer).
* Every outbound recall GET carries :func:`_fleet_headers` — a UNIVERSAL,
  runtime-agnostic ``X-Agent-Role`` (``AUDITTRACE_AGENT_ROLE``, default
  ``"fleet"``) that the server-side ``classify_recall_source_from_request``
  classifier alone is enough to label ``source="fleet"`` for BOTH the local
  OpenCode fleet AND the Sonnet cloud fleet, plus a runtime-honest
  ``X-Source`` (``AUDITTRACE_AGENT_RUNTIME``-``AUDITTRACE_AGENT_ROLE``, e.g.
  ``opencode-builder`` / ``sonnet-reviewer``) for the audit trail
  (RECALL-METRIC-COVERAGE crux #4a, 2026-08-12).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse

from scripts.deploy.build_record import (
    BuildRecordValidationError,  # noqa: F401 - re-exported for callers
    validate_build_record,
)
from scripts.deploy.frontdoor import DEFAULT_FRONT_DOOR as _DEFAULT_FRONT_DOOR
from scripts.deploy.frontdoor import resolve_front_door

logger = logging.getLogger("audittrace.deploy.memory")

# Re-export DEFAULT_FRONT_DOOR for backward compatibility (external code may
# import it from memory; tests assert memory.DEFAULT_FRONT_DOOR == the shared
# module's value). The noqa silences ruff's "unused import" because the name
# is re-exported at the module level, not used in the function body.
DEFAULT_FRONT_DOOR = _DEFAULT_FRONT_DOOR  # noqa: F401
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "audittrace" / "tokens.json"

# The decisions collection is where deploy lessons are indexed (the ``collections``
# default of :func:`log_deploy_record`), so recall reads it back.
DECISIONS_COLLECTION = "decisions"
DEFAULT_RECALL_LIMIT = 25

# ── build-outcome tagging (corpus-hygiene guard, 2026-08-21) ──────────────────
# A build / review / deploy record MAY carry an explicit outcome so that recall
# can QUARANTINE drift trajectories (a stalled or failed build) from being
# recalled as if it were valid precedent — "gold to study, poison to recall".
# The tag rides in the record FILENAME as an ``.outcome-<value>`` infix
# (``2026-08-21-BUILD-456.outcome-stalled.md``) so it is visible in the recall
# listing's ``title`` / ``key`` / ``source`` WITHOUT fetching content.
BUILD_OUTCOMES = ("pass", "reject", "stalled", "failed")
# Outcomes dropped from recall by default. ``pass`` is a precedent; ``reject`` is
# a genuine lesson (the reviewer's mandate-to-fail verdict IS the value of the
# loop); only the incomplete/aborted trajectories are quarantined.
RECALL_QUARANTINED_OUTCOMES = frozenset({"stalled", "failed"})
_OUTCOME_INFIX = ".outcome-"

# Multipart field name MUST be ``file`` — the upload endpoint binds
# ``file: UploadFile = File(...)``.
_UPLOAD_FIELD = "file"


class DeployLogError(RuntimeError):
    """Raised when logging a deploy/verify record FAILS.

    Logging is the ADR-059 contract, so — unlike a recall miss — a log failure
    is not swallowed. Carries the failing step ("upload" | "index") and the HTTP
    status so the caller can reconstruct what went wrong.
    """

    def __init__(self, step: str, status: int, detail: str) -> None:
        super().__init__(f"deploy-record {step} failed (HTTP {status}): {detail}")
        self.step = step
        self.status = status


# ── external-effect indirection (monkeypatched in tests) ─────────────────────


def _http_request(  # pragma: no cover - thin urllib egress boundary; monkeypatched in tests
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    context: ssl.SSLContext | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Perform an HTTP request; return ``(status, response_headers, body)``.

    The SOLE network-egress point (front door). Monkeypatched in tests so no real
    socket is opened. A transport failure returns status ``0`` so callers read it
    as a hard failure rather than an exception. ``context`` (when set) is the
    unverified SSL context selected by ``insecure=True``.

    Real ``urlopen`` RAISES the whole transport-error class here, not just
    ``URLError``: a READ timeout surfaces as a bare ``TimeoutError`` (==
    ``socket.timeout``, an ``OSError``) that is NOT a ``URLError`` (the WS5
    finding). Catching ``OSError`` after ``HTTPError`` collapses the ENTIRE class
    — ``URLError``, ``TimeoutError``/``socket.timeout``, ``ConnectionError`` — to
    the status-``0`` sentinel in ONE place, so every caller maps a timeout to its
    own clean contract: ``recall_deploy_lessons`` → ``[]``, and
    ``log_deploy_record`` → a clean ``DeployLogError`` via its non-200 guard.
    """
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - https front door
            request, timeout=timeout, context=context
        ) as response:
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            payload = response.read()
        return status, resp_headers, payload
    except HTTPError as exc:
        return exc.code, {}, exc.read()
    except OSError:
        # URLError (an OSError), a bare TimeoutError/socket.timeout on a read
        # timeout, or any ConnectionError — one sentinel for the whole class.
        return 0, {}, b""


# ── shared helpers ────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — used only to name a synthesised record file."""
    return datetime.now(UTC).isoformat()


def ssl_context(insecure: bool) -> ssl.SSLContext | None:
    """STRICT TLS by default (``None`` → system trust store).

    ``insecure=True`` is an EXPLICIT dev/self-signed opt-in (the laptop front door
    presents a self-signed cert), equivalent to ``curl -k``. Nothing is weakened
    by default.
    """
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _normalize_front_door(url: str) -> str:
    """Validate + normalise the front-door base URL with an EXACT scheme/host check.

    Uses :func:`urllib.parse.urlparse` — NOT substring matching — so a hostile
    URL such as ``https://evil/?x=audittrace`` cannot slip past a naive substring
    membership test on the URL (the CodeQL ``py/incomplete-url-substring`` class).
    Rejects anything that is not an http/https URL carrying a hostname.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"front-door must be an http(s) URL with a host: {url!r}")
    return url.rstrip("/")


def _resolve_token(token: str | None, token_file: Path | None = None) -> str | None:
    """Resolve a scoped JWT: an explicit arg first, then ``AUDITTRACE_TOKEN`` env,
    then the token file's ``access_token``.

    ``token_file`` defaults to the module-level :data:`DEFAULT_TOKEN_FILE` looked
    up at CALL time (not bound as a default arg) so a test can monkeypatch it. The
    token is used only as a Bearer header and is NEVER logged or printed. Returns
    ``None`` when no token is available.
    """
    if token:
        return token
    env_token = os.environ.get("AUDITTRACE_TOKEN")
    if env_token:
        return env_token
    resolved_file = token_file if token_file is not None else DEFAULT_TOKEN_FILE
    try:
        data = json.loads(Path(resolved_file).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    resolved = data.get("access_token")
    return resolved if isinstance(resolved, str) and resolved else None


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# The fleet marker sent when no per-agent role is configured. NEVER
# "backoffice" — a caller of this module is, BY DEFINITION, a fleet/agent
# recall (never a human front-door read); see :func:`_fleet_headers`.
_FLEET_DEFAULT_ROLE = "fleet"


def _fleet_headers() -> dict[str, str]:
    """Attribution headers stamped on EVERY outbound fleet recall GET.

    RECALL-METRIC-COVERAGE crux #4a (2026-08-12, independent review of
    ``ef49935``): the server-side classifier
    ``audittrace.services.recall_telemetry.classify_recall_source_from_request``
    labels a recall ``source="fleet"`` when the request carries an
    ``X-Source`` header starting ``"opencode-"`` (case-insensitive) OR a
    non-empty ``X-Agent-Role`` header — the SAME attribution
    ``routes/chat.py::_detect_source`` keys on for ``interactions.source``
    (SDLC-ADR-004 / #429-b). :func:`recall_deploy_lessons` never sent either
    header, so a genuine fleet recall was indistinguishable from a human
    front-door read and ``audittrace_recall_total{source="fleet"}`` read
    persistent zero from real fleet traffic. This is the wiring that closes
    that gap.

    ``recall_deploy_lessons`` is the SHARED recall path for BOTH the local
    OpenCode/Qwen fleet AND the Sonnet cloud fleet (the audittrace-builder /
    audittrace-reviewer subagents call this exact helper too) — so the two
    headers carry deliberately DIFFERENT jobs, never a hardcoded runtime:

    * ``X-Agent-Role`` is the UNIVERSAL, runtime-agnostic fleet marker.
      Always sent, non-empty (``AUDITTRACE_AGENT_ROLE``, e.g. ``builder``/
      ``reviewer``/``deployer``/``verifier``; default ``"fleet"`` — never
      ``"backoffice"``, since a caller of this module is BY DEFINITION a
      fleet/agent recall, never a human front-door read). This is the
      RELIABLE classification signal — the server counts ``source="fleet"``
      for BOTH fleets off this header alone, with no per-runtime cardinality.
    * ``X-Source`` stays RUNTIME-HONEST for the audit trail (``interactions
      .source``-style granularity, not the metric label): ``"<runtime>-
      <role>"`` where runtime comes from ``AUDITTRACE_AGENT_RUNTIME`` (e.g.
      ``"opencode"`` for the local Qwen fleet, ``"sonnet"`` for the cloud
      fleet). When the runtime is unset, ``X-Source`` degrades to the bare
      role (or the plain ``"fleet"`` marker when neither env var is set) —
      NEVER a hardcoded ``"opencode-"`` prefix, which would mislabel
      Sonnet-fleet recalls as OpenCode.
    """
    role = (
        os.environ.get("AUDITTRACE_AGENT_ROLE") or ""
    ).strip() or _FLEET_DEFAULT_ROLE
    runtime = (os.environ.get("AUDITTRACE_AGENT_RUNTIME") or "").strip()
    x_source = f"{runtime}-{role}" if runtime else role
    return {"X-Source": x_source, "X-Agent-Role": role}


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def _multipart_body(
    filename: str, content: bytes, content_type: str = "text/markdown"
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body for the single ``file`` field.

    Returns ``(body_bytes, content_type_header)``. A random boundary avoids any
    chance of colliding with the record's own bytes.
    """
    boundary = f"----audittrace-{uuid.uuid4().hex}"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{_UPLOAD_FIELD}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    return preamble + content + epilogue, f"multipart/form-data; boundary={boundary}"


def _is_existing_file(candidate: str) -> bool:
    """True iff ``candidate`` names an existing file.

    A candidate too long (or otherwise un-stat-able) to probe — e.g. literal
    record text raising ``OSError``/ENAMETOOLONG when its first path segment
    exceeds 255 bytes — is NOT a file; return ``False`` so the caller treats
    it as text rather than crashing.
    """
    try:
        return Path(candidate).is_file()
    except OSError:
        return False


def _record_bytes_and_name(
    path_or_text: str | Path, filename: str | None
) -> tuple[bytes, str]:
    """Resolve ``path_or_text`` to ``(content_bytes, leaf_filename)``.

    A :class:`~pathlib.Path`, or a ``str`` naming an EXISTING file, is read from
    disk (leaf name preserved). Anything else is treated as literal record text
    and encoded; the caller may name it via ``filename`` or a stamped default is
    synthesised. ``.md`` is enforced on the leaf so the upload endpoint takes the
    synchronous non-PDF path (HTTP 200) rather than the PDF scan flow.
    """
    if isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str) and _is_existing_file(path_or_text)
    ):
        p = Path(path_or_text)
        return p.read_bytes(), _ensure_md(filename or p.name)
    content = str(path_or_text).encode()
    stamp = _now_iso().replace(":", "").replace("-", "")
    return content, _ensure_md(filename or f"deploy-record-{stamp}.md")


def _ensure_md(name: str) -> str:
    """Force a ``.md`` extension so the record indexes as a markdown decision."""
    return name if name.endswith(".md") else f"{name}.md"


def _tag_outcome(leaf: str, outcome: str | None) -> str:
    """Insert an ``.outcome-<value>`` infix before ``.md`` so recall can see it.

    Idempotent: a leaf that already carries an outcome infix is returned
    unchanged (never double-tags). ``outcome=None`` is a no-op (backward
    compatible with every existing caller). An unknown ``outcome`` raises
    :class:`ValueError` — the log path is RELIABLE, so a bad tag is loud, not
    silently dropped.
    """
    if outcome is None:
        return leaf
    if outcome not in BUILD_OUTCOMES:
        raise ValueError(f"outcome must be one of {BUILD_OUTCOMES}, got {outcome!r}")
    if _OUTCOME_INFIX in leaf:
        return leaf
    stem = leaf[:-3] if leaf.endswith(".md") else leaf
    return f"{stem}{_OUTCOME_INFIX}{outcome}.md"


def _is_recall_quarantined(item: dict[str, Any]) -> bool:
    """True if a recall item is tagged with a quarantined build outcome.

    Keys on the recall-visible identity fields only (``title`` / ``key`` /
    ``source`` / ``name``) — no content fetch. A legacy record with no outcome
    infix is never quarantined, so the filter is non-regressive.
    """
    hay = " ".join(str(item.get(k, "")) for k in ("title", "key", "source", "name"))
    return any(f"{_OUTCOME_INFIX}{o}" in hay for o in RECALL_QUARANTINED_OUTCOMES)


# ── public API ────────────────────────────────────────────────────────────────


def recall_deploy_lessons(
    query: str = "",
    *,
    front_door: str | None = None,
    token: str | None = None,
    insecure: bool = False,
    limit: int = DEFAULT_RECALL_LIMIT,
    timeout: int = 30,
    include_quarantined: bool = False,
) -> list[dict[str, Any]]:
    """BEST-EFFORT recall of deploy lessons from the decisions layer.

    Reads the decisions collection through the front door
    (``GET /memory/semantic?collection=decisions``) and returns the lessons it
    finds. This is a DELIBERATELY forgiving path: a recall miss must never block
    a deploy, and recall of newly-indexed decisions is known-degraded (#383). So
    on ANY failure — no token, HTTP error, transport failure, empty body, bad
    JSON — it logs a warning and returns ``[]``; it NEVER raises.

    ``query`` is accepted for the caller's intent + logging; ranking is the memory
    server's job (and is the degraded part), so this helper does not fabricate
    client-side relevance — it returns the decisions-layer items as the server
    ordered them (most-recent-first), leaving selection to the agent that reads
    them. Defaults to ``""`` (ADR-059 WU-1b, 2026-08-07): a live fleet caller
    invoked this keyword-only-after-``query`` — ``memory.recall_deploy_lessons(
    front_door=..., insecure=...)`` — and hit ``TypeError: missing 1 required
    positional argument: 'query'`` because the parameter had no default. Recall
    is best-effort and ``query`` is logging-only (never used to filter/rank), so
    a caller omitting it should degrade to "no intent recorded", not crash the
    whole recall-before step of the ADR-059 loop.
    """
    try:
        base = _normalize_front_door(resolve_front_door(front_door))
    except ValueError as exc:
        logger.warning("recall skipped — bad front-door URL: %s", exc)
        return []
    resolved = _resolve_token(token)
    if not resolved:
        logger.warning(
            "recall skipped — no scoped JWT (best-effort; deploy proceeds without lessons)"
        )
        return []
    params = urlencode(
        {"collection": DECISIONS_COLLECTION, "limit": limit, "sort": "created_at"}
    )
    url = f"{base}/memory/semantic?{params}"
    request_headers = {**_bearer(resolved), **_fleet_headers()}
    try:
        status, _headers, body = _http_request(
            "GET",
            url,
            request_headers,
            timeout=timeout,
            context=ssl_context(insecure),
        )
    except Exception as exc:  # noqa: BLE001 - recall must never propagate (best-effort contract)
        logger.warning(
            "recall failed (%s: %s); returning no lessons", type(exc).__name__, exc
        )
        return []
    if status != 200:
        logger.warning(
            "recall for %r returned HTTP %s; returning no lessons (best-effort)",
            query,
            status,
        )
        return []
    parsed = _parse_json(body)
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list) or not items:
        logger.warning(
            "recall for %r found no decisions-layer lessons (#383 degradation is expected)",
            query,
        )
        return []
    dicts = [i for i in items if isinstance(i, dict)]
    if not include_quarantined:
        kept = [i for i in dicts if not _is_recall_quarantined(i)]
        dropped = len(dicts) - len(kept)
        if dropped:
            logger.info(
                "recall for %r quarantined %d stalled/failed build-record(s) "
                "(study-objects, not precedent; pass include_quarantined=True to include them)",
                query,
                dropped,
            )
        dicts = kept
    logger.info(
        "recall for %r returned %d decisions-layer lesson(s)", query, len(dicts)
    )
    return dicts


def log_deploy_record(
    path_or_text: str | Path,
    *,
    front_door: str | None = None,
    token: str | None = None,
    layer: str = "episodic",
    collections: tuple[str, ...] = (DECISIONS_COLLECTION,),
    filename: str | None = None,
    outcome: str | None = None,
    insecure: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """RELIABLE log of a deploy/verify record to the memory server (ADR-059).

    Uploads the record to ``POST /memory/upload?layer=<layer>`` (multipart, the
    single ``file`` field) and then indexes it via
    ``POST /memory/index?file=<layer>/<name>&collections=...``. Returns
    ``{"status", "key", "index_status"}``.

    ``outcome`` (one of :data:`BUILD_OUTCOMES`) tags the record filename with an
    ``.outcome-<value>`` infix so recall can quarantine drift trajectories
    (``stalled`` / ``failed``) from being recalled as precedent — see
    :func:`recall_deploy_lessons`. A bad ``outcome`` raises :class:`ValueError`
    (the log path is reliable, so a mistag is loud, not silent). ``None`` keeps
    the historical, untagged filename (backward compatible).

    Logging is the ADR-059 contract, so — unlike recall — a failure here is LOUD:
    a missing token, a non-200 upload, or a non-200 index all raise
    :class:`DeployLogError` with the failing step and HTTP status. The LOG side of
    the memory server is reliable even while recall (#383) is degraded.
    """
    base = _normalize_front_door(resolve_front_door(front_door))
    resolved = _resolve_token(token)
    if not resolved:
        raise DeployLogError(
            "auth", 0, "no scoped JWT available to log the deploy record"
        )
    content, leaf = _record_bytes_and_name(path_or_text, filename)
    leaf = _tag_outcome(leaf, outcome)

    # -- upload (multipart, non-PDF synchronous path → HTTP 200) --
    up_params = urlencode({"layer": layer, "filename": leaf})
    up_url = f"{base}/memory/upload?{up_params}"
    mp_body, mp_ct = _multipart_body(leaf, content)
    up_headers = {**_bearer(resolved), "Content-Type": mp_ct}
    up_status, _uh, up_resp = _http_request(
        "POST",
        up_url,
        up_headers,
        mp_body,
        timeout=timeout,
        context=ssl_context(insecure),
    )
    if up_status != 200:
        raise DeployLogError("upload", up_status, _err_detail(up_resp))
    up_json = _parse_json(up_resp)
    key = f"{layer}/{leaf}"
    if isinstance(up_json, dict):
        key = up_json.get("key") or up_json.get("object") or key

    # -- index the just-uploaded object into the decisions collection --
    ix_params = urlencode({"file": key, "collections": ",".join(collections)})
    ix_url = f"{base}/memory/index?{ix_params}"
    ix_status, _ih, ix_resp = _http_request(
        "POST",
        ix_url,
        _bearer(resolved),
        timeout=timeout,
        context=ssl_context(insecure),
    )
    if ix_status != 200:
        raise DeployLogError("index", ix_status, _err_detail(ix_resp))
    ix_json = _parse_json(ix_resp)

    logger.info(
        "logged deploy record %s and indexed into %s", key, ",".join(collections)
    )
    return {
        "status": "logged",
        "key": key,
        "index_status": ix_json if ix_json is not None else {"http_status": ix_status},
    }


def log_build_record(
    path_or_text: str | Path,
    *,
    front_door: str | None = None,
    token: str | None = None,
    layer: str = "episodic",
    collections: tuple[str, ...] = (DECISIONS_COLLECTION,),
    filename: str | None = None,
    outcome: str | None = None,
    insecure: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """Layer-0-VALIDATING variant of :func:`log_deploy_record` (SDLC-ADR-005 WU-1).

    ADDITIVE, opt-in entry point for builder/reviewer records that MUST carry
    the ADR-059 structured front-matter (``spec_ref``, ``spec_hash``,
    ``recall_evidence``, ``log_key``, ``index_status``, ``branch``, ``commit``,
    ``gates`` — see :mod:`scripts.deploy.build_record`). Validates the record
    BEFORE any network call — a malformed record never reaches the memory
    server, no upload/index HTTP round-trip is wasted on it — then delegates
    to :func:`log_deploy_record` unchanged for the actual upload + index.

    ``log_deploy_record`` itself is left completely untouched: the deploy /
    release / curator agents' records do NOT carry this schema, so Layer-0
    validation is scoped to this dedicated opt-in function rather than gating
    the shared helper every existing caller depends on — the least-breaking,
    most fail-closed choice (see the WU-1 build-record's deviation notes).

    Raises :class:`~scripts.deploy.build_record.BuildRecordValidationError` on
    a missing/empty required field (fail-closed, no network call is made);
    otherwise raises :class:`DeployLogError` exactly as :func:`log_deploy_record`
    does on an upload/index failure.
    """
    validate_build_record(path_or_text)
    return log_deploy_record(
        path_or_text,
        front_door=front_door,
        token=token,
        layer=layer,
        collections=collections,
        filename=filename,
        outcome=outcome,
        insecure=insecure,
        timeout=timeout,
    )


def _err_detail(body: bytes) -> str:
    """Best-effort human detail from an error response body (never leaks a token)."""
    parsed = _parse_json(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    text = body.decode("utf-8", "replace").strip()
    return text[:200] if text else "no response body"
