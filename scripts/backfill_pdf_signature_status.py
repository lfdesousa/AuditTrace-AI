#!/usr/bin/env python3
"""Caller-owned backfill: re-classify PDFs recorded as ``check_failed`` (#460 / GH #366).

**What it fixes.** Until #366 was fixed every signed PDF indexed through the
async pipeline was recorded ``signature_status = 'check_failed'``. After the
fix, re-indexing a document re-classifies it. This script finds the CALLER's
OWN affected rows and re-indexes them through the public API.

**Caller-owned only (operator decision, 2026-10-02).**

* A row is processed only if its owner (``created_by_user_id``) is the
  caller's own ``sub``. Rows owned by other users are COUNTED and LISTED
  (key, owner, created/modified ms) and NEVER touched.
* **Owner read by identity, not by enumeration (BACKFILL-LIVE-MAPPING).** The
  chunk id is deterministic (``sha256("ai_research_papers:<source_key>:p<page>:0")
  [:16]``, the pipeline's own rule), so the script reads THAT document's chunk
  with ``GET /memory/semantic/ai_research_papers/<id>`` and never walks the
  chunk listing (which is capped server-side at 200 discovered rows and keyed by
  title: it cannot see the rows it must guard). The pages ``1..page_count`` are
  probed until the first 200; a row without ``page_count`` is not probed.
* Skip reasons, fail closed, nothing is written for a skipped row:
  ``manifest_modifier_not_caller`` (manifest ``modified_by_user_id`` is not the
  caller: another user indexed it last), ``parse_timeout_partial_index`` (the
  last index run aborted: later pages may still carry another user's stamp),
  ``chunk_not_found``, ``chunk_read_failed`` (non-200/404), ``chunk_owner_missing``,
  ``chunk_source_key_mismatch``, ``chunk_owner_not_caller``.
* **The ``chunk_owner_not_caller`` pin is LOAD-BEARING under an admin token.**
  Corpus writes need ``audittrace:admin`` (no narrower scope exists for the
  episodic/procedural layers), and an admin token is admitted to EVERY chunk by
  the server's read predicate, so ``user_id == caller`` is then the ONLY owner
  guard. Do not weaken it.
* **404 is ambiguous for a non-admin token:** "never chunked" and "chunk owned by
  another user" both answer 404. The script treats both as ``chunk_not_found``
  (skip); the 7 measured 404s are attributed to never-chunked rows only because
  their ``page_count`` is null and they carry ``pdf_corrupted_structure``.
* Write only what the dry run predicts to change: a row whose dry run answers
  ``check_failed`` again is ``skipped:status_unchanged``.
* After a real re-index the write's response, the SAME chunk (``user_id``,
  ``source_key``, ``document_hash`` unchanged, ``signature_status``) and the
  re-listed manifest row (``signature_status``, ``created_by_user_id`` ==
  caller) are compared against the DRY-RUN prediction, never against the
  write's own response alone. Any miss STOPS the run (exit 3).

**Dry run first.** Without ``--apply`` the script only issues
``POST /memory/index?file=<key>&dry_run=true&details=true`` (no write) and
prints the before/after table. ``--apply`` runs the dry run for every row
first and re-indexes only rows whose dry run predicts a change, one at a time,
sleeping between rows (APU cap). Authority is decided by the server: a corpus
re-index needs an admin token; a 403 detail is recorded in the ``error`` column.

**Credentials.** The scoped JWT is resolved in-process (explicit argument,
``AUDITTRACE_TOKEN``, or the operator's login token file via
``scripts.deploy.memory``), sent only as a Bearer header, and never printed or
written. The caller's ``sub`` is read from the token's own payload.

**Portability.** The front door comes from ``--front-door`` or
``AUDITTRACE_FRONT_DOOR`` (``scripts.deploy.frontdoor``); nothing target-shaped
is hardcoded here.

Operator tooling, not a served API: it lives in ``scripts/``, imports nothing
from ``src/audittrace``, and IS under the per-file coverage gate (it is listed
in the coverage ``source`` and in ``make test``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from scripts.deploy import memory as _mem
from scripts.deploy.frontdoor import resolve_front_door

logger = logging.getLogger("audittrace.scripts.backfill_pdf_signature_status")

AFFECTED_STATUS = "check_failed"
LAYERS = ("episodic", "procedural")
PAGE_SIZE = 500
CHUNK_COLLECTION = "ai_research_papers"
DEFAULT_SLEEP_SECONDS = 2.0
DETAIL_MAX_CHARS = 500

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_OWNER_MISMATCH = 3
EXIT_FAILURE = 4


class BackfillError(RuntimeError):
    """A condition that must stop the run (auth, transport, bad response)."""


class OwnerMismatchError(BackfillError):
    """``chunk_owner_before != chunk_owner_after`` for a re-indexed row."""


@dataclass(frozen=True)
class OtherOwnerRow:
    """A ``check_failed`` row owned by someone else: listed, never touched."""

    layer: str
    key: str
    owner: str | None
    created_at_ms: int | None
    modified_at_ms: int | None


@dataclass
class TableRow:
    """One before/after line for a caller-owned row."""

    layer: str
    key: str
    old_status: str
    new_status: str | None = None
    trace_id: str | None = None
    doc_id: str | None = None
    chunk_owner_before: str | None = None
    chunk_owner_after: str | None = None
    manifest_status_after: str | None = None
    outcome: str = "pending"  # dry_run_ok | applied | skipped:<why> | failed:<why>
    error: str | None = None  # server ``detail`` of a non-200, if any


@dataclass
class BackfillReport:
    caller_sub: str
    applied: bool
    rows: list[TableRow] = field(default_factory=list)
    other_owner_rows: list[OtherOwnerRow] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "caller_sub": self.caller_sub,
                "applied": self.applied,
                "rows": [asdict(r) for r in self.rows],
                "other_owner_rows": [asdict(r) for r in self.other_owner_rows],
            },
            indent=2,
        )


# ───────────────────────────── identity + HTTP ─────────────────────────────


def caller_sub_from_token(token: str) -> str:
    """Read ``sub`` from the token's own payload (no verification: the server
    validates the token; this only learns who the caller claims to be)."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError) as exc:
        raise BackfillError("token is not a decodable JWT") from exc
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(sub, str) or not sub:
        raise BackfillError("token carries no sub claim")
    return sub


