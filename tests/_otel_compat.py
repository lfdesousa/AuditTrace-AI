"""Shared checkers for the OTel instrumentor-compatibility guards (G2/G2b).

Kept as plain functions that RETURN findings (never assert) so the guard tests
and the neuter tests drive the very same code: a neuter that makes a checker
return an empty list must turn the guard RED, which is the whole point.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import pathlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opentelemetry.instrumentation import dependencies as otel_deps
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUP = "opentelemetry_instrumentor"
RECOVERY = (
    "Recovery: this environment is stale or out of range. Pre-lock, rebuild it "
    "with `pip install -e .[dev]` in that venv; in the lock era use `make venv`."
)


def product_requirement_names(dist: str = "audittrace-ai") -> set[str]:
    """Canonical names of the product's own (non-extra) dependencies, read from
    the installed distribution's metadata, never from a hand list."""
    names: set[str] = set()
    for line in md.requires(dist) or []:
        req = Requirement(line)
        if req.marker is None or req.marker.evaluate({"extra": ""}):
            names.add(canonicalize_name(req.name))
    return names


def _conflict_library_names(conflict: Any) -> set[str]:
    reqs = [conflict.required] if conflict.required else []
    reqs += list(conflict.required_any or [])
    return {canonicalize_name(Requirement(r).name) for r in reqs}


def _is_version_conflict(conflict: Any) -> bool:
    return bool(conflict.found or conflict.found_any)


def installed_instrumentors() -> dict[str, Any]:
    """entry-point name -> instrumentor instance, for the running interpreter."""
    return {ep.name: ep.load()() for ep in md.entry_points(group=GROUP)}


def src_instrumentor_classes(src: pathlib.Path | None = None) -> set[str]:
    """Class names ending in ``Instrumentor`` that ``src/`` actually names."""
    found: set[str] = set()
    for path in (src or REPO_ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id.endswith("Instrumentor"):
                found.add(node.id)
            elif isinstance(node, ast.ImportFrom):
                found.update(
                    a.name for a in node.names if a.name.endswith("Instrumentor")
                )
    return found


def compat_findings(
    instrumentors: dict[str, Any],
    product_names: set[str],
    product_classes: Iterable[str],
) -> list[str]:
    """Every reason G2 is RED for ``instrumentors`` (empty list == GREEN).

    * a version conflict (library installed, outside the declared range) is
      always RED;
    * an ABSENT library is allowed only when it is not a product dependency
      (C1: ``httpx2`` is pulled in transitively and never instrumented);
    * positive control: every instrumentor class the product names resolves to
      an installed entry point and is conflict-free (including absence).
    """
    findings: list[str] = []
    get_conflicts = otel_deps.get_dependency_conflicts
    for ep_name, inst in sorted(instrumentors.items()):
        conflict = get_conflicts(inst.instrumentation_dependencies())
        if conflict is None:
            continue
        if _is_version_conflict(conflict):
            findings.append(f"{ep_name}: {conflict}")
        elif _conflict_library_names(conflict) & product_names:
            findings.append(
                f"{ep_name}: product dependency absent ({conflict.required or conflict.required_any})"
            )
    by_class = {type(i).__name__: n for n, i in instrumentors.items()}
    classes = set(product_classes)
    if not classes:
        findings.append("positive control: src/ names no *Instrumentor class")
    for cls in sorted(classes):
        if cls not in by_class:
            findings.append(f"positive control: {cls} has no installed entry point")
            continue
        inst = instrumentors[by_class[cls]]
        if get_conflicts(inst.instrumentation_dependencies()) is not None:
            findings.append(f"positive control: {cls} is not conflict-free")
    return findings


# ----------------------------------------------------------------- pin side --
@dataclass(frozen=True)
class Bounds:
    lo: Version | None
    lo_inc: bool
    hi: Version | None
    hi_inc: bool


def _bump(v: Version) -> Version:
    rel = list(v.release)
    if len(rel) == 1:
        return Version(str(rel[0] + 1))
    return Version(".".join(str(x) for x in [*rel[:-2], rel[-2] + 1]))


def bounds(spec: SpecifierSet) -> Bounds:
    lo: Version | None = None
    lo_inc = True
    hi: Version | None = None
    hi_inc = True
    for s in spec:
        v = Version(s.version)
        if s.operator in (">=", ">", "==", "~="):
            inc = s.operator != ">"
            if lo is None or v > lo or (v == lo and not inc):
                lo, lo_inc = v, inc
        if s.operator in ("<=", "<", "=="):
            inc = s.operator != "<"
            if hi is None or v < hi or (v == hi and not inc):
                hi, hi_inc = v, inc
        if s.operator == "~=":
            top = _bump(v)
            if hi is None or top < hi or (top == hi and hi_inc):
                hi, hi_inc = top, False
    return Bounds(lo, lo_inc, hi, hi_inc)


def spec_inside(pin: SpecifierSet, rng: SpecifierSet) -> bool:
    """True iff every version ``pin`` admits is inside ``rng`` (C8: the whole
    specifier set, not just the lower bound)."""
    p, r = bounds(pin), bounds(rng)
    if r.lo is not None:
        if p.lo is None or p.lo < r.lo:
            return False
        if p.lo == r.lo and p.lo_inc and not r.lo_inc:
            return False
    if r.hi is not None:
        if p.hi is None or p.hi > r.hi:
            return False
        if p.hi == r.hi and p.hi_inc and not r.hi_inc:
            return False
    return True


def pin_findings(
    instrumentors: dict[str, Any], pyproject_text: str
) -> tuple[list[str], set[str]]:
    """(findings, names checked) for the direct pins in ``pyproject_text``."""
    pins: dict[str, SpecifierSet] = {}
    for line in tomllib.loads(pyproject_text)["project"]["dependencies"]:
        req = Requirement(line)
        pins[canonicalize_name(req.name)] = req.specifier
    findings: list[str] = []
    checked: set[str] = set()
    for ep_name, inst in sorted(instrumentors.items()):
        for dep in inst.instrumentation_dependencies():
            req = Requirement(dep)
            key = canonicalize_name(req.name)
            if key not in pins:
                continue
            checked.add(key)
            if not spec_inside(pins[key], req.specifier):
                findings.append(
                    f"{ep_name}: pin `{req.name}{pins[key]}` is not inside the "
                    f"instrumentor's range `{req.specifier}`"
                )
    return findings, checked
