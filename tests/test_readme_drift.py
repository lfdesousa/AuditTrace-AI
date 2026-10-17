"""README drift guard.

Pins high-value, deterministic facts in ``README.md`` to the live system
so #365-class staleness (a 68-day-old README claiming a wrong test count
and documenting endpoints that had drifted) surfaces as a test failure
instead of rotting silently.

Two gates, both run in the normal suite (so CI *and* the local
CI-Agent gate enforce them):

1. **Endpoint-table drift (hard gate)** — every ``(path, method)`` the
   README's ``## API Endpoints`` table documents must exist in the live
   ``app.openapi()["paths"]``. Direction: "no documented endpoint is a
   lie." We deliberately do NOT require every live route to be
   documented (some are intentionally omitted from the human-facing
   table); the failure that bit us in #365 was a *documented* endpoint
   drifting, not an *undocumented* one.

2. **No hardcoded test count (regression guard)** — the README used to
   hardcode the exact suite size in two prose sites
   (``**Comprehensive Test Suite** -- N tests,`` and ``full suite is N
   tests /``). Because every branch that added or removed a test then
   had to rewrite that integer, two branches in flight both bumping the
   same line conflicted on rebase/merge (#456, #412 —
   ``feedback_dev_cycle_stabilization_pain_is_signal``: the recurring
   conflict is the signal). The number was moved OFF git entirely: CI
   (``.github/workflows/test-count-badge.yml``) computes the live count
   on every push to ``main`` and publishes it as a shields *endpoint*
   badge on the orphan ``badges`` branch; README.md renders it via the
   ``![Tests](...)`` badge instead of a committed integer. This gate is
   therefore inverted from the old freshness check: it asserts the two
   old hardcoded-integer patterns are **absent**, so nobody reintroduces
   the churn.

Coverage %% is deliberately kept OUT of these gates: it fluctuates
per-run and is circular to assert from inside the suite (and, per spec,
is not a per-PR churner the way the test count was).

Gate 1 mirrors the blessed OpenAPI-drift pattern
(``tests/test_openapi_drift.py`` + ``make openapi-export``,
``OPENAPI_SNAPSHOT_UPDATE=1``): the runtime is the source of truth, a
Make target regenerates the doc-side figure, and the diff lands in git
for the reviewer. Gate 2 deliberately does NOT follow that pattern —
there is nothing to regenerate; CI owns the number and the repo only
enforces its committed absence.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
README_PATH = REPO_ROOT / "README.md"

_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# The two prose sites that used to hardcode the suite size. Kept as a
# regression guard: neither pattern may reappear in README.md.
_HARDCODED_COUNT_PATTERNS = (
    re.compile(r"\*\*Comprehensive Test Suite\*\* -- \d+ tests,"),
    re.compile(r"full suite is \d+ tests"),
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


# ── Gate 2: no hardcoded test count may be committed ─────────────────────────


def test_readme_has_no_hardcoded_test_count() -> None:
    """README.md must never again hardcode the exact suite size.

    The two prose patterns that used to churn on every add/remove-a-test
    branch (``**Comprehensive Test Suite** -- N tests,`` and ``full suite
    is N tests``) must be absent. The live count now lives exclusively in
    the CI-published shields endpoint badge
    (``.github/workflows/test-count-badge.yml`` -> ``badges`` branch ->
    ``test-count.json``); README.md renders it via the badge image, never
    as a committed integer.
    """
    readme = _readme_text()
    offenders = [
        pattern.pattern
        for pattern in _HARDCODED_COUNT_PATTERNS
        if pattern.search(readme)
    ]
    assert not offenders, (
        "README.md re-introduces a hardcoded test-count pattern: "
        f"{offenders}. The count is CI-derived (test-count-badge.yml -> "
        "badges branch); do not hand-edit an integer into README.md."
    )