class Client:
    """Thin authenticated client over the front door."""

    def __init__(self, base: str, token: str, insecure: bool, timeout: int) -> None:
        self._base = base
        self._headers = _mem._bearer(token)
        self._ctx = _mem.ssl_context(insecure)
        self._timeout = timeout

    def request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], Any]:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urlencode(params)
        status, headers, body = _mem._http_request(
            method, url, self._headers, timeout=self._timeout, context=self._ctx
        )
        return status, headers, _mem._parse_json(body)


# ─────────────────────────────── discovery ───────────────────────────────


def list_layer(client: Client, layer: str) -> list[dict[str, Any]]:
    """Every non-deleted manifest row the caller can see in *layer*, through
    the public list read (all dates: never selected by ``created_at`` alone)."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        status, _h, body = client.request(
            "GET",
            f"/memory/{layer}",
            {"limit": PAGE_SIZE, "offset": offset, "sort": "created_at"},
        )
        if status != 200 or not isinstance(body, dict):
            raise BackfillError(f"GET /memory/{layer} failed (HTTP {status})")
        page = [i for i in body.get("items", []) if isinstance(i, dict)]
        rows += [i for i in page if i.get("deleted_at_ms") is None]
        offset += len(page)
        total = body.get("total")
        if not page or not isinstance(total, int) or offset >= total:
            return rows


def list_check_failed(client: Client, layer: str) -> list[dict[str, Any]]:
    """The ``check_failed`` subset of :func:`list_layer`."""
    return [
        r
        for r in list_layer(client, layer)
        if r.get("signature_status") == AFFECTED_STATUS
    ]


def index_file_param(row: dict[str, Any], caller_sub: str, layer: str) -> str:
    """The ``?file=`` value for a manifest row: the stored key when it already
    carries a ``<sub>/`` or ``<layer>/`` prefix, else the tier-appropriate
    shape (private: ``<sub>/<layer>/<key>``; corpus: ``<layer>/<key>``)."""
    key = str(row["key"])
    if key.startswith((f"{caller_sub}/", f"{layer}/")):
        return key
    if row.get("tier") == "private":
        return f"{caller_sub}/{layer}/{key}"
    return f"{layer}/{key}"


# ─────────────────────── chunk owner: read by identity ───────────────────────


@dataclass(frozen=True)
class ChunkOwner:
    """The metadata one chunk reports about itself (``GET .../<doc_id>``)."""

    doc_id: str
    page: int
    user_id: str | None
    source_key: str | None
    document_hash: str | None
    signature_status: str | None


@dataclass(frozen=True)
class ChunkRead:
    """Result of the identity read: the chunk, or why there is none."""

    owner: ChunkOwner | None
    reason: str | None = None


def derive_source_key(layer: str, key: str) -> str:
    """The pipeline's own rule: the key minus its ``<layer>/`` prefix."""
    prefix = f"{layer}/"
    return key[len(prefix) :] if key.startswith(prefix) else key


