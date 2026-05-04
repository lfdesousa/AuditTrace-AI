"""Drift guard for AGENTS.md.

Background. AGENTS.md is the shared agent-orientation file (the public
counterpart to the local CLAUDE.md). On 2026-05-04 the project
orientation flagged that AGENTS.md had drifted: it still talked about
``sovereign_memory/``, ``init-sovereign-app-role.sh``, "421 tests",
Traefik (replaced by Istio per ADR-033/034), etc. This test pins the
forbidden-term list so future contributors don't re-introduce the same
drift class.

ADR-035 explicitly retains a small set of names for backwards compat
(OTel attribute prefixes ``sovereign.component`` / ``sovereign.operation.*``,
the Redis key prefixes ``sovereign:tool-result:`` and ``sovereign:token:``,
the TLS cert filename ``certs/sovereign.pem``, and the Postgres
superuser role ``sovereign``). The test allowlists these by structure
(prefixed match) rather than by line context, since they appear in
day-to-day mentions that don't naturally carry rename-history markers.

Other historical references (``sovereign-memory-server``, ``SOVEREIGN_*``
env prefix) are allowed only when the surrounding line carries a
rename-context marker — that's how the file documents the rename history
without re-asserting current state.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"


# Strictly forbidden — no allow-context exemption. Each is something
# that was definitively renamed/replaced and should not appear in current
# guidance.
STRICT_FORBIDDEN: list[tuple[str, str]] = [
    (
        "sovereign-postgres",
        "Postgres service was renamed; current name is audittrace-postgresql.",
    ),
    (
        "init-sovereign-app-role",
        "Script was renamed; current name is init-audittrace-app-role.sh.",
    ),
    (
        "src/sovereign_memory/",
        "Package was renamed; current path is src/audittrace/ (ADR-035).",
    ),
    (
        "Traefik v3",
        "Edge proxy replaced by Istio Gateway during k3s migration (ADR-033/034).",
    ),
    (
        "421 tests",
        "Pinned count drifts every release; use a band or `make test` reference.",
    ),
    (
        "sovereign_ai",
        "DB name was renamed; current name is audittrace (ADR-035).",
    ),
]


# Soft-forbidden — allowed only when the surrounding line carries a marker
# indicating the mention is rename-history, not current guidance.
SOFT_FORBIDDEN: list[tuple[str, str]] = [
    (
        "sovereign-memory-server",
        "Old project name; mention is only allowed in rename-history context.",
    ),
    (
        "SOVEREIGN_",
        "Old env-var prefix; mention is only allowed in rename-history context.",
    ),
]

# Lines containing any of these markers are treated as rename-history
# context; soft-forbidden patterns are allowed there.
ALLOW_CONTEXT_MARKERS: list[str] = [
    "ADR-035",
    "renamed",
    "→",
    "previously",
    "historical",
    "stale",  # "stale SOVEREIGN_* / sovereign_memory names" — context_builder.py
]


# Allowlist for STRICT_FORBIDDEN: per-pattern surrounding-substring
# guards. Used sparingly — most strict patterns should never legitimately
# appear in AGENTS.md. If you find yourself adding one of these, consider
# whether the strict pattern itself is too broad.
STRICT_ALLOW_SUBSTRINGS: dict[str, list[str]] = {
    # Example shape (left empty intentionally — the strict list above is
    # already narrow enough that no mention should slip through):
    # "Traefik v3": ["was previously"],
}


def _line_has_allow_marker(line: str) -> bool:
    return any(marker in line for marker in ALLOW_CONTEXT_MARKERS)


def test_agents_md_has_no_stale_terms() -> None:
    """AGENTS.md must not contain forbidden stale terms outside of
    rename-history context.

    Failure mode: a contributor edits AGENTS.md (or merges old content)
    that re-introduces a drifted term. The assertion message lists each
    violation with line number + the line text + why it's forbidden, so
    the fix is one targeted edit per violation.
    """
    assert AGENTS_MD.exists(), f"AGENTS.md not found at {AGENTS_MD}"
    text = AGENTS_MD.read_text(encoding="utf-8")

    violations: list[str] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        # Strict layer — narrow patterns, no allow-context exemption,
        # but per-pattern substring allowlist is honoured.
        for pattern, reason in STRICT_FORBIDDEN:
            if pattern not in line:
                continue
            allow_subs = STRICT_ALLOW_SUBSTRINGS.get(pattern, [])
            if any(s in line for s in allow_subs):
                continue
            violations.append(
                f"  AGENTS.md:{line_no} — STRICT '{pattern}': {reason}\n"
                f"      → {line.strip()[:140]}"
            )

        # Soft layer — allowed in rename-history context.
        if _line_has_allow_marker(line):
            continue
        for pattern, reason in SOFT_FORBIDDEN:
            if pattern in line:
                violations.append(
                    f"  AGENTS.md:{line_no} — SOFT '{pattern}': {reason}\n"
                    f"      → {line.strip()[:140]}"
                )

    if violations:
        msg = (
            f"AGENTS.md drift detected ({len(violations)} occurrences). "
            f"Either rewrite the section to current state or wrap the "
            f"mention in rename-history context (one of: "
            f"{', '.join(repr(m) for m in ALLOW_CONTEXT_MARKERS)}).\n\n"
            + "\n".join(violations)
        )
        raise AssertionError(msg)
