"""Parse a pytest junit-xml file into per-test outcomes (SPEC v3 §4).

Outcomes come from the junit file, never the summary line. This module
reproduces pytest 9.1.1's own node-id -> ``(classname, name)`` derivation
(``_pytest/junitxml.py:445-453`` ``mangle_test_address`` + the classname
join at ``:114-121``) so a mapped node id can be looked up directly against
the junit XML's own ``classname``/``name`` attributes -- forward-computed
from a node id we already control, never inverted from the XML.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

#: A failed test is *assertion-shaped* iff its failure_type is one of these
#: (SF-A). Anything else (an ``IntegrityError``, a bare ``RuntimeError``
#: escaping a test body, ...) is a call-phase exception, not a proven guard.
ASSERTION_SHAPED_TYPES = frozenset({"AssertionError", "Failed"})


def mangle_node_id(node_id: str) -> tuple[str, str]:
    """Reproduce ``_pytest.junitxml.mangle_test_address`` + the classname
    join exactly, so we can look a node id up in the parsed XML without
    ever inverting pytest's own mangling."""
    path, bracket, params = node_id.partition("[")
    parts = path.split("::")
    parts[0] = parts[0].replace("/", ".")
    parts[0] = re.sub(r"\.py$", "", parts[0])
    parts[-1] = parts[-1] + bracket + params
    classname = ".".join(parts[:-1])
    name = parts[-1]
    return classname, name


@dataclass(frozen=True)
class JunitTestcase:
    outcome: str  # "passed" | "failed" | "error" | "skipped"
    failure_msg: str | None = None
    failure_type: str | None = None

    @property
    def assertion_shaped(self) -> bool:
        return self.failure_type in ASSERTION_SHAPED_TYPES


@dataclass(frozen=True)
class JunitResult:
    tests_collected: int | None
    parse_failed: bool
    outcomes: dict[str, JunitTestcase] = field(default_factory=dict)


@dataclass(frozen=True)
class FullJunitResult:
    """Every testcase in the suite, not just the mapped ids -- the sampled
    full-scope drift pass (§5) and ``arbitrate`` (§12) both need to see
    failures OUTSIDE the neuter's own mapped tests, which :func:`parse_junit`
    deliberately can't show (it only looks up the ids it's given)."""

    tests_collected: int | None
    parse_failed: bool
    by_key: dict[tuple[str, str], JunitTestcase] = field(default_factory=dict)


def _failure_type(failure_msg: str) -> str:
    """``failure_type := AssertionError if failure_msg starts with 'assert '``
    ``else the text before the first ':'`` (§4)."""
    first_line = failure_msg.splitlines()[0] if failure_msg else ""
    if first_line.startswith("assert "):
        return "AssertionError"
    return first_line.split(":", 1)[0].strip()


def _testcase_outcome(testcase: ET.Element) -> JunitTestcase:
    failure = testcase.find("failure")
    error = testcase.find("error")
    skipped = testcase.find("skipped")
    if failure is not None:
        msg = failure.get("message") or (failure.text or "")
        ftype = _failure_type(msg)
        return JunitTestcase("failed", msg.splitlines()[0] if msg else "", ftype)
    if error is not None:
        msg = error.get("message") or (error.text or "")
        return JunitTestcase("error", msg.splitlines()[0] if msg else "", None)
    if skipped is not None:
        return JunitTestcase("skipped")
    return JunitTestcase("passed")


def parse_junit_full(path: Path | None) -> FullJunitResult:
    """Parse every testcase in ``path``, keyed by ``(classname, name)`` --
    the basis both :func:`parse_junit` (mapped-id lookup) and the drift/
    arbitration full-scope checks build on."""
    if path is None or not path.exists():
        return FullJunitResult(tests_collected=None, parse_failed=True)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return FullJunitResult(tests_collected=None, parse_failed=True)

    root = tree.getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return FullJunitResult(tests_collected=None, parse_failed=True)

    tests_collected: int | None
    try:
        tests_collected = int(suite.get("tests", ""))
    except ValueError:
        tests_collected = None

    by_key: dict[tuple[str, str], JunitTestcase] = {}
    for testcase in suite.findall("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        by_key[(classname, name)] = _testcase_outcome(testcase)

    return FullJunitResult(
        tests_collected=tests_collected, parse_failed=False, by_key=by_key
    )


def parse_junit(path: Path | None, mapped_ids: list[str]) -> JunitResult:
    """Parse ``path`` (a pytest ``--junitxml`` file) for the given mapped ids.

    ``tests_collected`` stays ``None`` unless the file parses successfully
    (an absent or unparsable file is a *different* condition -- ``junit`` --
    at a lower precedence than ``collected``; see ``classify.py``).
    """
    full = parse_junit_full(path)
    outcomes: dict[str, JunitTestcase] = {}
    for node_id in mapped_ids:
        key = mangle_node_id(node_id)
        testcase = full.by_key.get(key)
        if testcase is not None:
            outcomes[node_id] = testcase
    return JunitResult(
        tests_collected=full.tests_collected,
        parse_failed=full.parse_failed,
        outcomes=outcomes,
    )


def mapped_failed_in_full(full: FullJunitResult, mapped_ids: list[str]) -> list[str]:
    """Which of ``mapped_ids`` actually show as ``failed`` in a FULL-scope
    junit (SPEC v3 §5 ``not_reproduced``, review round 2 blockers 1/2): the
    tie-break for "did the full-scope run even see the edit". A targeted
    run that was RED, whose full-scope re-run shows NONE of the same mapped
    ids failing, was not reproduced -- most likely the full-scope run
    imported a different, unedited copy of the module (a blind
    ``PYTHONPATH``), not a real recovery."""
    return sorted(
        node_id
        for node_id in mapped_ids
        if (tc := full.by_key.get(mangle_node_id(node_id))) is not None
        and tc.outcome == "failed"
    )


def unmapped_assertion_shaped_failures(
    full: FullJunitResult, mapped_ids: list[str]
) -> list[str]:
    """Every OTHER assertion-shaped failure in the full-scope junit, outside
    ``mapped_ids`` (SPEC v3 §5 drift: "full-scope failing ids outside tests
    -> unmapped_red[]"). Returns ``classname::name`` strings (a diagnostic
    label, not necessarily an exact pytest node id -- the mangling isn't
    perfectly invertible, and this is for the DRIFT report, not re-lookup)."""
    mapped_keys = {mangle_node_id(t) for t in mapped_ids}
    unmapped: list[str] = []
    for key, testcase in full.by_key.items():
        if key in mapped_keys:
            continue
        if testcase.outcome == "failed" and testcase.assertion_shaped:
            unmapped.append("::".join(key))
    return sorted(unmapped)