def chunk_doc_id(source_key: str, page: int) -> str:
    """Deterministic id of chunk 0 of *page* (pages are 1-based)."""
    raw = f"{CHUNK_COLLECTION}:{source_key}:p{page}:0"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _get_chunk(client: Client, doc_id: str, page: int) -> tuple[int, ChunkOwner | None]:
    """``GET /memory/semantic/<collection>/<doc_id>``: ``(status, chunk)``;
    the chunk is ``None`` unless the answer was a 200 with a metadata object."""
    status, _h, body = client.request(
        "GET", f"/memory/semantic/{CHUNK_COLLECTION}/{doc_id}"
    )
    if status != 200:
        return status, None
    meta = body.get("metadata") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return status, ChunkOwner(doc_id, page, None, None, None, None)
    return status, ChunkOwner(
        doc_id=doc_id,
        page=page,
        user_id=_str_or_none(meta.get("user_id")),
        source_key=_str_or_none(meta.get("source_key")),
        document_hash=_str_or_none(meta.get("document_hash")),
        signature_status=_str_or_none(meta.get("signature_status")),
    )


def read_chunk_owner(
    client: Client, layer: str, key: str, page_count: Any
) -> ChunkRead:
    """Find the document's first chunk by identity: probe pages
    ``1..page_count`` until the first 200. No listing call is ever made."""
    source_key = derive_source_key(layer, key)
    if not isinstance(page_count, int) or isinstance(page_count, bool):
        return ChunkRead(None, "chunk_not_found")
    for page in range(1, page_count + 1):
        status, chunk = _get_chunk(client, chunk_doc_id(source_key, page), page)
        if chunk is not None:
            return ChunkRead(chunk)
        if status != 404:
            return ChunkRead(None, "chunk_read_failed")
    return ChunkRead(None, "chunk_not_found")


def chunk_skip_reason(
    chunk: ChunkOwner, source_key: str, caller_sub: str
) -> str | None:
    """Why the row must NOT be re-indexed, or ``None`` if it may be.

    The ``user_id == caller_sub`` comparison is the only owner guard under an
    admin token (the server admits an admin to every chunk): load-bearing."""
    if chunk.user_id is None:
        return "chunk_owner_missing"
    if chunk.source_key != source_key:
        return "chunk_source_key_mismatch"
    if chunk.user_id != caller_sub:
        return "chunk_owner_not_caller"
    return None


def manifest_skip_reason(row: dict[str, Any], caller_sub: str) -> str | None:
    """Pins read from the manifest row itself (no network): the last indexer
    must be the caller, and the last index run must not have been aborted."""
    if row.get("modified_by_user_id") != caller_sub:
        return "manifest_modifier_not_caller"
    warnings = row.get("extraction_warnings")
    if isinstance(warnings, list) and any(
        isinstance(w, dict) and w.get("code") == "parse_timeout" for w in warnings
    ):
        return "parse_timeout_partial_index"
    return None


# ───────────────────────────────── the run ─────────────────────────────────


