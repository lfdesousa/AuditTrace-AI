"""G6 — every Python-version pin site names ONE Python (DEP-LOCK-AT, ADR-065).

The hashed locks are valid for exactly one interpreter line, and the repo
spells that line in 15 places (spec §4.3; CLAUDE.md's "8" is stale, measured).
Dependabot only ever touches the Dockerfile, so drift between sites is the
known failure mode (`project_python_version_pin_sites`). This test reads ALL
of them, asserts that the per-file site COUNT is what the spec enumerated (so
deleting a site is also RED, not silently "all remaining sites agree"), and
that each lock header's ``--python-version`` equals it.

Neuter N9: change ``Dockerfile`` line 2 to ``python:3.13-slim`` -> RED.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

LOCKS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-gate.txt",
    "bff/requirements.txt",
    "images/llm-stub/requirements.txt",
)

#: file -> number of Python pin sites in it (spec §4.3 enumerates these).
EXPECTED_SITES = {
    "Dockerfile": 2,
    "bff/Dockerfile": 1,
    "images/llm-stub/Dockerfile": 1,
    "pyproject.toml": 4,
    ".python-version": 1,
    ".github/workflows/ci.yml": 4,
    ".github/workflows/integration-content-control.yml": 1,
    ".github/workflows/test-count-badge.yml": 1,
}


def _walk_python_versions(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "python-version":
                found.append(str(value))
            else:
                found.extend(_walk_python_versions(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_python_versions(item))
    return found


def collect_sites(root: Path) -> dict[str, list[str]]:
    """file -> list of major.minor strings, one per pin site in that file."""
    sites: dict[str, list[str]] = {}
    for rel in ("Dockerfile", "bff/Dockerfile", "images/llm-stub/Dockerfile"):
        text = (root / rel).read_text()
        sites[rel] = re.findall(r"^FROM python:(\d+\.\d+)-slim", text, re.M)

    py = tomllib.loads((root / "pyproject.toml").read_text())
    requires = re.fullmatch(r">=(\d+\.\d+)", py["project"]["requires-python"])
    ruff = re.fullmatch(r"py(\d)(\d+)", py["tool"]["ruff"]["target-version"])
    black = re.fullmatch(r"py(\d)(\d+)", py["tool"]["black"]["target-version"][0])
    assert requires and ruff and black, "unparseable pyproject python pin"
    sites["pyproject.toml"] = [
        requires.group(1),
        f"{ruff.group(1)}.{ruff.group(2)}",
        py["tool"]["mypy"]["python_version"],
        f"{black.group(1)}.{black.group(2)}",
    ]

    sites[".python-version"] = [(root / ".python-version").read_text().strip()]
    for rel in (
        ".github/workflows/ci.yml",
        ".github/workflows/integration-content-control.yml",
        ".github/workflows/test-count-badge.yml",
    ):
        sites[rel] = _walk_python_versions(yaml.safe_load((root / rel).read_text()))
    return sites


def lock_header_versions(root: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for rel in LOCKS:
        m = re.search(r"--python-version\s+(\d+\.\d+)", (root / rel).read_text())
        out[rel] = m.group(1) if m else None
    return out


def violations(
    sites: dict[str, list[str]], headers: dict[str, str | None]
) -> list[str]:
    bad: list[str] = []
    for rel, want in EXPECTED_SITES.items():
        if len(sites.get(rel, [])) != want:
            bad.append(f"{rel}: expected {want} pin site(s), found {sites.get(rel)}")
    values = {v for found in sites.values() for v in found}
    values |= {v for v in headers.values() if v is not None}
    if len(values) != 1:
        bad.append(f"pin sites disagree: {sorted(values)}")
    for rel, v in headers.items():
        if v is None:
            bad.append(f"{rel}: lock header has no --python-version")
    return bad


def test_all_pin_sites_agree() -> None:
    assert violations(collect_sites(ROOT), lock_header_versions(ROOT)) == []


def test_there_are_exactly_fifteen_sites_plus_five_lock_headers() -> None:
    assert sum(EXPECTED_SITES.values()) == 15
    assert len(LOCKS) == 5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s, h: s["Dockerfile"].__setitem__(0, "3.13"),  # N9
        lambda s, h: s["pyproject.toml"].__setitem__(1, "3.11"),
        lambda s, h: h.__setitem__("requirements.txt", "3.13"),
        lambda s, h: h.__setitem__("bff/requirements.txt", None),
        lambda s, h: s[".github/workflows/ci.yml"].pop(),  # a site deleted
    ],
    ids=[
        "dockerfile-3.13",
        "ruff-3.11",
        "lock-header-3.13",
        "lock-header-missing",
        "site-deleted",
    ],
)
def test_the_guard_flags_drift_in_any_site(mutate: Any) -> None:
    sites = {k: list(v) for k, v in collect_sites(ROOT).items()}
    headers = dict(lock_header_versions(ROOT))
    mutate(sites, headers)
    assert violations(sites, headers)
