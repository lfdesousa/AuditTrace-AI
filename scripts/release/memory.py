"""ADR-059 memory-wiring helper for the Release Agent (#398 wrapped as a
dispatchable subagent, #424).

The small, tested seam the Release Agent uses to (a) RECALL release lessons
from the internal memory server before cutting a release and (b) LOG the
release record back after — the *recall-before / log-after* discipline of
ADR-059 (agents on the memory server that improve on their own history).

This is a self-contained SIBLING of :mod:`scripts.deploy.memory` (the deploy
and release runners are deliberately parallel siblings —
``deploy.runner`` ‖ ``release.runner`` — so their memory helpers mirror too;
see SPEC-424). Duplicating the generic egress plumbing is the established
house pattern here; :mod:`scripts.deploy.memory` is NOT refactored to share
code with this module.

Two public functions with DELIBERATELY ASYMMETRIC failure contracts:

* :func:`recall_release_lessons` — **best-effort**. A recall miss must NEVER
  block a release cut. On ANY failure (HTTP error, transport failure, empty
  result, bad JSON) it logs a warning and returns ``[]``; it never raises.
  Recall of newly-indexed decisions is a KNOWN-DEGRADED path (#383): the
  LLM-driven ``recall_*`` tool loop may return nothing/irrelevant, so this
  helper reads the decisions layer directly through the front door and
  treats an empty answer as normal, not as an error.

* :func:`log_release_record` — **reliable**. Logging IS the ADR-059
  contract, so a failure here is loud: it raises :class:`ReleaseLogError`
  with a clear message. The LOG side of the memory server works reliably
  even while recall is degraded.

**Method discipline (shared with the runners):**

* Single monkeypatchable network-egress seam, :func:`_http_request` (mirrors
  :func:`scripts.deploy.memory._http_request`). Tests substitute it; no
  sockets.
* The scoped JWT is loaded in-memory from ``~/.config/audittrace/tokens.json``
  (or the ``AUDITTRACE_TOKEN`` env) when not passed, used only as a Bearer
  header, and NEVER printed or written anywhere.
* Every front-door URL is validated with :func:`urllib.parse.urlparse` EXACT
  scheme/host checks — never a substring membership test on the raw URL
  string (the CodeQL ``py/incomplete-url-substring`` class).
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

logger = logging.getLogger(__name__)

# Laptop default (the reference/dev environment); the Release Agent overrides
# this via env for other targets (portability invariant — never hardcode a
# target in core logic).
DEFAULT_FRONT_DOOR = "https://audittrace.local"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "audittrace" / "tokens.json"

# The decisions collection is where release lessons are indexed (the
# ``collections`` default of :func:`log_release_record`), so recall reads it
# back.
DECISIONS_COLLECTION = "decisions"
DEFAULT_RECALL_LIMIT = 5

# Multipart field name MUST be ``file`` — the upload endpoint binds
# ``file: UploadFile = File(...)``.
_UPLOAD_FIELD = "file"


class ReleaseLogError(RuntimeError):
    """Raised when logging a release record FAILS.

    Logging is the ADR-059 contract, so — unlike a recall miss — a log
    failure is not swallowed. Carries the failing step ("auth" | "upload" |
    "index") and the HTTP status so the caller can reconstruct what went
    wrong. A release that cannot be recorded is not done.
    """

    def __init__(self, step: str, status: int, detail: str) -> None:
        super().__init__(f"release-record {step} failed (HTTP {status}): {detail}")
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

    The SOLE network-egress point (front door). Monkeypatched in tests so no
    real socket is opened. A transport failure returns status ``0`` so
    callers read it as a hard failure rather than an exception. ``context``
    (when set) is the unverified SSL context selected by ``insecure=True``.

    Real ``urlopen`` RAISES the whole transport-error class here, not just
    ``URLError``: a READ timeout surfaces as a bare ``TimeoutError`` (==
    ``socket.timeout``, an ``OSError``) that is NOT a ``URLError``. Catching
    ``OSError`` after ``HTTPError`` collapses the ENTIRE class — ``URLError``,
    ``TimeoutError``/``socket.timeout``, ``ConnectionError`` — to the
    status-``0`` sentinel in ONE place, so every caller maps a timeout to its
    own clean contract: ``recall_release_lessons`` → ``[]``, and
    ``log_release_record`` → a clean ``ReleaseLogError`` via its non-200
    guard.
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

    ``insecure=True`` is an EXPLICIT dev/self-signed opt-in (the laptop front
    door presents a self-signed cert), equivalent to ``curl -k``. Nothing is
    weakened by default.
    """
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _normalize_front_door(url: str) -> str:
    """Validate + normalise the front-door base URL with an EXACT scheme/host check.

    Uses :func:`urllib.parse.urlparse` — NOT substring matching — so a
    hostile URL such as ``https://evil/?x=audittrace`` cannot slip past a
    naive substring membership test on the URL (the CodeQL
    ``py/incomplete-url-substring`` class). Rejects anything that is not an
    http/https URL carrying a hostname.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"front-door must be an http(s) URL with a host: {url!r}")
    return url.rstrip("/")


def _resolve_token(token: str | None, token_file: Path | None = None) -> str | None:
    """Resolve a scoped JWT: an explicit arg first, then ``AUDITTRACE_TOKEN``
    env, then the token file's ``access_token``.

    ``token_file`` defaults to the module-level :data:`DEFAULT_TOKEN_FILE`
    looked up at CALL time (not bound as a default arg) so a test can
    monkeypatch it. The token is used only as a Bearer header and is NEVER
    logged or printed. Returns ``None`` when no token is available.
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


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def _multipart_body(
    filename: str, content: bytes, content_type: str = "text/markdown"
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body for the single ``file`` field.

    Returns ``(body_bytes, content_type_header)``. A random boundary avoids
    any chance of colliding with the record's own bytes.
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


def _record_bytes_and_name(
    path_or_text: str | Path, filename: str | None
) -> tuple[bytes, str]:
    """Resolve ``path_or_text`` to ``(content_bytes, leaf_filename)``.

    A :class:`~pathlib.Path`, or a ``str`` naming an EXISTING file, is read
    from disk (leaf name preserved). Anything else is treated as literal
    record text and encoded; the caller may name it via ``filename`` or a
    stamped default is synthesised. ``.md`` is enforced on the leaf so the
    upload endpoint takes the synchronous non-PDF path (HTTP 200) rather than
    the PDF scan flow.
    """
    if isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str) and Path(path_or_text).is_file()
    ):
        p = Path(path_or_text)
        return p.read_bytes(), _ensure_md(filename or p.name)
    content = str(path_or_text).encode()
    stamp = _now_iso().replace(":", "").replace("-", "")
    return content, _ensure_md(filename or f"release-record-{stamp}.md")


