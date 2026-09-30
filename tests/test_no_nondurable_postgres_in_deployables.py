"""Guard: the harness's non-durable Postgres flags (SPEC v3 §8) must never
reach the chart, compose, or deploy/release tooling.

A test-only, throwaway ``postgres:16`` container running with ``fsync=off``,
``synchronous_commit=off``, ``full_page_writes=off`` is fine for a neuter
pool -- crash durability doesn't matter for a container that lives seconds
and is force-removed. The SAME flags on a real deployment would risk silent
data loss on a crash. This guard is broad and case-insensitive on purpose
(``off``/``false``/``no``/``0``): a narrower regex is a narrower guard.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Broad, case-insensitive on purpose (SPEC v3 §8).
_NONDURABLE_RE = re.compile(
    r"(fsync|synchronous_commit|full_page_writes)\s*[:=]\s*['\"]?(off|false|no|0)\b",
    re.IGNORECASE,
)

_DEPLOYABLE_GLOBS = (
    ("charts", "**/*"),
    (".", "docker-compose*.yml"),
    ("scripts/deploy", "**/*"),
    ("scripts/release", "**/*"),
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _deployable_tracked_files() -> list[Path]:
    tracked = set(_tracked_files())
    matched: list[Path] = []
    for base, pattern in _DEPLOYABLE_GLOBS:
        for path in (REPO_ROOT / base).glob(pattern):
            if path.is_file() and path in tracked:
                matched.append(path)
    return matched


def test_deployable_tracked_files_scan_is_non_empty():
    assert len(_deployable_tracked_files()) >= 5


def test_no_nondurable_postgres_flags_in_tracked_deployables():
    offenders = []
    for path in _deployable_tracked_files():
        text = path.read_text(errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _NONDURABLE_RE.search(line):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:160]}"
                )
    assert offenders == [], (
        "non-durable Postgres flags found in a deployable:\n" + "\n".join(offenders)
    )


def test_no_nondurable_postgres_flags_in_rendered_chart():
    result = subprocess.run(
        [
            "helm",
            "template",
            "audittrace",
            str(REPO_ROOT / "charts" / "audittrace"),
            "--set",
            "vault.enabled=false",
            "--set",
            "secrets.minio.secretKey=ci-test",
            "--set",
            "secrets.minio.kmsKey=ci-test",
            "--set",
            "secrets.chromadb.token=ci-test",
            "--set",
            "secrets.keycloak.adminPassword=ci-test",
            "--set",
            "secrets.postgres.appPassword=ci-test",
            "--set",
            "secrets.postgres.password=ci-test",
            "--set",
            "secrets.redis.password=ci-test",
            "--set",
            "secrets.summariser.password=ci-test",
            "--set",
            "externalLLM.host=llm.test.invalid",
            "--set",
            "observability.external.langfuseHost=langfuse.test.invalid",
            "--set",
            "observability.external.tempoHost=tempo.test.invalid",
            "--set",
            "observability.external.lokiHost=loki.test.invalid",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    offenders = [
        line for line in result.stdout.splitlines() if _NONDURABLE_RE.search(line)
    ]
    assert offenders == [], (
        "non-durable Postgres flags found in `helm template` output:\n"
        + "\n".join(offenders)
    )


def test_positive_control_regex_matches_the_test_fixture_itself():
    """The regex must fire on ``tests/_pg_ephemeral.py`` -- proof the
    pattern is live, not just an untriggered guard."""
    text = (REPO_ROOT / "tests" / "_pg_ephemeral.py").read_text()
    matches = [line for line in text.splitlines() if _NONDURABLE_RE.search(line)]
    assert len(matches) == 3, matches
