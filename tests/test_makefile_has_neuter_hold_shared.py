"""Guard: the heavy cap actually wraps `make test`/`test-cov` and precedes
every image build (SPEC v3 §10; FNH2-BL-1).

Asserts both pytest lines in the Makefile's `test` and `test-cov` targets
start with the `hold-shared` wrapper, and that `docker-build` and the
`Dockerfile.tests` build (`test-integration`) are preceded by `assert-idle`.
A removed wrapper reddens this test -- it is the mechanical enforcement,
not a comment.
"""

from __future__ import annotations

from pathlib import Path

MAKEFILE = (Path(__file__).resolve().parent.parent / "Makefile").read_text()

_HOLD_SHARED = "python -m scripts.neuter.runner hold-shared -- .venv/bin/pytest tests/"
_ASSERT_IDLE = "python -m scripts.neuter.runner assert-idle"


def _target_body(name: str) -> str:
    lines = MAKEFILE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
    body = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ")):
            break
        body.append(line)
    return "\n".join(body)


def test_test_target_pytest_line_wrapped_by_hold_shared():
    body = _target_body("test")
    assert _HOLD_SHARED in body


def test_test_cov_target_pytest_line_wrapped_by_hold_shared():
    body = _target_body("test-cov")
    assert _HOLD_SHARED in body


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
