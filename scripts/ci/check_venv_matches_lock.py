#!/usr/bin/env python3
"""G5 — fail closed unless the running interpreter's environment IS the lock.

DEP-LOCK-AT (ADR-065, spec gate condition C2). ``scripts/ci/run-tests.sh``
calls this before pytest, so a suite never runs on an environment that has
drifted from ``requirements-dev.txt`` (the 2026-10-18 failure: a venv with
opentelemetry-api 1.44.0 against ``>=1.45.0,<1.46.0``, fastapi 0.140.7 while
CI had 0.146.0).

Checks, all mandatory, no bypass flag and no environment variable:

1. every lock pin whose environment marker applies here is installed at
   exactly that version;
2. everything installed that the lock does not name is in ``ALLOWED_EXTRAS``
   (the editable project and the VCS package, which pip cannot hash);
3. the project is the editable install at the ``pyproject.toml`` version;
4. the VCS package's ``direct_url.json`` ``commit_id`` equals the SHA pinned in
   ``pyproject.toml``;
5. the interpreter's major.minor equals the lock header's ``--python-version``;
6. no distribution name resolves to MORE THAN ONE location on ``sys.path``
   (review B2: a stale, gitignored ``src/audittrace_ai.egg-info`` reachable
   through the editable ``.pth`` shadows the venv's dist-info). The refusal
   NAMES every extra path and prints the exact ``rm -rf`` command.

Recovery path (also in the error text): ``rm -rf .venv && make venv``
(``make venv`` also removes ``src/*.egg-info``, scoped to that glob).

Falsifiable by tests/test_check_venv_matches_lock.py: each check has a test
that goes RED when its branch is removed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.markers import InvalidMarker, Marker
from packaging.utils import canonicalize_name

#: Distributions legitimately installed OUTSIDE the hashed lock.
ALLOWED_EXTRAS = frozenset({"audittrace-ai", "audittrace-object-storage"})
VCS_PACKAGE = "audittrace-object-storage"
PROJECT = "audittrace-ai"

RECOVERY = "Recovery: rm -rf .venv && make venv   (rebuilds from the hashed lock)."

_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==(?P<ver>[^\s;\\]+)"
)
_HEADER_PYVER = re.compile(r"--python-version\s+(\d+\.\d+)")
_VCS_SHA = re.compile(r"audittrace-object-storage\.git@([0-9a-f]{40})\"")


def parse_lock(text: str) -> tuple[dict[str, tuple[str, str | None]], str | None]:
    """Return ({canonical-name: (version, marker-or-None)}, header python version)."""
    pins: dict[str, tuple[str, str | None]] = {}
    for line in text.splitlines():
        m = _PIN.match(line)
        if not m:
            continue
        marker: str | None = None
        if ";" in line:
            marker = line.split(";", 1)[1].rstrip(" \\").strip() or None
        pins[canonicalize_name(m.group("name"))] = (m.group("ver"), marker)
    header = _HEADER_PYVER.search(text)
    return pins, (header.group(1) if header else None)


def marker_applies(marker: str | None) -> bool:
    """True when the pin's environment marker holds for this interpreter."""
    if marker is None:
        return True
    try:
        return Marker(marker).evaluate()
    except InvalidMarker:
        # An unparseable marker cannot be proven irrelevant: treat as applying
        # (fail closed — the pin must then be installed).
        return True


def _dist_path(dist: metadata.Distribution) -> str:
    """Where this distribution's metadata lives (best effort, for messages)."""
    path = getattr(dist, "_path", None)
    return str(path if path is not None else dist.locate_file(""))


def installed_distributions() -> dict[str, metadata.Distribution]:
    """canonical name -> the distribution the RUNTIME resolves.

    ``importlib.metadata.version()`` returns the FIRST match on ``sys.path``,
    so the first one is kept (it used to keep the last, which let a stale
    egg-info beyond site-packages impersonate the install — review B2).
    """
    out: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            out.setdefault(canonicalize_name(name), dist)
    return out


def duplicate_locations() -> dict[str, list[str]]:
    """canonical name -> metadata paths, only for names found in 2+ places."""
    seen: dict[str, list[str]] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            seen.setdefault(canonicalize_name(name), []).append(_dist_path(dist))
    return {n: paths for n, paths in seen.items() if len(set(paths)) > 1}


