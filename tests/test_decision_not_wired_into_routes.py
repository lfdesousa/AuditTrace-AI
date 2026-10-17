"""#459 WU-459-1: the decision client is not wired into any route yet.

Until WU-459-2 wires the decision client into the chat route deliberately, no
module under ``src/audittrace/routes/`` may import ``audittrace.services.decision``.

This guards ONE thing: that import edge, found by an AST walk over every module
under the routes package. Every ``import`` and ``from ... import`` statement is
resolved to absolute dotted names (relative imports are resolved against the
module's own package) and flagged when a name equals or sits under
``audittrace.services.decision``. Forms covered:

- ``import audittrace.services.decision[.x]`` and the ``as`` alias form
- ``from audittrace.services.decision[.x] import name``
- ``from audittrace.services import decision`` (the imported name is part of
  the resolved name)
- the same three shapes written as relative imports (``from ..services...``)
- any of the above anywhere in the module: inside a function, ``async def``,
  ``if``, ``try``, a class body or an ``if TYPE_CHECKING:`` block
- a multi-name ``from audittrace.services import mcp_broker, decision`` (the
  decision name need not come first)

NOT covered, by design: dynamic imports (``importlib.import_module``,
``__import__``, ``exec``) and attribute access on an already-imported parent package. It
does not guard the routes diff and says nothing about the ``/v1`` wire bytes
(those stay with the existing OpenAI-compatibility tests). It is a slice guard
with a planned end: WU-459-2 removes or replaces it as part of its own spec.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROUTES = REPO / "src/audittrace/routes"
ROUTES_PACKAGE = "audittrace.routes"
TARGET = "audittrace.services.decision"


def _module_package(routes: Path, file: Path, routes_package: str) -> str:
    """Dotted name of the package a module lives in (an __init__ is its own)."""
    parts = [*routes_package.split("."), *file.relative_to(routes).parts[:-1]]
    return ".".join(parts)


def _imported_names(node: ast.AST, package: str) -> list[str]:
    """Absolute dotted names a single import statement refers to."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    base = node.module or ""
    if node.level:
        anchor = package.split(".")
        if node.level - 1 > len(anchor):
            return []  # reaches above the top-level package: not valid Python
        anchor = anchor[: len(anchor) - (node.level - 1)]
        base = ".".join([*anchor, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _hits_target(name: str) -> bool:
    return name == TARGET or name.startswith(TARGET + ".")


def find_route_importers(
    routes: Path, root: Path, routes_package: str = ROUTES_PACKAGE
) -> list[str]:
    """Route modules that import the decision package (must be none)."""
    found: list[str] = []
    for path in sorted(routes.rglob("*.py")):
        package = _module_package(routes, path, routes_package)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = (n for node in ast.walk(tree) for n in _imported_names(node, package))
        if any(_hits_target(n) for n in names):
            found.append(str(path.relative_to(root)))
    return found


def test_no_route_module_imports_the_decision_package() -> None:
    assert find_route_importers(ROUTES, REPO) == []


PLANTS = {
    "absolute-import": "import audittrace.services.decision.client",
    "absolute-import-aliased": "import audittrace.services.decision as dec",
    "from-absolute": "from audittrace.services.decision import LlamaCppDecisionClient",
    "from-absolute-submodule": "from audittrace.services.decision.client import X",
    "from-package-import-name": "from audittrace.services import decision",
    "relative-module": "from ..services.decision import LlamaCppDecisionClient",
    "relative-submodule": "from ..services.decision.client import LlamaCppDecisionClient",
    "relative-package-import-name": "from ..services import decision",
    "multi-name-absolute": "from audittrace.services import mcp_broker, decision",
    "multi-name-relative": "from ..services import mcp_broker, decision",
    "inside-def": "def f():\n    from audittrace.services.decision import X\n",
    "inside-async-def": "async def f():\n    import audittrace.services.decision\n",
    "inside-if": "if True:\n    from audittrace.services.decision import X\n",
    "inside-try": "try:\n    from ..services.decision import X\nexcept ImportError:\n    X = None\n",
    "inside-class-body": "class C:\n    from audittrace.services import decision\n",
    "under-type-checking": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from audittrace.services.decision import X\n"
    ),
}
RELATIVE = [k for k in PLANTS if k.startswith("relative")]


@pytest.mark.parametrize("name", list(PLANTS))
def test_the_guard_sees_every_planted_importer_form(tmp_path: Path, name: str) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "ok.py").write_text("x = 1\n")
    (routes / "bad.py").write_text(PLANTS[name] + "\n")
    assert find_route_importers(routes, tmp_path) == ["routes/bad.py"]


def test_the_guard_sees_the_exact_relative_plant_in_a_real_route_module(
    tmp_path: Path,
) -> None:
    """The reviewer's plant: appended to a copy of routes/chat.py."""
    routes = tmp_path / "routes"
    routes.mkdir()
    chat = (ROUTES / "chat.py").read_text(encoding="utf-8")
    plant = "from ..services.decision.client import LlamaCppDecisionClient\n"
    (routes / "chat.py").write_text(chat + "\n" + plant)
    assert find_route_importers(routes, tmp_path) == ["routes/chat.py"]


def test_relative_levels_resolve_against_the_modules_own_package(
    tmp_path: Path,
) -> None:
    routes = tmp_path / "routes"
    (routes / "sub").mkdir(parents=True)
    # In audittrace.routes.sub, level 3 reaches `audittrace`: a real hit.
    (routes / "sub" / "deep.py").write_text("from ...services.decision import X\n")
    # Level 2 reaches audittrace.routes: audittrace.routes.services.decision
    # is NOT the decision package.
    (routes / "sub" / "near.py").write_text("from ..services.decision import X\n")
    assert find_route_importers(routes, tmp_path) == ["routes/sub/deep.py"]


@pytest.mark.parametrize(
    "source",
    [
        "from . import decision",
        "from audittrace.services import decision_other",
        "from audittrace.services.decisions import x",
        "import audittrace.services.decisionx",
        "# from audittrace.services.decision import X",
        'S = "from audittrace.services.decision import X"',
        "from audittrace.services import mcp_broker",
        "from .....toodeep import x",
    ],
)
def test_the_guard_does_not_flag_look_alikes_comments_or_strings(
    tmp_path: Path, source: str
) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "ok.py").write_text(source + "\n")
    assert find_route_importers(routes, tmp_path) == []