def _detail_text(body: Any) -> str | None:
    """The server's ``detail`` (a string, or JSON text), bounded."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is None:
        return None
    text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True)
    return text[:DETAIL_MAX_CHARS]


def _post_index(
    client: Client, file_param: str, *, dry_run: bool
) -> tuple[int, str | None, str | None, str | None]:
    """``POST /memory/index?file=...&details=true[&dry_run=true]``; returns
    ``(http_status, signature_status, trace_id, error_detail)``."""
    params: dict[str, Any] = {"file": file_param, "details": "true"}
    if dry_run:
        params["dry_run"] = "true"
    status, headers, body = client.request("POST", "/memory/index", params)
    new_status: str | None = None
    error: str | None = None
    if status == 200 and isinstance(body, dict):
        docs = body.get("documents")
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            value = docs[0].get("signature_status")
            new_status = value if isinstance(value, str) else None
    elif status != 200:
        error = _detail_text(body)
    trace_id = headers.get("x-trace-id") or headers.get("traceparent")
    return status, new_status, trace_id, error


def _mismatch(row: TableRow, what: str, before: Any, after: Any) -> OwnerMismatchError:
    row.outcome = "failed:post_write_mismatch"
    return OwnerMismatchError(
        f"post-write mismatch for {row.key}: {what} {before!r} -> {after!r}"
    )


def verify_post_write(
    client: Client,
    layer: str,
    row: TableRow,
    before: ChunkOwner,
    caller_sub: str,
    expected: str,
) -> None:
    """Second line: re-read the SAME chunk and re-list the layer; any miss
    raises :class:`OwnerMismatchError` (the run stops). *expected* is the DRY-RUN
    prediction, never the write's own response."""
    _s, after = _get_chunk(client, before.doc_id, before.page)
    if after is None:
        raise _mismatch(row, "chunk", before.doc_id, None)
    row.chunk_owner_after = after.user_id
    if after.user_id != caller_sub:
        raise _mismatch(row, "chunk user_id", before.user_id, after.user_id)
    if after.source_key != before.source_key:
        raise _mismatch(row, "chunk source_key", before.source_key, after.source_key)
    if after.document_hash != before.document_hash:
        raise _mismatch(
            row, "chunk document_hash", before.document_hash, after.document_hash
        )
    if after.signature_status != expected:
        raise _mismatch(row, "chunk signature_status", expected, after.signature_status)
    listed = next(
        (r for r in list_layer(client, layer) if r.get("key") == row.key), None
    )
    if listed is None:
        raise _mismatch(row, "manifest row", row.key, None)
    row.manifest_status_after = _str_or_none(listed.get("signature_status"))
    if listed.get("signature_status") != expected:
        raise _mismatch(
            row, "manifest signature_status", expected, listed.get("signature_status")
        )
    if listed.get("created_by_user_id") != caller_sub:
        raise _mismatch(
            row,
            "manifest created_by_user_id",
            caller_sub,
            listed.get("created_by_user_id"),
        )


def run_backfill(
    client: Client,
    caller_sub: str,
    *,
    layers: tuple[str, ...] = LAYERS,
    apply: bool = False,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    scanner: Callable[[Client, str, str, Any], ChunkRead] | None = None,
) -> BackfillReport:
    """Select, dry-run and (with ``apply``) re-classify the caller's own rows."""
    read_owner = scanner or read_chunk_owner
    report = BackfillReport(caller_sub=caller_sub, applied=apply)
    candidates: list[tuple[TableRow, dict[str, Any]]] = []
    for layer in layers:
        for row in list_check_failed(client, layer):
            owner = row.get("created_by_user_id")
            if owner != caller_sub:
                report.other_owner_rows.append(
                    OtherOwnerRow(
                        layer=layer,
                        key=str(row.get("key")),
                        owner=owner if isinstance(owner, str) else None,
                        created_at_ms=row.get("created_at_ms"),
                        modified_at_ms=row.get("modified_at_ms"),
                    )
                )
                continue
            candidates.append(
                (
                    TableRow(
                        layer=layer, key=str(row["key"]), old_status=AFFECTED_STATUS
                    ),
                    row,
                )
            )

    # Phase 1: owner read (fail closed), then dry run, then the change rule.
    runnable: list[tuple[TableRow, str, ChunkOwner]] = []
    for table_row, row in candidates:
        report.rows.append(table_row)
        skip = manifest_skip_reason(row, caller_sub)
        if skip is not None:
            table_row.outcome = f"skipped:{skip}"
            continue
        source_key = derive_source_key(table_row.layer, table_row.key)
        read = read_owner(client, table_row.layer, table_row.key, row.get("page_count"))
        chunk = read.owner
        if chunk is None:
            table_row.outcome = f"skipped:{read.reason or 'chunk_not_found'}"
            continue
        table_row.doc_id = chunk.doc_id
        table_row.chunk_owner_before = chunk.user_id
        skip = chunk_skip_reason(chunk, source_key, caller_sub)
        if skip is not None:
            table_row.outcome = f"skipped:{skip}"
            continue
        file_param = index_file_param(row, caller_sub, table_row.layer)
        status, new_status, trace_id, error = _post_index(
            client, file_param, dry_run=True
        )
        table_row.new_status, table_row.trace_id = new_status, trace_id
        if status != 200:
            table_row.outcome = f"failed:dry_run_http_{status}"
            table_row.error = error
        elif new_status is None:
            table_row.outcome = "skipped:dry_run_status_missing"
        elif new_status == AFFECTED_STATUS:
            table_row.outcome = "skipped:status_unchanged"
        else:
            table_row.outcome = "dry_run_ok"
            runnable.append((table_row, file_param, chunk))

    if not apply:
        return report

    # Phase 2: real re-index, one row at a time, post-write checks enforced.
    for table_row, file_param, chunk in runnable:
        sleep(sleep_seconds)
        status, new_status, trace_id, error = _post_index(
            client, file_param, dry_run=False
        )
        table_row.trace_id = trace_id or table_row.trace_id
        if status != 200:
            table_row.outcome = f"failed:index_http_{status}"
            table_row.error = error
            continue
        predicted = table_row.new_status  # the dry-run prediction (a str here)
        table_row.new_status = new_status
        if predicted is None or new_status != predicted:
            raise _mismatch(
                table_row, "index response signature_status", predicted, new_status
            )
        verify_post_write(
            client, table_row.layer, table_row, chunk, caller_sub, predicted
        )
        table_row.outcome = "applied"
    return report