def vcs_commit(dist: metadata.Distribution) -> str | None:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return str(data.get("vcs_info", {}).get("commit_id") or "") or None


def is_editable(dist: metadata.Distribution) -> bool:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except json.JSONDecodeError:
        return False


def check(
    lock_text: str,
    pyproject: Mapping[str, Any],
    pyproject_text: str,
    installed: Mapping[str, metadata.Distribution],
    python_version: tuple[int, int],
    duplicates: Mapping[str, list[str]] | None = None,
) -> list[str]:
    """Return the list of violations (empty == the environment is the lock)."""
    pins, header_pyver = parse_lock(lock_text)
    problems: list[str] = []

    for name, paths in sorted((duplicates or {}).items()):
        used, shadows = paths[0], sorted(set(paths[1:]) - {paths[0]})
        problems.append(
            f"{name} resolves to {len(set(paths))} locations: {used} (used by the "
            f"runtime) and the shadowing/stale {', '.join(shadows)} — remove it: "
            f"rm -rf {' '.join(shadows)}"
        )

    if header_pyver is None:
        problems.append("lock header has no --python-version (not a uv-compiled lock?)")
    elif header_pyver != f"{python_version[0]}.{python_version[1]}":
        problems.append(
            f"interpreter is {python_version[0]}.{python_version[1]} but the lock "
            f"was compiled for {header_pyver}"
        )

    applicable = {n: v for n, (v, marker) in pins.items() if marker_applies(marker)}
    for name, version in sorted(applicable.items()):
        dist = installed.get(name)
        if dist is None:
            problems.append(f"{name}=={version} is in the lock but NOT installed")
        elif dist.version != version:
            problems.append(f"{name}: installed {dist.version}, lock pins {version}")

    for name in sorted(set(installed) - set(applicable)):
        if name not in ALLOWED_EXTRAS:
            problems.append(
                f"{name}=={installed[name].version} is installed but NOT in the lock"
            )

    want_version = (pyproject.get("project") or {}).get("version")
    proj_dist = installed.get(PROJECT)
    if proj_dist is None:
        problems.append(f"{PROJECT} is not installed (run make venv)")
    else:
        if proj_dist.version != want_version:
            problems.append(
                f"{PROJECT}: installed {proj_dist.version}, pyproject says {want_version}"
            )
        if not is_editable(proj_dist):
            problems.append(f"{PROJECT} is not an editable install (-e .)")

    sha_match = _VCS_SHA.search(pyproject_text)
    vcs_dist = installed.get(VCS_PACKAGE)
    if sha_match is None:
        problems.append(
            "pyproject.toml has no 40-hex audittrace-object-storage commit SHA"
        )
    elif vcs_dist is None:
        problems.append(f"{VCS_PACKAGE} is not installed")
    elif vcs_commit(vcs_dist) != sha_match.group(1):
        problems.append(
            f"{VCS_PACKAGE}: installed commit {vcs_commit(vcs_dist)!r}, "
            f"pyproject pins {sha_match.group(1)}"
        )
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "lock", type=Path, help="the hashed lock, e.g. requirements-dev.txt"
    )
    parser.add_argument(
        "--pyproject", type=Path, default=Path("pyproject.toml"), help=argparse.SUPPRESS
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    lock_text = args.lock.read_text(encoding="utf-8")
    pyproject_text = args.pyproject.read_text(encoding="utf-8")
    problems = check(
        lock_text,
        tomllib.loads(pyproject_text),
        pyproject_text,
        installed_distributions(),
        (sys.version_info.major, sys.version_info.minor),
        duplicate_locations(),
    )
    if problems:
        print(
            f"❌ check-venv-matches-lock: this environment is NOT {args.lock} "
            f"({len(problems)} problem(s)):",
            file=sys.stderr,
        )
        for p in problems[:40]:
            print(f"   - {p}", file=sys.stderr)
        if len(problems) > 40:
            print(f"   ... and {len(problems) - 40} more", file=sys.stderr)
        print(f"   {RECOVERY}", file=sys.stderr)
        return 1
    print(
        f"check-venv-matches-lock: environment == {args.lock} ({len(_pins(lock_text))} pins)"
    )
    return 0


def _pins(lock_text: str) -> dict[str, tuple[str, str | None]]:
    return parse_lock(lock_text)[0]


if __name__ == "__main__":
    sys.exit(main())
