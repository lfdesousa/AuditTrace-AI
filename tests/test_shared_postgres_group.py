"""Every test module that reaches the shared Postgres is in the xdist group.

Recomputes the set from the sources (AST, no imports run) and compares it with
``tests/_shared_postgres.SHARED_POSTGRES_MODULES``:

* a ROOT is a module that reads ``AUDITTRACE_TEST_POSTGRES_URL`` and otherwise
  starts an ephemeral container (the "env URL, else own container" pattern), or
  any test module in a directory whose ``conftest.py`` does;
* a module is in the set when it is a root or imports (``tests.<module>``,
  transitively) a module that is.

Neuter: delete a module from the set -> ``missing`` is non-empty, RED.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._shared_postgres import SHARED_POSTGRES_MODULES, XDIST_GROUP

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"
ENV_NAME = "AUDITTRACE_TEST_POSTGRES_URL"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _is_root(tree: ast.Module) -> bool:
    has_env = False
    starts_container = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == ENV_NAME:
            has_env = True
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name == "start_ephemeral_postgres":
                starts_container = True
    return has_env and starts_container


def _test_imports(tree: ast.Module) -> set[str]:
    return {
        node.module.removeprefix("tests.")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("tests.")
    }


def _computed_set() -> set[str]:
    modules = {p.stem: p for p in TESTS.glob("test_*.py")}
    trees = {stem: _parse(path) for stem, path in modules.items()}
    users = {stem for stem, tree in trees.items() if _is_root(tree)}
    changed = True
    while changed:
        changed = False
        for stem, tree in trees.items():
            if stem not in users and _test_imports(tree) & users:
                users.add(stem)
                changed = True
    found = {f"tests/{stem}.py" for stem in users}
    for conftest in TESTS.glob("*/conftest.py"):
        if _is_root(_parse(conftest)):
            found |= {
                p.relative_to(REPO_ROOT).as_posix()
                for p in conftest.parent.rglob("test_*.py")
            }
    return found


def test_the_listed_modules_are_exactly_the_ones_that_reach_the_shared_postgres():
    computed = _computed_set()

    missing = sorted(computed - SHARED_POSTGRES_MODULES)
    stale = sorted(SHARED_POSTGRES_MODULES - computed)
    assert not missing, f"reach the shared Postgres but are not grouped: {missing}"
    assert not stale, f"grouped but no longer reach the shared Postgres: {stale}"
    assert computed, "the computation found nothing: the detector is broken"


def test_every_listed_module_exists():
    for relative in SHARED_POSTGRES_MODULES:
        assert (REPO_ROOT / relative).is_file(), relative


class _FakeItem:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def test_a_collected_item_of_a_listed_module_gets_the_group_marker():
    """The marker is attached at collection time, which is what xdist reads to
    place the group. Neuter: drop the body of ``pytest_itemcollected`` -> RED."""
    from tests.conftest import pytest_itemcollected

    item = _FakeItem(REPO_ROOT / "tests" / "test_summariser_bypassrls.py")
    pytest_itemcollected(item)  # type: ignore[arg-type]

    assert len(item.markers) == 1
    marker = item.markers[0]
    assert marker.name == "xdist_group"  # type: ignore[attr-defined]
    assert marker.args == (XDIST_GROUP,)  # type: ignore[attr-defined]


def test_a_collected_item_of_an_unlisted_module_gets_no_marker():
    from tests.conftest import pytest_itemcollected

    item = _FakeItem(REPO_ROOT / "tests" / "test_check_collected_vs_junit.py")
    pytest_itemcollected(item)  # type: ignore[arg-type]

    assert item.markers == []


def test_a_path_outside_the_repo_is_ignored():
    from tests.conftest import pytest_itemcollected

    item = _FakeItem(Path("/nonexistent/elsewhere/test_x.py"))
    pytest_itemcollected(item)  # type: ignore[arg-type]

    assert item.markers == []
