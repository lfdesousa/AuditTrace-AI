"""README drift guard.

Pins two high-value, deterministic facts in ``README.md`` to the live
system so #365-class staleness (a 68-day-old README claiming a wrong
test count and documenting endpoints that had drifted) surfaces as a
test failure instead of rotting silently.

Two gates, both run in the normal suite (so CI *and* the local
CI-Agent gate enforce them):

1. **Endpoint-table drift (hard gate)** — every ``(path, method)`` the
   README's ``## API Endpoints`` table documents must exist in the live
   ``app.openapi()["paths"]``. Direction: "no documented endpoint is a
   lie." We deliberately do NOT require every live route to be
   documented (some are intentionally omitted from the human-facing
   table); the failure that bit us in #365 was a *documented* endpoint
   drifting, not an *undocumented* one.

2. **Test-count freshness (regenerable gate)** — the ``NNNN tests``
   figure the README quotes must equal the live collected count. On
   mismatch the fix is one command::

       make readme-export

   which rewrites the count in place from a fresh collection.

Coverage %% is deliberately kept OUT of these gates: it fluctuates
per-run and is circular to assert from inside the suite. Test count and
the endpoint table are the deterministic, high-value invariants.

Mirrors the blessed OpenAPI-drift pattern
(``tests/test_openapi_drift.py`` + ``make openapi-export``,
``OPENAPI_SNAPSHOT_UPDATE=1``): the runtime is the source of truth, a
Make target regenerates the doc-side figure, and the diff lands in git
for the reviewer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
README_PATH = REPO_ROOT / "README.md"

_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# The two README sites that quote the suite size. Each capture group is
# the integer count; the surrounding text is stable and unique enough to
# target without matching unrelated "N tests" prose.
_COUNT_PATTERNS = (
    re.compile(r"(\*\*Comprehensive Test Suite\*\* -- )(\d+)( tests,)"),
    re.compile(r"(full suite is )(\d+)( tests /)"),
)


def _readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


def _parse_api_endpoint_rows(readme: str) -> list[tuple[str, str]]:
    """Return ``(path, method)`` pairs documented in the README's
    ``## API Endpoints`` table.

    A row looks like::

        | `/memory/{layer}` | GET / POST / PUT / DELETE | ... |

    The method cell may hold several ``/``-separated verbs; each becomes
    its own ``(path, method)`` pair.
    """
    lines = readme.splitlines()
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in lines:
        if line.strip().startswith("## API Endpoints"):
            in_table = True
            continue
        if in_table:
            stripped = line.strip()
            if stripped.startswith("## "):
                break  # next section — table is over
            if not stripped.startswith("|"):
                continue  # blank line or prose between header and table
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            path_match = re.search(r"`([^`]+)`", cells[0])
            if path_match is None:
                continue  # header row / separator row — no backticked path
            path = path_match.group(1)
            if not path.startswith("/"):
                continue
            for token in cells[1].split("/"):
                method = token.strip().upper()
                if method in _HTTP_METHODS:
                    rows.append((path, method))
    return rows


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def _is_placeholder(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _doc_prefix_matches_live(doc_segs: list[str], live_segs: list[str]) -> bool:
    """True if the documented path is a segment-wise prefix of a live
    path, treating any ``{placeholder}`` on either side as a
    single-segment wildcard.

    Prefix (not exact) matching is intentional: the README compresses a
    resource family such as ``/memory/{layer}`` (with GET/POST/PUT/DELETE)
    into one row, while the live app splits it across ``/memory/episodic``
    (GET, POST) and ``/memory/episodic/{filename}`` (GET, PUT, DELETE). A
    documented row is honest as long as *some* live path under it serves
    the method. A genuinely wrong path (wrong literal segment) still
    fails, because a literal-vs-literal mismatch is never wild-carded.
    """
    if len(doc_segs) > len(live_segs):
        return False
    for doc_seg, live_seg in zip(doc_segs, live_segs):
        if _is_placeholder(doc_seg) or _is_placeholder(live_seg):
            continue
        if doc_seg != live_seg:
            return False
    return True


def _live_openapi_paths() -> dict[str, set[str]]:
    """Return ``{path: {UPPER_METHOD, ...}}`` from the live FastAPI app.

    Imported inside the function so create_app()'s import-time side
    effects (logging setup) stay scoped to this call — mirrors
    ``tests/test_openapi_drift.py``.
    """
    from audittrace.server import create_app

    app = create_app()
    paths = app.openapi().get("paths") or {}
    return {
        path: {method.upper() for method in ops if method.upper() in _HTTP_METHODS}
        for path, ops in paths.items()
    }


def _live_collected_count() -> int:
    """Collect ``tests/`` in a fresh subprocess and return the total
    number of collected test items.

    A subprocess is used so the count is independent of the running
    pytest session (no self-reference into this module's own collection)
    and deterministic regardless of ``-k`` / selection args the parent
    invocation used. ``-o addopts=""`` strips the coverage addopts so
    ``--collect-only`` stays a pure count.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "tests"),
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if match is None:
        raise AssertionError(
            "Could not determine live collected test count from "
            f"pytest --collect-only output:\n{proc.stdout[-2000:]}\n"
            f"{proc.stderr[-2000:]}"
        )
    return int(match.group(1))


