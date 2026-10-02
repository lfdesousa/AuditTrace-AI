"""#459 WU-459-1: the decision client is not wired into any route yet.

Until WU-459-2 wires the decision client into the chat route deliberately, no
module under ``src/audittrace/routes/`` may import ``audittrace.services.decision``.

This guards ONE thing: that import edge. It does not guard the routes diff and
it says nothing about the ``/v1`` wire bytes (those stay with the existing
OpenAI-compatibility tests). It is a slice guard with a planned end: WU-459-2
removes or replaces it as part of its own spec.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROUTES = REPO / "src/audittrace/routes"


def find_route_importers(routes: Path, root: Path) -> list[str]:
    """Route modules that import the decision package (must be none)."""
    pattern = re.compile(r"audittrace\.services\.decision|services\s+import\s+decision")
    return [
        str(p.relative_to(root))
        for p in sorted(routes.rglob("*.py"))
        if pattern.search(p.read_text(encoding="utf-8"))
    ]


def test_no_route_module_imports_the_decision_package() -> None:
    assert find_route_importers(ROUTES, REPO) == []


@pytest.mark.parametrize(
    "line",
    [
        "from audittrace.services.decision import LlamaCppDecisionClient",
        "import audittrace.services.decision.client",
        "from audittrace.services import decision",
    ],
)
def test_the_static_import_check_sees_a_planted_importer(
    tmp_path: Path, line: str
) -> None:
    routes = tmp_path / "routes" / "sub"
    routes.mkdir(parents=True)
    (tmp_path / "routes" / "ok.py").write_text("x = 1\n")
    (routes / "bad.py").write_text(line + "\n")
    assert find_route_importers(tmp_path / "routes", tmp_path) == ["routes/sub/bad.py"]
