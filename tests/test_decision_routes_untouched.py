"""#459 WU-459-1 T12: ``/v1`` untouched, and no route calls the client yet."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTES = REPO / "src/audittrace/routes"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def test_no_route_module_imports_the_decision_package() -> None:
    pattern = re.compile(r"audittrace\.services\.decision|services\s+import\s+decision")
    offenders = [
        str(p.relative_to(REPO))
        for p in ROUTES.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_t12_routes_diff_against_main_merge_base_is_empty() -> None:
    base = _git("merge-base", "HEAD", "origin/main")
    if base.returncode != 0:
        # Shallow CI checkout without origin/main: the static import check
        # above is the instrument that still runs; nothing else to diff.
        assert (ROUTES / "chat.py").is_file()
        return
    diff = _git("diff", base.stdout.strip(), "HEAD", "--", "src/audittrace/routes")
    assert diff.returncode == 0
    assert diff.stdout == ""
