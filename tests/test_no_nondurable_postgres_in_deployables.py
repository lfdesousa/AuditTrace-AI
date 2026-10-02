"""Guard: the harness's non-durable Postgres flags (SPEC v3 §8) must never
reach the chart, compose, or deploy/release tooling.

A test-only, throwaway ``postgres:16`` container running with ``fsync=off``,
``synchronous_commit=off``, ``full_page_writes=off`` is fine for a neuter
pool -- crash durability doesn't matter for a container that lives seconds
and is force-removed. The SAME flags on a real deployment would risk silent
data loss on a crash.

Review round 2 should-fix: enumerating spellings loses (a review-1 fix
already had to add ``f``/``n`` after ``fsync = of`` / ``fal`` were found to
pass while ``postgres:16`` itself reports ``off`` for both). Postgres's OWN
boolean-parsing rule is not an enumeration -- it accepts ``1``/``0`` or ANY
UNAMBIGUOUS PREFIX of ``on``/``off``/``true``/``false``/``yes``/``no``
(case-insensitively), and rejects a prefix that is ambiguous between the
true and false word lists (``"o"`` alone, prefixing both ``on`` and
``off``). This guard now PARSES the value the same way Postgres's
``parse_bool`` does (see ``src/backend/utils/misc/guc.c`` /
``postgresql.conf`` documentation), rather than enumerating a fixed set of
strings that always trails the server's real acceptance list.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_KEY_RE = re.compile(
    r"(fsync|synchronous_commit|full_page_writes)\s*[:=]\s*(['\"]?)([A-Za-z01]+)\2",
    re.IGNORECASE,
)

#: Postgres's own accepted spellings (``src/backend/utils/adt/bool.c``
#: ``parse_bool_with_len``): case-insensitive, ``1``/``0``, or ANY
#: unambiguous prefix of these words (``on``/``off`` need 2 chars minimum
#: to disambiguate from each other; ``t``/``f``/``y``/``n`` are already
#: unambiguous single-char prefixes since no OTHER word shares that first
#: letter).
_TRUE_WORDS = ("on", "true", "yes")
_FALSE_WORDS = ("off", "false", "no")


def _postgres_bool(raw: str) -> bool | None:
    """Parse ``raw`` the way Postgres parses a boolean GUC value. Returns
    ``True``/``False``, or ``None`` if ``raw`` is not a valid/unambiguous
    Postgres boolean at all (never durable OR non-durable -- e.g. a
    completely different setting's value that happened to match the outer
    key/value regex by coincidence)."""
    v = raw.strip().lower()
    if v == "1":
        return True
    if v == "0":
        return False
    if not v:
        return None
    true_hit = any(w.startswith(v) for w in _TRUE_WORDS)
    false_hit = any(w.startswith(v) for w in _FALSE_WORDS)
    if true_hit and not false_hit:
        return True
    if false_hit and not true_hit:
        return False
    return None  # ambiguous (e.g. "o") or unrecognised


def _nondurable_matches(text: str) -> list[re.Match[str]]:
    """Every regex match in ``text`` whose value PARSES to a non-durable
    (``False``) Postgres boolean -- never a durable/ambiguous/unrelated
    one."""
    return [m for m in _KEY_RE.finditer(text) if _postgres_bool(m.group(3)) is False]


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
            if _nondurable_matches(line):
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
        line for line in result.stdout.splitlines() if _nondurable_matches(line)
    ]
    assert offenders == [], (
        "non-durable Postgres flags found in `helm template` output:\n"
        + "\n".join(offenders)
    )


def test_positive_control_regex_matches_the_test_fixture_itself():
    """The parser must fire on ``tests/_pg_ephemeral.py`` -- proof the
    pattern is live, not just an untriggered guard."""
    text = (REPO_ROOT / "tests" / "_pg_ephemeral.py").read_text()
    matches = [line for line in text.splitlines() if _nondurable_matches(line)]
    assert len(matches) == 3, matches


def test_parser_accepts_every_unambiguous_prefix_and_full_spelling():
    """Review round 2 should-fix, the reviewer's exact escape: ``fsync = of``
    and ``fsync = fal`` (unambiguous PREFIXES of ``off``/``false``, which
    ``postgres:16`` itself parses identically to the full spelling) must be
    caught -- an enumeration of only the full words misses every prefix a
    real ``postgresql.conf`` accepts."""
    for key in ("fsync", "synchronous_commit", "full_page_writes"):
        for spelling in (
            "off",
            "OFF",
            "of",
            "false",
            "False",
            "fal",
            "f",
            "F",
            "no",
            "No",
            "n",
            "0",
        ):
            for sep in (":", "="):
                for quote in ("", '"', "'"):
                    line = f"{key} {sep} {quote}{spelling}{quote}"
                    assert _nondurable_matches(line), f"no match: {line!r}"


def test_parser_rejects_durable_and_ambiguous_values():
    """The mirror image: durable values (``on``, ``true``, ``1``, ``t``,
    ``yes``, ``y``) must never trip the guard, and a genuinely AMBIGUOUS
    prefix (``o``, which prefixes both ``on`` and ``off``) must not be
    silently treated as either -- Postgres itself rejects it as invalid,
    and this guard must not invent a verdict Postgres wouldn't give."""
    for spelling in ("on", "ON", "true", "True", "1", "t", "T", "yes", "y"):
        line = f"fsync = {spelling}"
        assert not _nondurable_matches(line), f"unexpected match: {line!r}"
    assert _postgres_bool("o") is None
    assert not _nondurable_matches("fsync = o")


def test_postgres_bool_parser_matches_documented_semantics_directly():
    """Unit-level proof of the parser itself, independent of the regex:
    every value in Postgres's own documented boolean grammar round-trips
    to the expected True/False, and unrelated garbage is ``None``."""
    for word in ("on", "true", "yes", "1", "TRUE", "Yes"):
        assert _postgres_bool(word) is True
    for word in ("off", "false", "no", "0", "FALSE", "No"):
        assert _postgres_bool(word) is False
    for word in ("", "maybe", "2", "o", "xyz"):
        assert _postgres_bool(word) is None
