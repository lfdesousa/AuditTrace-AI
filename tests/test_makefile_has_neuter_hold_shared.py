"""Guard: the heavy cap actually wraps `make test`/`test-cov` and precedes
every image build (SPEC v3 §10; FNH2-BL-1).

Parses ``make -n <target>`` (a real dry-run, resolving Make's own
variables/recipe-echoing exactly as it would execute) rather than grepping
the Makefile's raw text: a ``: <wrapper>`` no-op comment line PLUS a
genuinely unwrapped ``pytest`` invocation both still contain the wrapper
STRING somewhere in the file, so a text-presence check stays green while
the real recipe silently drops the wrap (review round 1 should-fix). This
asserts on the ACTUAL command lines `make` would run: every line invoking
pytest must itself be the ``hold-shared`` wrapper, never a bare pytest.

Review round 2 should-fix: a line that wraps a DECOY ``--collect-only``
invocation with ``hold-shared`` while leaving a SECOND, real ``python -m
pytest`` line unwrapped must also be caught -- the detection regex covers
BOTH the ``.venv/bin/pytest`` binary form and the ``python -m pytest``
module form, not just the former.

Review round 3 should-fix: the regex now also catches the legacy ``py.test``
launcher name (pytest's own pre-rename entry point, still installed by
some distributions) -- previously invisible to a plain ``pytest`` substring
search because of the literal dot.

This Makefile-level guard stays STATIC (a ``make -n`` dry-run + regex),
not B2's runtime chokepoint token: ``make test``'s own top-level pytest
invocation is the legitimate, un-gated OUTER session every developer/CI
run depends on (``scripts.neuter.pytest_run`` is not even imported yet
when it starts), so making it carry a token would break every normal test
run, not just a bypass. B2's token protects invocations the NEUTER HARNESS
itself spawns (baseline/drift/arbitrate/collect/neuter); this guard
protects `make test`'s OWN recipe text from acquiring a second, unwrapped
pytest line.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (REPO_ROOT / "Makefile").read_text()

_HOLD_SHARED_PREFIX = (
    ".venv/bin/python -m scripts.neuter.runner hold-shared -- .venv/bin/pytest"
)
_ASSERT_IDLE = "python -m scripts.neuter.runner assert-idle"

#: Matches the direct binary (``.venv/bin/pytest``, ``pytest``), the module
#: form (``python -m pytest`` / ``python3 -m pytest``), OR the legacy
#: ``py.test`` launcher name -- a Makefile recipe that switches to any of
#: these to dodge a substring-only check on one specific spelling must
#: still be caught.
_PYTEST_INVOCATION_RE = re.compile(
    r"(?:/|^|\s)pytest\b|\bpython3?\s+-m\s+pytest\b|(?:/|^|\s)py\.test\b"
)


def _dry_run(target: str) -> list[str]:
    """The exact command lines ``make -n <target>`` would execute, with
    Make's own ``@``-echo suppression and variable expansion already
    applied -- what the shell would ACTUALLY run, not the recipe's source
    text."""
    result = subprocess.run(
        ["make", "-n", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _assert_every_pytest_invocation_is_wrapped(target: str) -> None:
    lines = _dry_run(target)
    pytest_lines = [line for line in lines if _PYTEST_INVOCATION_RE.search(line)]
    assert pytest_lines, f"`make -n {target}` never invokes pytest at all"
    for line in pytest_lines:
        assert line.strip().startswith(_HOLD_SHARED_PREFIX), (
            f"unwrapped pytest invocation in `make -n {target}`: {line!r}"
        )


def test_test_target_pytest_line_wrapped_by_hold_shared():
    _assert_every_pytest_invocation_is_wrapped("test")


def test_test_cov_target_pytest_line_wrapped_by_hold_shared():
    _assert_every_pytest_invocation_is_wrapped("test-cov")


def test_regex_catches_the_module_form_escape():
    """Positive control on the DETECTION ITSELF: a recipe that wraps a
    decoy ``--collect-only`` with ``hold-shared`` but leaves a real,
    module-form ``python -m pytest`` line unwrapped (the reviewer's
    should-fix escape) must be caught, not missed by a binary-path-only
    regex."""
    wrapped_decoy = (
        f"{_HOLD_SHARED_PREFIX} --collect-only -q -o addopts= tests/test_x.py"
    )
    unwrapped_module_form = "python -m pytest tests/ --cov=src --cov-fail-under=90"
    assert _PYTEST_INVOCATION_RE.search(wrapped_decoy)
    assert _PYTEST_INVOCATION_RE.search(unwrapped_module_form)
    assert not unwrapped_module_form.strip().startswith(_HOLD_SHARED_PREFIX)


def test_regex_does_not_match_unrelated_lines():
    assert not _PYTEST_INVOCATION_RE.search("echo running the suite")
    assert not _PYTEST_INVOCATION_RE.search("docker build -t audittrace-ai .")


def test_regex_catches_the_legacy_py_dot_test_launcher():
    """Review round 3 should-fix: ``py.test`` (pytest's pre-rename entry
    point) is a DIFFERENT literal string than ``pytest`` -- a plain
    substring search for ``pytest`` never matches it (the dot breaks the
    run), so an unwrapped ``py.test tests/`` line would have sailed through
    this guard undetected before this round."""
    unwrapped_py_dot_test = "py.test tests/ --cov=src --cov-fail-under=90"
    assert _PYTEST_INVOCATION_RE.search(unwrapped_py_dot_test)
    assert not unwrapped_py_dot_test.strip().startswith(_HOLD_SHARED_PREFIX)
    assert "pytest" not in unwrapped_py_dot_test  # confirms the OLD regex missed it


def _target_body(name: str) -> str:
    lines = MAKEFILE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
    body = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ")):
            break
        body.append(line)
    return "\n".join(body)


def test_docker_build_preceded_by_assert_idle():
    body = _target_body("docker-build")
    lines = [line for line in body.splitlines() if line.strip().lstrip("@")]
    docker_idx = next(
        i for i, line in enumerate(lines) if "docker build -t audittrace-ai" in line
    )
    assert any(_ASSERT_IDLE in line for line in lines[:docker_idx])


def test_dockerfile_tests_build_preceded_by_assert_idle():
    body = _target_body("test-integration")
    lines = [line for line in body.splitlines() if line.strip().lstrip("@")]
    docker_idx = next(
        i for i, line in enumerate(lines) if "docker build -f Dockerfile.tests" in line
    )
    assert any(_ASSERT_IDLE in line for line in lines[:docker_idx])
