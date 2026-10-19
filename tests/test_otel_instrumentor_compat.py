"""G2 / G2b - every registered OTel instrumentor is compatible with the
installed (G2) and the pinned (G2b) library versions.

SQLALCHEMY-SPANS, operator directive 2026-10-19 (telemetry fixes need proper
tests). The defect this class catches: sqlalchemy 2.1.x was installed while
``opentelemetry-instrumentation-sqlalchemy`` declares ``< 2.1.0``, so
``instrument()`` logged one line, returned ``None`` and PostgreSQL emitted zero
spans. Nothing in ``make test`` or CI noticed.

* G2  - an identity pin on the ENVIRONMENT: for every entry point in the
  ``opentelemetry_instrumentor`` group of the running interpreter,
  ``get_dependency_conflicts(instrumentation_dependencies())`` is ``None``.
  Never a hand-kept list. An ABSENT library is tolerated only when it is not a
  dependency of the installed ``audittrace-ai`` distribution (C1).
* G2b - a pin-side check on ``pyproject.toml``: the whole specifier set of each
  direct pin an instrumentor names sits inside that instrumentor's range (C8),
  so a Dependabot bump to ``2.1.x`` is RED before any install.

Neuters (each applied alone, recorded in the build record):
N4  an instrumentor declares an unsatisfiable range          -> G2 RED
N4b a product library reported absent (found=None)           -> G2 RED
N5  the positive-control family set removed                  -> G2 RED
N6  pin text edited to ``sqlalchemy==2.1.5``                 -> G2b RED
N6b pin text edited to ``sqlalchemy>=2.0.25,<3``             -> G2b RED
The neuter tests below prove each is caught; the source-level pin move is
recorded RED-before / GREEN-after in a fresh venv in the build record.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.instrumentation import dependencies as otel_deps
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from tests import _otel_compat as compat

PYPROJECT = compat.REPO_ROOT / "pyproject.toml"


def _installed() -> dict[str, Any]:
    return compat.installed_instrumentors()


def test_every_installed_instrumentor_is_compatible() -> None:
    findings = compat.compat_findings(
        _installed(),
        compat.product_requirement_names(),
        compat.src_instrumentor_classes(),
    )
    assert not findings, (
        "OTel instrumentor / library version mismatch (instrument() would "
        "silently attach nothing):\n  " + "\n  ".join(findings) + "\n" + compat.RECOVERY
    )


def test_positive_control_covers_the_six_product_families() -> None:
    assert compat.src_instrumentor_classes() >= {
        "FastAPIInstrumentor",
        "HTTPXClientInstrumentor",
        "LoggingInstrumentor",
        "RedisInstrumentor",
        "SQLAlchemyInstrumentor",
        "URLLib3Instrumentor",
    }


def test_absent_non_product_library_is_not_a_conflict() -> None:
    """httpx2 is installed transitively and never instrumented: tolerated."""
    assert "httpx2" not in compat.product_requirement_names()


# ----------------------------------------------------------------- neuters --
def test_n4_unsatisfiable_range_turns_g2_red(monkeypatch: pytest.MonkeyPatch) -> None:
    insts = _installed()
    monkeypatch.setattr(
        type(insts["sqlalchemy"]),
        "instrumentation_dependencies",
        lambda self: ("sqlalchemy < 1.0",),
    )
    findings = compat.compat_findings(
        insts,
        compat.product_requirement_names(),
        compat.src_instrumentor_classes(),
    )
    assert any(f.startswith("sqlalchemy:") for f in findings)


def test_n4b_product_library_reported_absent_turns_g2_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = otel_deps.get_dependency_conflicts

    def fake(deps: Any) -> Any:
        if any(str(d).lower().startswith("sqlalchemy") for d in deps):
            return otel_deps.DependencyConflict("sqlalchemy >= 1.0.0", None)
        return real(deps)

    monkeypatch.setattr(otel_deps, "get_dependency_conflicts", fake)
    findings = compat.compat_findings(
        _installed(),
        compat.product_requirement_names(),
        compat.src_instrumentor_classes(),
    )
    assert any("product dependency absent" in f for f in findings)


def test_n5_removed_positive_control_turns_g2_red() -> None:
    insts = _installed()
    insts.pop("sqlalchemy")
    findings = compat.compat_findings(
        insts,
        compat.product_requirement_names(),
        compat.src_instrumentor_classes(),
    )
    assert any(
        "SQLAlchemyInstrumentor has no installed entry point" in f for f in findings
    )
    assert compat.compat_findings(insts, set(), []) == [
        "positive control: src/ names no *Instrumentor class"
    ]


# ------------------------------------------------------------------- G2b ----
def test_pins_are_inside_every_instrumentor_range() -> None:
    findings, checked = compat.pin_findings(_installed(), PYPROJECT.read_text())
    assert {"sqlalchemy", "fastapi", "redis", "httpx"} <= checked
    assert not findings, "\n".join(findings) + "\n" + compat.RECOVERY


def _with_sqlalchemy_pin(spec: str) -> str:
    text = PYPROJECT.read_text()
    lines = [
        f'    "sqlalchemy[asyncio]{spec}",' if '"sqlalchemy[asyncio]' in line else line
        for line in text.splitlines()
    ]
    return "\n".join(lines)


@pytest.mark.parametrize("spec", ["==2.1.5", ">=2.0.25,<3", ">=2.1.1,<3"])
def test_n6_n6b_out_of_range_pins_turn_g2b_red(spec: str) -> None:
    findings, _ = compat.pin_findings(_installed(), _with_sqlalchemy_pin(spec))
    assert any("sqlalchemy" in f for f in findings), spec


@pytest.mark.parametrize("spec", ["==2.0.55", ">=2.0.25,<2.1"])
def test_in_range_pins_are_green(spec: str) -> None:
    findings, _ = compat.pin_findings(_installed(), _with_sqlalchemy_pin(spec))
    assert not any("sqlalchemy" in f for f in findings), findings


# -------------------------------------------------------- the interval model -
@pytest.mark.parametrize(
    ("pin", "rng", "inside"),
    [
        (">=2.0.25,<2.1", ">=1.0.0,<2.1.0", True),
        ("==2.0.55", ">=1.0.0,<2.1.0", True),
        ("==2.1.0", ">=1.0.0,<2.1.0", False),
        (">=2.0.25,<3", ">=1.0.0,<2.1.0", False),
        (">=2.0.25", ">=1.0.0,<2.1.0", False),
        (">=0.5,<2", ">=1.0.0,<2.1.0", False),
        (">=0.109,<1", "~=0.92", True),
        (">=0.109", "~=0.92", False),
        (">=0.25.0", ">=0.18.0", True),
        (">0.18.0", ">=0.18.0", True),
        (">=0.18.0", ">0.18.0", False),
        ("<=2.0.5", "<=2.0.5", True),
        ("<=2.0.5", "<2.0.5", False),
        ("<2.0.5", "<=2.0.5", True),
        ("~=1.4.2", "<1.5", True),
    ],
)
def test_spec_inside_interval_model(pin: str, rng: str, inside: bool) -> None:
    assert compat.spec_inside(SpecifierSet(pin), SpecifierSet(rng)) is inside


def test_bump_single_segment() -> None:
    assert str(compat._bump(Version("3"))) == "4"
