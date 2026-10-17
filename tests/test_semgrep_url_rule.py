"""Falsifiable test for the offline semgrep security-lint gate (#382, ADR-059 WS1).

Proves the vendored rule ``audittrace-url-substring-sanitization``
(``ci/semgrep/rules.yml``) actually catches the WS2 class of finding
(``py/incomplete-url-substring-sanitization``) and does NOT false-positive on
the correct ``urlparse(url).hostname == ...`` form.

The whole point of the gate is that removing or breaking the rule makes this
test fail — a mechanical guarantee that the local gate keeps mirroring the
CodeQL backstop.

Determinism / offline: semgrep is invoked with a LOCAL ``--config`` only
(never ``--config auto``), ``--metrics=off`` and ``--disable-version-check`` —
so the test needs no network and returns the same result every run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "ci" / "semgrep" / "rules.yml"
RULE_ID_SUFFIX = "audittrace-url-substring-sanitization"

# The WS2 shape: a hostname-shaped substring test, and a host-named variable
# substring test — both against a URL-like variable. Either MUST fire.
BAD_SRC = """\
def check_literal(url):
    if "evil.example.com" in url:
        return True
    return False


def check_hostvar(url, host):
    if host in url:
        return True
    return False
"""

# The correct form CodeQL is happy with — plus the legitimate scheme/driver
# `in` tests already present in the tree (config.py, db/factory.py). NONE of
# these may fire, or the gate would block the current clean tree.
SAFE_SRC = """\
from urllib.parse import urlparse


def check_safe(url):
    if urlparse(url).hostname == "evil.example.com":
        return True
    return False


def driver_check(url):
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return True
    if "://" in url:
        return False
    return False
"""


def _semgrep_bin() -> str | None:
    """Locate the semgrep binary.

    Prefer the interpreter's own venv (``sys.executable``'s directory) so the
    test uses the SAME pinned semgrep that ``make security-lint`` uses, then
    fall back to PATH.
    """
    candidate = Path(sys.executable).parent / "semgrep"
    if candidate.exists():
        return str(candidate)
    return shutil.which("semgrep")


def _run_semgrep(target: Path) -> list[str]:
    """Run the vendored rule offline against ``target``; return fired rule ids."""
    semgrep = _semgrep_bin()
    if semgrep is None:  # pragma: no cover - environment guard
        pytest.skip("semgrep binary not installed (expected in the dev venv)")

    proc = subprocess.run(
        [
            semgrep,
            "--quiet",
            "--disable-version-check",
            "--metrics=off",
            "--json",
            "--config",
            str(RULES),
            str(target),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.stdout, f"semgrep produced no JSON (stderr: {proc.stderr!r})"
    payload = json.loads(proc.stdout)
    return [result["check_id"] for result in payload["results"]]


def test_rules_file_is_present() -> None:
    """The vendored, offline rules file must exist and declare the custom rule.

    The rules file is a local ``--config`` payload: it must define the rule as
    an ERROR-severity Python rule. (Offline invocation — no registry, no
    ``--config auto`` — is enforced by the callers here and by ``make
    security-lint`` / the CI step, not by this rule file's text.)
    """
    assert RULES.is_file(), f"missing vendored rules file: {RULES}"
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    ids = [rule["id"] for rule in doc["rules"]]
    assert RULE_ID_SUFFIX in ids, f"custom rule missing from {ids!r}"
    rule = next(r for r in doc["rules"] if r["id"] == RULE_ID_SUFFIX)
    assert rule["severity"] == "ERROR"
    assert rule["languages"] == ["python"]


def test_rule_fires_on_url_substring_sanitization(tmp_path: Path) -> None:
    """The bad WS2 pattern MUST trip the custom rule (both branches)."""
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_SRC, encoding="utf-8")

    fired = _run_semgrep(bad)

    matched = [cid for cid in fired if cid.endswith(RULE_ID_SUFFIX)]
    # Two occurrences: the domain-literal branch AND the host-var branch.
    assert len(matched) == 2, (
        f"expected the URL-substring rule to fire twice, got {fired!r}"
    )


def test_rule_does_not_fire_on_safe_form(tmp_path: Path) -> None:
    """The correct ``urlparse().hostname ==`` form must NOT trip the rule.

    Also covers the legitimate scheme/driver ``in`` tests already in the tree,
    guarding against a false positive that would block the clean tree.
    """
    safe = tmp_path / "safe.py"
    safe.write_text(SAFE_SRC, encoding="utf-8")

    fired = _run_semgrep(safe)

    assert fired == [], f"rule false-positived on the safe form: {fired!r}"


def test_clean_tree_has_no_findings() -> None:
    """The current src/ tests/ scripts/ tree must be clean under the gate.

    This is the ADR-049 verification anchor: the gate we are adding must not
    block the tree it lands on.
    """
    semgrep = _semgrep_bin()
    if semgrep is None:  # pragma: no cover - environment guard
        pytest.skip("semgrep binary not installed (expected in the dev venv)")

    proc = subprocess.run(
        [
            semgrep,
            "--error",
            "--quiet",
            "--disable-version-check",
            "--metrics=off",
            "--config",
            str(RULES),
            "src/",
            "tests/",
            "scripts/",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, (
        "security-lint gate is not clean on the current tree:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