def _ensure_md(name: str) -> str:
    """Force a ``.md`` extension so the record indexes as a markdown decision."""
    return name if name.endswith(".md") else f"{name}.md"


def _err_detail(body: bytes) -> str:
    """Best-effort human detail from an error response body (never leaks a token)."""
    parsed = _parse_json(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    text = body.decode("utf-8", "replace").strip()
    return text[:200] if text else "no response body"


# ── public API ────────────────────────────────────────────────────────────────


def recall_release_lessons(
    query: str,
    *,
    front_door: str = DEFAULT_FRONT_DOOR,
    token: str | None = None,
    token_file: Path | None = None,
    insecure: bool = False,
    limit: int = DEFAULT_RECALL_LIMIT,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """BEST-EFFORT recall of release lessons from the decisions layer.

    Reads the decisions collection through the front door
    (``GET /memory/semantic?collection=decisions``) and returns the lessons
    it finds. This is a DELIBERATELY forgiving path: a recall miss must never
    block a release cut, and recall of newly-indexed decisions is
    known-degraded (#383). So on ANY failure — no token, bad front-door URL,
    HTTP error, transport failure, empty body, bad JSON — it logs a warning
    and returns ``[]``; it NEVER raises.

    ``query`` is accepted for the caller's intent + logging; ranking is the
    memory server's job (and is the degraded part), so this helper does not
    fabricate client-side relevance — it returns the decisions-layer items as
    the server ordered them (most-recent-first), leaving selection to the
    agent that reads them.
    """
    try:
        base = _normalize_front_door(front_door)
    except ValueError as exc:
        logger.warning("recall skipped — bad front-door URL: %s", exc)
        return []
    resolved = _resolve_token(token, token_file)
    if not resolved:
        logger.warning(
            "recall skipped — no scoped JWT (best-effort; release proceeds without lessons)"
        )
        return []
    params = urlencode(
        {"collection": DECISIONS_COLLECTION, "limit": limit, "sort": "created_at"}
    )
    url = f"{base}/memory/semantic?{params}"
    try:
        status, _headers, body = _http_request(
            "GET",
            url,
            _bearer(resolved),
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
    logger.info(
        "recall for %r returned %d decisions-layer lesson(s)", query, len(items)
    )
    return [i for i in items if isinstance(i, dict)]


def log_release_record(
    record_path: str | Path,
    *,
    front_door: str = DEFAULT_FRONT_DOOR,
    token: str | None = None,
    token_file: Path | None = None,
    layer: str = "episodic",
    collections: tuple[str, ...] = (DECISIONS_COLLECTION,),
    filename: str | None = None,
    insecure: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """RELIABLE log of a release record to the memory server (ADR-059).

    Uploads the record to ``POST /memory/upload?layer=<layer>`` (multipart,
    the single ``file`` field) and then indexes it via
    ``POST /memory/index?file=<layer>/<name>&collections=...``. Returns
    ``{"status", "key", "index_status"}``.

    Logging is the ADR-059 contract, so — unlike recall — a failure here is
    LOUD: a bad front-door URL, a missing token, a non-200 upload, or a
    non-200 index all raise :class:`ReleaseLogError` with the failing step
    and HTTP status. The LOG side of the memory server is reliable even
    while recall (#383) is degraded.
    """
    base = _normalize_front_door(front_door)
    resolved = _resolve_token(token, token_file)
    if not resolved:
        raise ReleaseLogError(
            "auth", 0, "no scoped JWT available to log the release record"
        )
    content, leaf = _record_bytes_and_name(record_path, filename)

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
        raise ReleaseLogError("upload", up_status, _err_detail(up_resp))
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
        raise ReleaseLogError("index", ix_status, _err_detail(ix_resp))
    ix_json = _parse_json(ix_resp)

    logger.info(
        "logged release record %s and indexed into %s", key, ",".join(collections)
    )
    return {
        "status": "logged",
        "key": key,
        "index_status": ix_json if ix_json is not None else {"http_status": ix_status},
    }