def _readme_stated_counts(readme: str) -> list[int]:
    counts: list[int] = []
    for pattern in _COUNT_PATTERNS:
        match = pattern.search(readme)
        assert match is not None, (
            f"README is missing the test-count site matched by {pattern.pattern!r}. "
            "The README drift guard depends on both count sites being present."
        )
        counts.append(int(match.group(2)))
    return counts


# ── Gate 1: documented endpoints must exist live ────────────────────────────


def test_documented_endpoints_exist_in_live_openapi() -> None:
    """Every ``(path, method)`` in the README API-Endpoints table must
    map to a live route in ``app.openapi()``."""
    rows = _parse_api_endpoint_rows(_readme_text())
    assert rows, (
        "Parsed zero endpoint rows from README '## API Endpoints' — the "
        "parser or the table heading drifted."
    )

    live = _live_openapi_paths()
    live_items = [(path, _segments(path), methods) for path, methods in live.items()]

    offenders: list[str] = []
    for doc_path, doc_method in rows:
        doc_segs = _segments(doc_path)
        confirmed = any(
            doc_method in methods and _doc_prefix_matches_live(doc_segs, live_segs)
            for _live_path, live_segs, methods in live_items
        )
        if not confirmed:
            offenders.append(f"{doc_method} {doc_path}")

    assert not offenders, (
        "README '## API Endpoints' documents route(s) with no matching live "
        f"path+method (drift — a documented endpoint is a lie): {offenders}. "
        "Fix the README row or the route; live paths are: "
        f"{sorted(live)}"
    )


# ── Gate 2: README test count must match the live collected count ────────────


def test_readme_test_count_matches_live() -> None:
    """The ``NNNN tests`` figure quoted in the README (two sites) must
    equal the live collected count. On mismatch, run ``make
    readme-export`` to refresh it."""
    readme = _readme_text()
    stated = _readme_stated_counts(readme)

    # Both README sites must already agree with each other.
    assert len(set(stated)) == 1, (
        f"README quotes inconsistent test counts across its two sites: {stated}. "
        "Run 'make readme-export'."
    )

    live_count = _live_collected_count()
    assert stated[0] == live_count, (
        f"README test count is stale — run 'make readme-export' "
        f"(README says {stated[0]} tests, live collection is {live_count})."
    )


# ── Regeneration entrypoint (mirrors OPENAPI_SNAPSHOT_UPDATE=1) ──────────────


def test_readme_count_export() -> None:
    """When ``README_STATS_UPDATE=1`` is set, rewrite the README test
    count in place from the live collection (this is what ``make
    readme-export`` runs); otherwise no-op.

    Kept in the test module — not a standalone script — so the exact
    parse/rewrite logic the gate asserts against is the same code that
    regenerates the figure, exactly like ``test_openapi_drift`` doubles
    as its own snapshot writer under ``OPENAPI_SNAPSHOT_UPDATE=1``.
    """
    if os.environ.get("README_STATS_UPDATE") != "1":
        return

    live_count = _live_collected_count()
    readme = _readme_text()
    for pattern in _COUNT_PATTERNS:
        readme = pattern.sub(rf"\g<1>{live_count}\g<3>", readme)
    README_PATH.write_text(readme, encoding="utf-8")
