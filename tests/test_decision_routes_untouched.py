"""#459 WU-459-1 T12: ``/v1`` untouched, and no route calls the client yet.

The git-diff instrument FAILS CLOSED: if the merge base with ``origin/main``
cannot be resolved the test FAILS (CI checks out with full history for this
reason). There is no override. The static "no route imports the package"
check always runs as a second assertion.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROUTES = REPO / "src/audittrace/routes"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def routes_diff_is_empty(repo: Path) -> None:
    """Assert the routes diff against the merge base is empty (fail closed)."""
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    assert base.returncode == 0, (
        "cannot resolve the merge base with origin/main (shallow checkout?): "
        "fetch full history"
    )
    diff = _git(
        repo, "diff", base.stdout.strip(), "HEAD", "--", "src/audittrace/routes"
    )
    assert diff.returncode == 0
    assert diff.stdout == ""


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


def test_t12_routes_diff_against_main_merge_base_is_empty() -> None:
    routes_diff_is_empty(REPO)


# ---- the instrument itself, on throw-away repositories ----------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / "src/audittrace/routes").mkdir(parents=True)
    (repo / "src/audittrace/routes/chat.py").write_text("x = 1\n")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-q", "-m", "base"],
    ):
        assert _git(repo, *cmd).returncode == 0
    return repo


def _make_origin_main(repo: Path) -> None:
    assert _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD").returncode == 0


def test_instrument_fails_closed_when_origin_main_is_unresolvable(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)  # no origin/main: the shallow-CI shape
    with pytest.raises(AssertionError, match="cannot resolve the merge base"):
        routes_diff_is_empty(repo)


def test_instrument_passes_on_an_untouched_routes_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _make_origin_main(repo)
    (repo / "other.txt").write_text("y\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "elsewhere")
    routes_diff_is_empty(repo)  # no exception: diff empty


def test_instrument_goes_red_when_a_route_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _make_origin_main(repo)
    (repo / "src/audittrace/routes/chat.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "touch a route")
    with pytest.raises(AssertionError):
        routes_diff_is_empty(repo)
