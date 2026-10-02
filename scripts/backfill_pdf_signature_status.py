#!/usr/bin/env python3
"""Caller-owned backfill: re-classify PDFs recorded as ``check_failed`` (#460 / GH #366).

**What it fixes.** Until #366 was fixed every signed PDF indexed through the
async pipeline was recorded ``signature_status = 'check_failed'``. After the
fix, re-indexing a document re-classifies it. This script finds the CALLER's
OWN affected rows and re-indexes them through the public API.

**Caller-owned only (operator decision, 2026-10-02).**

* A row is processed only if its owner (``created_by_user_id``) is the
  caller's own ``sub``. Rows owned by other users are COUNTED and LISTED
  (key, owner, created/modified ms) and NEVER touched: an ownership-preserving
  re-classify path for them is a separate follow-up, opened only if that count
  is above zero.
* ``chunk_owner_before`` / ``chunk_owner_after`` are read around every real
  re-index and must be equal; on a mismatch the script STOPS (exit 3). A row
  whose chunk owner cannot be read is SKIPPED, not guessed, because the
  invariant cannot be checked for it.

**Dry run first.** Without ``--apply`` the script only issues
``POST /memory/index?file=<key>&dry_run=true&details=true`` (no write) and
prints the before/after table. ``--apply`` runs the dry run for every row
first and re-indexes only rows whose dry run answered 200, one at a time,
sleeping between rows (APU cap).

**Credentials.** The scoped JWT is resolved in-process (explicit argument,
``AUDITTRACE_TOKEN``, or the operator's login token file via
``scripts.deploy.memory``), sent only as a Bearer header, and never printed or
written. The caller's ``sub`` is read from the token's own payload.

**Portability.** The front door comes from ``--front-door`` or
``AUDITTRACE_FRONT_DOOR`` (``scripts.deploy.frontdoor``); nothing target-shaped
is hardcoded here.

Off-gate by design (operator tooling, not a served API): it lives in
``scripts/`` and imports nothing from ``src/audittrace``.
"""

from __future__ import annotations

import argparse
import base64
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
    chunk_owner_before: str | None = None
    chunk_owner_after: str | None = None
    outcome: str = "pending"  # dry_run_ok | applied | skipped:<why> | failed:<why>


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


def list_check_failed(client: Client, layer: str) -> list[dict[str, Any]]:
    """Every non-deleted ``check_failed`` row the caller can see in *layer*,
    through the public list read (all dates: never selected by ``created_at``
    alone)."""
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
        rows += [
            i
            for i in page
            if i.get("signature_status") == AFFECTED_STATUS
            and i.get("deleted_at_ms") is None
        ]
        offset += len(page)
        total = body.get("total")
        if not page or not isinstance(total, int) or offset >= total:
            return rows


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


def read_chunk_owner(client: Client, key: str) -> str | None:
    """Who owns the document's chunks, via the public semantic list at chunk
    granularity (``created_by_user_id`` is the chunk's ``user_id`` metadata).

    Returns the sorted, comma-joined distinct owners of the chunk rows whose
    title is the document's file name, or ``None`` when no chunk row is
    visible (the invariant cannot be checked for that row)."""
    filename = key.rsplit("/", 1)[-1]
    owners: set[str] = set()
    found = False
    offset = 0
    while True:
        status, _h, body = client.request(
            "GET",
            "/memory/semantic",
            {
                "collection": CHUNK_COLLECTION,
                "granularity": "chunk",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
        )
        if status != 200 or not isinstance(body, dict):
            return None
        page = [i for i in body.get("items", []) if isinstance(i, dict)]
        for item in page:
            if item.get("title") == filename:
                found = True
                owners.add(str(item.get("created_by_user_id")))
        offset += len(page)
        total = body.get("total")
        if not page or not isinstance(total, int) or offset >= total:
            break
    return ",".join(sorted(owners)) if found else None


# ───────────────────────────────── the run ─────────────────────────────────


def _post_index(
    client: Client, file_param: str, *, dry_run: bool
) -> tuple[int, str | None, str | None]:
    """``POST /memory/index?file=...&details=true[&dry_run=true]``; returns
    ``(http_status, signature_status, trace_id)``."""
    params: dict[str, Any] = {"file": file_param, "details": "true"}
    if dry_run:
        params["dry_run"] = "true"
    status, headers, body = client.request("POST", "/memory/index", params)
    new_status: str | None = None
    if status == 200 and isinstance(body, dict):
        docs = body.get("documents")
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            value = docs[0].get("signature_status")
            new_status = value if isinstance(value, str) else None
    trace_id = headers.get("x-trace-id") or headers.get("traceparent")
    return status, new_status, trace_id


def run_backfill(
    client: Client,
    caller_sub: str,
    *,
    layers: tuple[str, ...] = LAYERS,
    apply: bool = False,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    owner_reader: Callable[[Client, str], str | None] | None = None,
) -> BackfillReport:
    """Select, dry-run and (with ``apply``) re-classify the caller's own rows."""
    read_owner = owner_reader or read_chunk_owner
    report = BackfillReport(caller_sub=caller_sub, applied=apply)
    candidates: list[tuple[TableRow, str]] = []
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
            file_param = index_file_param(row, caller_sub, layer)
            candidates.append(
                (
                    TableRow(
                        layer=layer, key=str(row["key"]), old_status=AFFECTED_STATUS
                    ),
                    file_param,
                )
            )

    # Phase 1: dry run for every caller-owned row.
    runnable: list[tuple[TableRow, str]] = []
    for table_row, file_param in candidates:
        before = read_owner(client, table_row.key)
        table_row.chunk_owner_before = before
        status, new_status, trace_id = _post_index(client, file_param, dry_run=True)
        table_row.new_status, table_row.trace_id = new_status, trace_id
        report.rows.append(table_row)
        if status != 200:
            table_row.outcome = f"failed:dry_run_http_{status}"
        elif before is None:
            table_row.outcome = "skipped:chunk_owner_unreadable"
        else:
            table_row.outcome = "dry_run_ok"
            runnable.append((table_row, file_param))

    if not apply:
        return report

    # Phase 2: real re-index, one row at a time, owner invariant enforced.
    for table_row, file_param in runnable:
        sleep(sleep_seconds)
        status, new_status, trace_id = _post_index(client, file_param, dry_run=False)
        table_row.trace_id = trace_id or table_row.trace_id
        if status != 200:
            table_row.outcome = f"failed:index_http_{status}"
            continue
        table_row.new_status = new_status
        table_row.chunk_owner_after = read_owner(client, table_row.key)
        if table_row.chunk_owner_after != table_row.chunk_owner_before:
            table_row.outcome = "failed:chunk_owner_changed"
            raise OwnerMismatchError(
                f"chunk owner changed for {table_row.key}: "
                f"{table_row.chunk_owner_before!r} -> {table_row.chunk_owner_after!r}"
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
        "key | old_status | new_status | trace_id | chunk_owner_before | "
        "chunk_owner_after | outcome",
    ]
    for r in report.rows:
        lines.append(
            f"{r.layer}/{r.key} | {r.old_status} | {r.new_status} | {r.trace_id} | "
            f"{r.chunk_owner_before} | {r.chunk_owner_after} | {r.outcome}"
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
