"""G3 — every direct pin is in its hashed lock, at that version, with hashes.

DEP-LOCK-AT (ADR-065), offline unit test, spec gate conditions C1 + C9.

* every ``[project].dependencies`` pin is ``==`` (the only non-``==`` spec is the
  VCS requirement, pinned to a 40-hex commit SHA) and is in BOTH
  ``requirements.txt`` and ``requirements-dev.txt`` at that version;
* every ``[project.optional-dependencies].dev`` pin is in ``requirements-dev.txt``;
* ``[build-system].requires`` == ``requirements-build.in`` and both are in BOTH
  locks (C1: ``--no-build-isolation`` builds use the hashed setuptools/wheel);
* the ``gate`` extra == ``requirements-gate.in`` and is in
  ``requirements-gate.txt`` (C7);
* ``bff/requirements.in`` and ``images/llm-stub/requirements.in`` pins are in
  their locks;
* a pin is satisfied only by a ``--hash`` line inside ITS OWN block (block
  slicing, so another package's hashes cannot vouch for it — the harness F2
  lesson).

Neuters (each applied individually): N3 strip one package's ``--hash`` lines ->
RED; N4 change a direct pin's version in the lock only -> RED. They are the
``test_the_guard_*`` cases below, run against mutated copies of the real files.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
_BLOCK_HEAD = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


def lock_blocks(text: str) -> dict[str, tuple[str, int]]:
    """canonical name -> (version, number of --hash lines in ITS OWN block)."""
    blocks: dict[str, tuple[str, int]] = {}
    current: str | None = None
    for line in text.splitlines():
        head = _BLOCK_HEAD.match(line)
        if head:
            current = canonicalize_name(head.group(1))
            blocks[current] = (head.group(2), 0)
        elif current is not None and line.lstrip().startswith("--hash=sha256:"):
            version, n = blocks[current]
            blocks[current] = (version, n + 1)
        elif line and not line[0].isspace() and not line.startswith("#"):
            current = None
    return blocks


def exact_pins(specs: list[str]) -> dict[str, str]:
    """Parse requirement strings; return the ``==`` pins (name -> version)."""
    out: dict[str, str] = {}
    for spec in specs:
        if "@" in spec.split(";")[0]:
            continue  # the VCS requirement; covered by test_the_vcs_requirement_*
        req = Requirement(spec)
        pins = [s.version for s in req.specifier if s.operator == "=="]
        if pins:
            out[canonicalize_name(req.name)] = pins[0]
    return out


def in_file_pins(path: Path) -> dict[str, str]:
    """Pins from a ``.in`` file (comments and blank lines ignored)."""
    specs = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return exact_pins(specs)


def missing(pins: dict[str, str], lock_text: str, label: str) -> list[str]:
    blocks = lock_blocks(lock_text)
    bad: list[str] = []
    for name, version in sorted(pins.items()):
        if name not in blocks:
            bad.append(f"{label}: {name}=={version} absent from the lock")
            continue
        locked, hashes = blocks[name]
        if locked != version:
            bad.append(f"{label}: {name} pinned {version} but the lock has {locked}")
        if hashes == 0:
            bad.append(f"{label}: {name}=={locked} has no --hash in its own block")
    return bad


class Inputs:
    """The real repo files, loadable as text so tests can mutate copies."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.pyproject = tomllib.loads((root / "pyproject.toml").read_text())
        self.pyproject_text = (root / "pyproject.toml").read_text()
        self.locks = {
            rel: (root / rel).read_text()
            for rel in (
                "requirements.txt",
                "requirements-dev.txt",
                "requirements-gate.txt",
                "bff/requirements.txt",
                "images/llm-stub/requirements.txt",
            )
        }


def all_violations(inp: Inputs) -> list[str]:
    root = inp.root
    project = inp.pyproject["project"]
    runtime_specs: list[str] = project["dependencies"]
    dev_specs: list[str] = project["optional-dependencies"]["dev"]
    gate_specs: list[str] = project["optional-dependencies"]["gate"]
    build_specs: list[str] = inp.pyproject["build-system"]["requires"]

    bad: list[str] = []
    # every non-VCS direct spec is exact: no range survives anywhere.
    for label, specs in (
        ("runtime", runtime_specs),
        ("dev", dev_specs),
        ("gate", gate_specs),
        ("build-system", build_specs),
    ):
        for spec in specs:
            if "@" in spec.split(";")[0]:
                continue
            if canonicalize_name(Requirement(spec).name) not in exact_pins([spec]):
                bad.append(f"{label}: {spec!r} is not an exact == pin")

    runtime = exact_pins(runtime_specs)
    dev = exact_pins(dev_specs)
    build = exact_pins(build_specs)
    locks = inp.locks
    for lock in ("requirements.txt", "requirements-dev.txt"):
        bad += missing(runtime, locks[lock], f"runtime pin vs {lock}")
        bad += missing(build, locks[lock], f"build-system pin vs {lock}")
    bad += missing(
        dev, locks["requirements-dev.txt"], "dev pin vs requirements-dev.txt"
    )

    build_in = in_file_pins(root / "requirements-build.in")
    if build_in != build:
        bad.append(
            f"requirements-build.in {build_in} != [build-system].requires {build}"
        )

    gate_in = in_file_pins(root / "requirements-gate.in")
    gate = exact_pins(gate_specs)
    if any(gate_in.get(n) != v for n, v in gate.items()):
        bad.append(f"requirements-gate.in {gate_in} lacks pyproject gate pins {gate}")
    bad += missing(
        gate_in, locks["requirements-gate.txt"], "requirements-gate.in vs lock"
    )

    bad += missing(
        in_file_pins(root / "bff/requirements.in"),
        locks["bff/requirements.txt"],
        "bff/requirements.in vs lock",
    )
    bad += missing(
        in_file_pins(root / "images/llm-stub/requirements.in"),
        locks["images/llm-stub/requirements.txt"],
        "llm-stub requirements.in vs lock",
    )
    return bad


