"""Per-guard report generation + ``--verify`` (SPEC v3 §13; SF-C, SF-D).

Generated from ``neuter_results.jsonl`` (+ ``arbitration.jsonl``) only.
``--verify`` byte-compares a freshly generated report against the one on
disk and exits 7 on any difference, or if any arbitration row lacks a
``defect_ref`` (SF-D) -- a table without a matching trailer, or with an
open arbitration defect, is not evidence.
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

    green_rows = [r for r in rows if r["verdict"] == "GREEN"]
    error_rows = [r for r in rows if r["verdict"] == "ERROR"]
    drift_rows = [r for r in rows if r.get("unmapped_red")]

    lines = ["# per_guard_table.md", ""]
    lines.append("| " + " | ".join(_COLUMNS) + " |")
    lines.append("|" + "---|" * len(_COLUMNS))
    for row in sorted(rows, key=lambda r: r["id"]):
        lines.append(_row_line(row, neuters_by_id))
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
    for row in arbitration:
        lines.append(
            f"- {row['id']}: harness={row.get('harness_verdict')} authoritative={row.get('authoritative_verdict')} "
            f"defect_ref={row.get('defect_ref')}"
        )
        if row.get("harness_verdict") != row.get(
            "authoritative_verdict"
        ) and not row.get("defect_ref"):
            defect_n += 1
    lines.append("")

    error_n = len(error_rows)
    body = "\n".join(lines)
    sha = hashlib.sha256(body.encode()).hexdigest()
    trailer = (
        f"generated-from: {evidence_dir}/neuter_results.jsonl sha256={sha} "
        f"harness={harness_version} run_id={run_id} drift_n={drift_n} error_n={error_n} "
        f"arbitrated_n={len(arbitration)} defect_n={defect_n}"
    )
    return body + "\n" + trailer + "\n"


def write_report(evidence_dir: Path, spec: Any, **kwargs: Any) -> Path:
    text = generate_report(evidence_dir, spec, **kwargs)
    out = evidence_dir / "per_guard_table.md"
    out.write_text(text)
    return out


def verify_report(evidence_dir: Path, spec: Any, **kwargs: Any) -> int:
    """``--verify``: exit 7 on any byte difference, or an open arbitration
    defect (SF-D)."""
    out = evidence_dir / "per_guard_table.md"
    if not out.exists():
        return EXIT_VERIFY_FAILED
    fresh = generate_report(evidence_dir, spec, **kwargs)
    if fresh != out.read_text():
        return EXIT_VERIFY_FAILED
    trailer_line = fresh.splitlines()[-1]
    if "defect_n=0" not in trailer_line:
        return EXIT_VERIFY_FAILED
    return 0
