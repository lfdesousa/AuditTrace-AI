#!/usr/bin/env python3
"""AC-6 / G5 for environments the suite does not run in: freeze == lock.

DEP-LOCK-AT WU-2 (ADR-065). ``check_venv_matches_lock.py`` (G5) inspects the
interpreter that runs it. The gate venv and the three first-party IMAGES
(memory-server, BFF, llm-stub) are other environments; their ground truth is
what the interpreter's own freeze listing (``freeze --all``) prints for them. This script compares that listing
with a lock, exactly, and fails closed:

* every lock pin whose environment marker applies is installed at that version;
* nothing is installed that the lock does not name, except ``--allow-extra``
  distributions (the project and the VCS package, which cannot be hashed);
* the VCS package, when named with ``--vcs-package``, is installed from a
  ``git+...@<40-hex>`` reference whose SHA equals the one pinned in
  ``pyproject.toml`` (a commit SHA is content-addressed);
* a freeze line that is not ``name==version`` or ``name @ url`` (an editable
  ``-e`` line, a malformed line) is itself a violation, never skipped.

Sources, no bypass flag and no environment variable:

* the lock: ``--lock PATH`` or ``--lock-rev REV:PATH`` (``git show REV:PATH``,
  the committed bytes, not the working tree: AC-6 compares an image with
  ``git show HEAD:<lock>``);
* the environment: ``--freeze FILE|-`` (the freeze listing of an image, see
  check-images-match-locks.sh) or ``--python PATH`` (runs its freeze listing);
* the marker environment: the host's by default; ``--marker-env FILE`` takes
  the target's ``default_environment()`` JSON, which is how an image (or an
  arm64 install) is judged by ITS markers and not the runner's.

Falsifiable by tests/test_check_freeze_matches_lock.py: each check has a test
that goes RED when its branch is removed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

try:
    from scripts.ci.check_venv_matches_lock import (
        InvalidMarker,
        Marker,
        canonicalize_name,
        parse_lock,
    )
except ModuleNotFoundError:  # run as ``python scripts/ci/check_freeze_matches_lock.py``
    from check_venv_matches_lock import (  # type: ignore[no-redef]
        InvalidMarker,
        Marker,
        canonicalize_name,
        parse_lock,
    )

_PINNED = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<ver>\S+)$")
_DIRECT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s+(?P<url>\S+)$")
_GIT_SHA = re.compile(r"^git\+\S+@(?P<sha>[0-9a-f]{40})$")
_PYPROJECT_SHA = re.compile(r"audittrace-object-storage\.git@([0-9a-f]{40})\"")


def applies(marker: str | None, env: Mapping[str, str] | None) -> bool:
    """True when the pin's marker holds in ``env`` (the host's when None).

    An unparseable marker cannot be proven irrelevant: it applies (fail closed).
    """
    if marker is None:
        return True
    try:
        return bool(Marker(marker).evaluate(dict(env) if env else None))
    except InvalidMarker:
        return True


def parse_freeze(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return (pinned {name: version}, direct {name: url}, unparseable lines)."""
    pinned: dict[str, str] = {}
    direct: dict[str, str] = {}
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if m := _PINNED.match(line):
            pinned[canonicalize_name(m.group("name"))] = m.group("ver")
        elif m := _DIRECT.match(line):
            direct[canonicalize_name(m.group("name"))] = m.group("url")
        else:
            bad.append(line)
    return pinned, direct, bad