def test_every_direct_pin_is_in_the_lock_with_a_hash() -> None:
    assert all_violations(Inputs()) == []


def test_the_vcs_requirement_is_a_full_commit_sha_and_not_in_any_lock() -> None:
    inp = Inputs()
    vcs = [s for s in inp.pyproject["project"]["dependencies"] if " @ git+" in s]
    assert len(vcs) == 1
    assert re.search(r"\.git@[0-9a-f]{40}$", vcs[0]), vcs[0]
    for rel, text in inp.locks.items():
        assert "audittrace-object-storage" not in lock_blocks(text), rel
        assert "git+" not in "".join(
            line for line in text.splitlines() if not line.startswith("#")
        ), rel


def test_the_locks_carry_pip_setuptools_wheel_and_uv_hashed() -> None:
    inp = Inputs()
    dev = lock_blocks(inp.locks["requirements-dev.txt"])
    for name in ("pip", "setuptools", "wheel", "uv"):
        assert name in dev and dev[name][1] > 0, name
    runtime = lock_blocks(inp.locks["requirements.txt"])
    for name in ("setuptools", "wheel"):
        assert name in runtime and runtime[name][1] > 0, name
    gate = lock_blocks(inp.locks["requirements-gate.txt"])
    assert "semgrep" in gate and gate["pip"][1] > 0


# ───────────────────────────── neuters (N3, N4) ─────────────────────────────


def _mutated(rel: str, fn: Callable[[str], str]) -> Inputs:
    inp = Inputs()
    inp.locks[rel] = fn(inp.locks[rel])
    return inp


def _strip_hashes_of(name: str) -> Callable[[str], str]:
    def apply(text: str) -> str:
        out: list[str] = []
        inside = False
        for line in text.splitlines():
            head = _BLOCK_HEAD.match(line)
            if head:
                inside = canonicalize_name(head.group(1)) == name
            if inside and line.lstrip().startswith("--hash="):
                continue
            out.append(line)
        return "\n".join(out) + "\n"

    return apply


@pytest.mark.parametrize("rel", ["requirements.txt", "requirements-dev.txt"])
def test_the_guard_flags_a_package_whose_hashes_were_stripped_n3(rel: str) -> None:
    inp = _mutated(rel, _strip_hashes_of("fastapi"))
    assert any("fastapi" in v and "no --hash" in v for v in all_violations(inp))


def test_the_guard_flags_a_lock_only_version_change_n4() -> None:
    inp = _mutated(
        "requirements-dev.txt",
        lambda t: t.replace("\nfastapi==0.146.0 ", "\nfastapi==0.145.0 ", 1),
    )
    assert any(
        "fastapi pinned 0.146.0 but the lock has 0.145.0" in v
        for v in all_violations(inp)
    )


def test_a_neighbours_hashes_do_not_vouch_for_a_pin() -> None:
    """Block slicing: with fastapi's own hashes gone, the lock still contains
    hundreds of other --hash lines; a whole-file `--hash` search would pass."""
    inp = _mutated("requirements.txt", _strip_hashes_of("fastapi"))
    assert "--hash=sha256:" in inp.locks["requirements.txt"]
    assert all_violations(inp)


def test_the_guard_flags_a_range_in_pyproject() -> None:
    inp = Inputs()
    inp.pyproject["project"]["dependencies"].append("rich>=13")
    assert any("not an exact == pin" in v for v in all_violations(inp))


def test_the_guard_flags_an_absent_pin() -> None:
    inp = Inputs()
    inp.pyproject["project"]["optional-dependencies"]["dev"].append("nonexistent==1.0")
    assert any(
        "nonexistent==1.0 absent from the lock" in v for v in all_violations(inp)
    )


def test_the_guard_flags_build_in_drift() -> None:
    inp = Inputs()
    inp.pyproject["build-system"]["requires"] = ["setuptools==83.0.0", "wheel==0.48.0"]
    assert any("requirements-build.in" in v for v in all_violations(inp))


def test_lock_blocks_ignores_comments_and_marker_lines() -> None:
    text = (
        "# header\n"
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
        "    --hash=sha256:aa \\\n"
        "    --hash=sha256:bb\n"
        "    # via pytest\n"
        "# trailing comment\n"
        "tqdm==4.0 \\\n"
        "    --hash=sha256:cc\n"
    )
    assert lock_blocks(text) == {"colorama": ("0.4.6", 2), "tqdm": ("4.0", 1)}
