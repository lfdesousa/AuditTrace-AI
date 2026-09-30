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

#: Matches EITHER the direct binary (``.venv/bin/pytest``, ``pytest``) or
#: the module form (``python -m pytest`` / ``python3 -m pytest``) -- a
#: Makefile recipe that switches to the module form to dodge a
#: substring-only check on the binary path must still be caught.
_PYTEST_INVOCATION_RE = re.compile(r"(?:/|^|\s)pytest\b|\bpython3?\s+-m\s+pytest\b")


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
