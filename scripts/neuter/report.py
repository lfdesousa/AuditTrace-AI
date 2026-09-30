"""Per-guard report generation + ``--verify`` (SPEC v3 §13; SF-C, SF-D).

Generated from ``neuter_results.jsonl`` (+ ``arbitration.jsonl``) only.
``--verify`` fails (exit 7) on any of:

- a byte difference against the report on disk;
- an arbitration row that disagrees with the harness without a
  ``defect_ref`` (SF-D);
- a neuter in the spec with NO row at all (review round 1 blocker 1 --
  a report can't certify a run that silently dropped ids);
- a GREEN row with no full-scope arbitration confirming it GREEN (review
  round 1 blocker 3 -- "every GREEN is arbitrated", never convention-only);
- any ERROR row, unless the caller passes ``ack_errors=True`` (review
  round 1 blocker 1 -- ``error_n > 0`` can never pass silently).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

EXIT_VERIFY_FAILED = 7

_COLUMNS = [
    "id",
    "guard",
    "clause",
    "file",
    "engines",
    "mapped n",
    "collected",
    "failed n",
    "failure_types",
    "verdict",
    "error_reason",
    "secs",
    "worker",
    "restored",
    "pg_settings",
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _row_line(row: dict[str, Any], neuters_by_id: dict[str, Any]) -> str:
    entry = neuters_by_id.get(row["id"])
    guard = getattr(entry, "guard", "") if entry else ""
    clause = getattr(entry, "clause", "") if entry else ""
    engines = ",".join(getattr(entry, "engines", []) or []) if entry else ""
    return (
        "| "
        + " | ".join(
            str(v)
            for v in (
                row["id"],
                guard,
                clause,
                row.get("file", ""),
                engines,
                row.get("tests_expected", ""),
                row.get("tests_collected", ""),
                len(row.get("failed", [])),
                ",".join(row.get("failure_types", [])),
                row["verdict"],
                row.get("error_reason", ""),
                row.get("secs", ""),
                row.get("worker", ""),
                row.get("restored_clean", ""),
                row.get("pg_settings", ""),
            )
        )
        + " |"
    )


def _guard_tests_section(
    builder_guard_tests: list[Any],
    reviewer_guard_tests: list[Any] | None,
    union_tests: set[str],
) -> str:
    builder_ids = sorted({g.id for g in builder_guard_tests})
    lines = ["## GUARD-TESTS", "", f"builder ({len(builder_ids)}): {builder_ids}", ""]
    if reviewer_guard_tests is not None:
        reviewer_ids = sorted({g.id for g in reviewer_guard_tests})
        diff = sorted(set(builder_ids) ^ set(reviewer_ids))
        lines += [
            f"reviewer ({len(reviewer_ids)}): {reviewer_ids}",
            "",
            f"diff: {diff}",
            "",
        ]
    lines += [f"union(tests) ({len(union_tests)}): {sorted(union_tests)}", ""]
    return "\n".join(lines)


def generate_report(
    evidence_dir: Path,
    spec: Any,
    *,
    reviewer_guard_tests: list[Any] | None = None,
    harness_version: str = "1",
    run_id: str = "",
) -> str:
    """Build ``per_guard_table.md`` text (not yet written to disk)."""
    rows = _read_jsonl(evidence_dir / "neuter_results.jsonl")
    arbitration = _read_jsonl(evidence_dir / "arbitration.jsonl")
    neuters_by_id = {n.id: n for n in spec.neuters}

    if not run_id:
        # Auto-populate from the rows themselves (should-fix: the trailer's
        # run_id used to stay empty unless a caller remembered to pass it).
        run_ids = {r.get("run_id") for r in rows if r.get("run_id")}
        run_id = sorted(run_ids)[0] if len(run_ids) == 1 else ",".join(sorted(run_ids))

    expected_ids = {n.id for n in spec.neuters}
    got_ids = {r["id"] for r in rows}
    missing_ids = sorted(expected_ids - got_ids)

    green_rows = [r for r in rows if r["verdict"] == "GREEN"]
    error_rows = [r for r in rows if r["verdict"] == "ERROR"]
    drift_rows = [r for r in rows if r.get("unmapped_red")]

    lines = ["# per_guard_table.md", ""]
    lines.append("| " + " | ".join(_COLUMNS) + " |")
    lines.append("|" + "---|" * len(_COLUMNS))
    for row in sorted(rows, key=lambda r: r["id"]):
        lines.append(_row_line(row, neuters_by_id))
    lines.append("")

    lines.append(f"## MISSING (missing_n={len(missing_ids)})")
    lines += [f"- {mid}" for mid in missing_ids]
    lines.append("")

    lines.append(f"## GREEN ({len(green_rows)})")
    lines += [f"- {r['id']}" for r in sorted(green_rows, key=lambda r: r["id"])]
    lines.append("")

    lines.append(f"## ERROR ({len(error_rows)})")
    by_reason: dict[str, list[str]] = {}
    for row in error_rows:
        by_reason.setdefault(row.get("error_reason", "unknown"), []).append(row["id"])
    for reason, ids in sorted(by_reason.items()):
        lines.append(f"- {reason}: {sorted(ids)}")
    lines.append("")

    drift_n = len(drift_rows)
    lines.append(f"## DRIFT (drift_n={drift_n})")
    for row in drift_rows:
        lines.append(f"- {row['id']}: unmapped_red={row['unmapped_red']}")
    lines.append("")

    union_tests: set[str] = set()
    for n in spec.neuters:
        union_tests.update(n.tests)
    lines.append(
        _guard_tests_section(spec.guard_tests, reviewer_guard_tests, union_tests)
    )

    lines.append("## ARBITRATION")
    defect_n = 0
    arb_by_id: dict[str, dict[str, Any]] = {}
    for row in arbitration:
        arb_by_id[row["id"]] = row
        lines.append(
            f"- {row['id']}: harness={row.get('harness_verdict')} authoritative={row.get('authoritative_verdict')} "
            f"defect_ref={row.get('defect_ref')}"
        )
        if row.get("harness_verdict") != row.get(
            "authoritative_verdict"
        ) and not row.get("defect_ref"):
            defect_n += 1
    lines.append("")

    # Blocker 3: "every GREEN is arbitrated" is enforced in CODE, not
    # convention -- a GREEN row with no arbitration entry confirming it
    # GREEN is listed here (and fails --verify below), never silently
    # certified as clean.
    unconfirmed_green = sorted(
        r["id"]
        for r in green_rows
        if arb_by_id.get(r["id"], {}).get("authoritative_verdict") != "GREEN"
    )
    lines.append(f"## UNCONFIRMED-GREEN (unconfirmed_green_n={len(unconfirmed_green)})")
    lines += [f"- {gid}" for gid in unconfirmed_green]
    lines.append("")

    error_n = len(error_rows)
    body = "\n".join(lines)
    sha = hashlib.sha256(body.encode()).hexdigest()
    trailer = (
        f"generated-from: {evidence_dir}/neuter_results.jsonl sha256={sha} "
        f"harness={harness_version} run_id={run_id} drift_n={drift_n} error_n={error_n} "
        f"arbitrated_n={len(arbitration)} defect_n={defect_n} missing_n={len(missing_ids)} "
        f"unconfirmed_green_n={len(unconfirmed_green)}"
    )
    return body + "\n" + trailer + "\n"


def write_report(evidence_dir: Path, spec: Any, **kwargs: Any) -> Path:
    text = generate_report(evidence_dir, spec, **kwargs)
    out = evidence_dir / "per_guard_table.md"
    out.write_text(text)
    return out


def _parse_trailer_fields(trailer_line: str) -> dict[str, str]:
    """Parse the trailer's own ``key=value`` tokens (review round 2
    should-fix): the trailer EMBEDS the evidence directory's path
    (``generated-from: <evidence_dir>/neuter_results.jsonl ...``), so a
    plain substring check (``"drift_n=0" not in trailer_line``) is fooled
    by an evidence directory whose OWN path happens to contain that exact
    text (e.g. a test fixture literally named ``.../drift_n=0/...``).
    Splitting on whitespace and requiring an EXACT key match before the
    first ``=`` means a malformed "key" lifted out of the path (which will
    contain ``/`` and never equals a real field name) can never collide
    with the genuine trailer field emitted later on the same line."""
    fields: dict[str, str] = {}
    for token in trailer_line.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    return fields


def verify_report(
    evidence_dir: Path,
    spec: Any,
    *,
    ack_errors: bool = False,
    ack_drift: bool = False,
    **kwargs: Any,
) -> int:
    """``--verify``: exit 7 on any byte difference, an open arbitration
    defect (SF-D), a spec neuter with no row (blocker 1), a GREEN with no
    confirming full-scope arbitration (blocker 3), an unacknowledged ERROR
    (``error_n > 0`` without ``ack_errors=True``, blocker 1), or an
    unacknowledged non-empty ``unmapped_red`` on ANY row (``drift_n > 0``
    without ``ack_drift=True`` -- review round 2 blocker 4: a RED row with
    drift is not "clean" just because its own targeted verdict was already
    RED)."""
    out = evidence_dir / "per_guard_table.md"
    if not out.exists():
        return EXIT_VERIFY_FAILED
    fresh = generate_report(evidence_dir, spec, **kwargs)
    if fresh != out.read_text():
        return EXIT_VERIFY_FAILED
    trailer_line = fresh.splitlines()[-1]
    fields = _parse_trailer_fields(trailer_line)
    if fields.get("defect_n") != "0":
        return EXIT_VERIFY_FAILED
    if fields.get("missing_n") != "0":
        return EXIT_VERIFY_FAILED
    if fields.get("unconfirmed_green_n") != "0":
        return EXIT_VERIFY_FAILED
    if fields.get("error_n") != "0" and not ack_errors:
        return EXIT_VERIFY_FAILED
    if fields.get("drift_n") != "0" and not ack_drift:
        return EXIT_VERIFY_FAILED
    return 0
