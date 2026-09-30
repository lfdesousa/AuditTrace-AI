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
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (REPO_ROOT / "Makefile").read_text()

_HOLD_SHARED_PREFIX = (
    ".venv/bin/python -m scripts.neuter.runner hold-shared -- .venv/bin/pytest"
)
_ASSERT_IDLE = "python -m scripts.neuter.runner assert-idle"


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
    pytest_lines = [line for line in lines if ".venv/bin/pytest" in line]
    assert pytest_lines, f"`make -n {target}` never invokes pytest at all"
    for line in pytest_lines:
        assert line.strip().startswith(_HOLD_SHARED_PREFIX), (
            f"unwrapped pytest invocation in `make -n {target}`: {line!r}"
        )


def test_test_target_pytest_line_wrapped_by_hold_shared():
    _assert_every_pytest_invocation_is_wrapped("test")


def test_test_cov_target_pytest_line_wrapped_by_hold_shared():
    _assert_every_pytest_invocation_is_wrapped("test-cov")


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