def check(
    lock_text: str,
    freeze_text: str,
    *,
    allow_extra: Iterable[str] = (),
    allow_extra_pinned: Iterable[str] = (),
    vcs_package: str | None = None,
    pyproject_text: str | None = None,
    marker_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the violations (empty == the environment IS the lock)."""
    pins, _ = parse_lock(lock_text)
    wanted = {n: v for n, (v, mk) in pins.items() if applies(mk, marker_env)}
    pinned, direct, bad = parse_freeze(freeze_text)
    extras = {canonicalize_name(n) for n in allow_extra}
    pinned_extras = {canonicalize_name(n) for n in allow_extra_pinned}
    problems = [f"unparseable freeze line (fail closed): {line!r}" for line in bad]

    for name, version in sorted(wanted.items()):
        if name in pinned:
            if pinned[name] != version:
                problems.append(
                    f"{name}: installed {pinned[name]}, lock pins {version}"
                )
        elif name in direct:
            problems.append(
                f"{name}: installed as a direct reference ({direct[name]}), lock pins {version}"
            )
        else:
            problems.append(f"{name}=={version} is in the lock but NOT installed")

    for name in sorted(pinned_extras & set(direct)):
        problems.append(
            f"{name} @ {direct[name]} is a direct reference; only {name}==<version> is allowed"
        )
    for name in sorted(
        (set(pinned) | set(direct)) - set(wanted) - extras - pinned_extras
    ):
        shown = f"=={pinned[name]}" if name in pinned else f" @ {direct[name]}"
        problems.append(f"{name}{shown} is installed but NOT in the lock")

    if vcs_package is not None:
        problems.extend(
            _check_vcs(canonicalize_name(vcs_package), direct, pyproject_text)
        )
    return problems


def _check_vcs(
    name: str, direct: Mapping[str, str], pyproject_text: str | None
) -> list[str]:
    sha = _PYPROJECT_SHA.search(pyproject_text or "")
    if sha is None:
        return ["pyproject.toml has no 40-hex audittrace-object-storage commit SHA"]
    url = direct.get(name)
    if url is None:
        return [f"{name} is not installed as a git reference"]
    ref = _GIT_SHA.match(url)
    if ref is None or ref.group("sha") != sha.group(1):
        return [f"{name}: installed {url!r}, pyproject pins commit {sha.group(1)}"]
    return []


def _lock_text(lock: Path | None, lock_rev: str | None) -> str:
    if lock_rev is not None:
        proc = subprocess.run(
            ["git", "show", lock_rev],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(
                f"❌ check-freeze-matches-lock: git show {lock_rev} failed: {proc.stderr.strip()}"
            )
        return proc.stdout
    assert lock is not None
    return lock.read_text(encoding="utf-8")


def _freeze_text(freeze: str | None, python: str | None) -> str:
    if python is not None:
        proc = subprocess.run(
            [python, "-m", "pip", "freeze", "--all"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(
                f"❌ check-freeze-matches-lock: {python} freeze listing failed: {proc.stderr.strip()}"
            )
        return proc.stdout
    assert freeze is not None
    if freeze == "-":
        return sys.stdin.read()
    return Path(freeze).read_text(encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--lock", type=Path, help="the lock file in the working tree")
    src.add_argument(
        "--lock-rev", help="REV:PATH, read with `git show` (committed bytes)"
    )
    env = parser.add_mutually_exclusive_group(required=True)
    env.add_argument("--freeze", help="freeze --all listing, or - for stdin")
    env.add_argument("--python", help="interpreter whose freeze listing is checked")
    parser.add_argument(
        "--allow-extra",
        action="append",
        default=[],
        help="distribution allowed outside the lock",
    )
    parser.add_argument(
        "--allow-extra-pinned",
        action="append",
        default=[],
        help="distribution allowed outside the lock ONLY as name==version",
    )
    parser.add_argument(
        "--vcs-package",
        help="distribution that must be a git+...@<pyproject SHA> reference",
    )
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--marker-env",
        type=Path,
        help="JSON of the target's packaging default_environment()",
    )
    parser.add_argument(
        "--label", default="environment", help="name shown in the verdict line"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    lock_text = _lock_text(args.lock, args.lock_rev)
    marker_env = (
        json.loads(args.marker_env.read_text(encoding="utf-8"))
        if args.marker_env
        else None
    )
    pyproject_text = None
    if args.vcs_package:
        pyproject_text = args.pyproject.read_text(encoding="utf-8")
        tomllib.loads(pyproject_text)  # a malformed pyproject is an error, not a pass
    problems = check(
        lock_text,
        _freeze_text(args.freeze, args.python),
        allow_extra=args.allow_extra,
        allow_extra_pinned=args.allow_extra_pinned,
        vcs_package=args.vcs_package,
        pyproject_text=pyproject_text,
        marker_env=marker_env,
    )
    lock_name = args.lock or args.lock_rev
    if problems:
        print(
            f"❌ check-freeze-matches-lock: {args.label} is NOT {lock_name} ({len(problems)} problem(s)):",
            file=sys.stderr,
        )
        for p in problems[:40]:
            print(f"   - {p}", file=sys.stderr)
        if len(problems) > 40:
            print(f"   ... and {len(problems) - 40} more", file=sys.stderr)
        return 1
    n = len(
        [
            1
            for _, (_, mk) in parse_lock(lock_text)[0].items()
            if applies(mk, marker_env)
        ]
    )
    print(f"check-freeze-matches-lock: {args.label} == {lock_name} ({n} pins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
