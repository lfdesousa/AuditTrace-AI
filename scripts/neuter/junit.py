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


def _failure_type(failure_msg: str) -> str:
    """``failure_type := AssertionError if failure_msg starts with 'assert '``
    ``else the text before the first ':'`` (§4)."""
    first_line = failure_msg.splitlines()[0] if failure_msg else ""
    if first_line.startswith("assert "):
        return "AssertionError"
    return first_line.split(":", 1)[0].strip()


def parse_junit(path: Path | None, mapped_ids: list[str]) -> JunitResult:
    """Parse ``path`` (a pytest ``--junitxml`` file) for the given mapped ids.

    ``tests_collected`` stays ``None`` unless the file parses successfully
    (an absent or unparsable file is a *different* condition -- ``junit`` --
    at a lower precedence than ``collected``; see ``classify.py``).
    """
    if path is None or not path.exists():
        return JunitResult(tests_collected=None, parse_failed=True)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return JunitResult(tests_collected=None, parse_failed=True)

    root = tree.getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return JunitResult(tests_collected=None, parse_failed=True)

    tests_collected: int | None
    try:
        tests_collected = int(suite.get("tests", ""))
    except ValueError:
        tests_collected = None

    by_key: dict[tuple[str, str], ET.Element] = {}
    for testcase in suite.findall("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        by_key[(classname, name)] = testcase

    outcomes: dict[str, JunitTestcase] = {}
    for node_id in mapped_ids:
        key = mangle_node_id(node_id)
        testcase = by_key.get(key)
        if testcase is None:
            continue
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            msg = failure.get("message") or (failure.text or "")
            ftype = _failure_type(msg)
            outcomes[node_id] = JunitTestcase(
                "failed", msg.splitlines()[0] if msg else "", ftype
            )
        elif error is not None:
            msg = error.get("message") or (error.text or "")
            outcomes[node_id] = JunitTestcase(
                "error", msg.splitlines()[0] if msg else "", None
            )
        elif skipped is not None:
            outcomes[node_id] = JunitTestcase("skipped")
        else:
            outcomes[node_id] = JunitTestcase("passed")

    return JunitResult(
        tests_collected=tests_collected, parse_failed=False, outcomes=outcomes
    )