# ──────────────────────────────────── CLI ────────────────────────────────────


def format_report(report: BackfillReport) -> str:
    lines = [
        f"mode: {'APPLY' if report.applied else 'DRY RUN'}",
        f"caller-owned check_failed rows: {len(report.rows)}",
        f"other-owner check_failed rows (listed, NEVER touched): "
        f"{len(report.other_owner_rows)}",
        "",
        "layer | key | old_status | new_status | trace_id | doc_id | "
        "chunk_owner_before | chunk_owner_after | manifest_status_after | "
        "outcome | error",
    ]
    for r in report.rows:
        lines.append(
            f"{r.layer} | {r.key} | {r.old_status} | {r.new_status} | {r.trace_id} | "
            f"{r.doc_id} | {r.chunk_owner_before} | {r.chunk_owner_after} | "
            f"{r.manifest_status_after} | {r.outcome} | {r.error}"
        )
    if report.other_owner_rows:
        lines += [
            "",
            "other-owner rows: layer/key | owner | created_at_ms | modified_at_ms",
        ]
        for o in report.other_owner_rows:
            lines.append(
                f"{o.layer}/{quote(o.key, safe='/')} | {o.owner} | "
                f"{o.created_at_ms} | {o.modified_at_ms}"
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-classify the caller's own check_failed PDFs (#460)."
    )
    parser.add_argument("--front-door", default=None, help="base URL (else env)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verify")
    parser.add_argument(
        "--layers", default=",".join(LAYERS), help="comma-separated layers"
    )
    parser.add_argument("--apply", action="store_true", help="really re-index")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--json-out", default=None, help="write the report JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layers = tuple(x for x in args.layers.split(",") if x)
    if not layers or any(x not in LAYERS for x in layers):
        print(f"--layers must be a subset of {LAYERS}", file=sys.stderr)
        return EXIT_USAGE
    try:
        base = _mem._normalize_front_door(resolve_front_door(args.front_door))
    except ValueError as exc:
        print(f"bad front door: {exc}", file=sys.stderr)
        return EXIT_USAGE
    token = _mem._resolve_token(None)
    if not token:
        print("no scoped JWT available (login first)", file=sys.stderr)
        return EXIT_USAGE
    try:
        sub = caller_sub_from_token(token)
        client = Client(base, token, args.insecure, args.timeout)
        report = run_backfill(
            client,
            sub,
            layers=layers,
            apply=args.apply,
            sleep_seconds=args.sleep,
        )
    except OwnerMismatchError as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return EXIT_OWNER_MISMATCH
    except BackfillError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print(format_report(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(report.to_json())
    failed = any(r.outcome.startswith("failed") for r in report.rows)
    return EXIT_FAILURE if failed else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
