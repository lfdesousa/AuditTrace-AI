"""Check 12 of ``scripts/post-deploy-verify.sh`` — Keycloak IdP drift (#403).

Two layers of proof, matching the spec's two falsifiable-test requirement:

1. **Pure comparison logic** (``scripts/idp-drift-check.sh``,
   ``idp_drift_report``) is exercised directly via a bash subprocess with
   mocked declared/live alias lists — no cluster needed. A companion
   "neutered" variant demonstrates that removing the live-only side of the
   diff lets an undeclared IdP pass silently, proving the positive test is
   not vacuous (`feedback_vacuous_neuter_test_antipattern`).
2. **Property-pinning tests** on ``post-deploy-verify.sh`` itself, mirroring
   ``TestPostDeployVerifyKeycloakScopeGuard`` in ``test_chart_drift_guards.py``
   for Check 11 — the same class of check, the same discipline (credential on
   stdin, SKIP not FAIL without a credential, read-only).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IDP_DRIFT_LIB = REPO_ROOT / "scripts" / "idp-drift-check.sh"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "post-deploy-verify.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash CLI not on PATH"
)

# A deliberately broken clone of idp_drift_report: the live-only ("extra" /
# UNDECLARED) side of the diff is dropped entirely. This is NOT derived by
# string-patching the real file (fragile, silently stops testing anything
# useful if the real file's formatting changes) — it is a standalone,
# hand-written reproduction of the exact #370/#371-class shadow-client blind
# spot the real function exists to close.
_NEUTERED_LIB = """
idp_drift_report() {
    local declared="$1"
    local live="$2"
    local missing rc=0
    local declared_sorted live_sorted
    declared_sorted=$(printf '%s\\n' "$declared" | sort -u | grep -v '^$' || true)
    live_sorted=$(printf '%s\\n' "$live" | sort -u | grep -v '^$' || true)

    # NEUTERED: the live-only ("extra" / UNDECLARED) side of the diff is
    # skipped entirely.
    missing=$(comm -23 <(printf '%s\\n' "$declared_sorted") \\
                        <(printf '%s\\n' "$live_sorted") || true)

    for alias in $missing; do
        echo "MISSING: ${alias} is committed but not present in the live realm"
        rc=1
    done

    return "$rc"
}
"""


def _run_idp_drift_report(
    declared: str, live: str, *, lib_path: Path = IDP_DRIFT_LIB
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'. "{lib_path}"; idp_drift_report "$1" "$2"',
            "bash",
            declared,
            live,
        ],
        capture_output=True,
        text=True,
    )


# ── pure comparison logic: real behaviour ───────────────────────────────────


def test_lib_file_exists_and_is_sourceable():
    assert IDP_DRIFT_LIB.exists(), "scripts/idp-drift-check.sh is missing"
    result = subprocess.run(
        ["bash", "-c", f'. "{IDP_DRIFT_LIB}"; type idp_drift_report'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_no_drift_when_declared_matches_live():
    result = _run_idp_drift_report("google-test", "google-test")
    assert result.returncode == 0
    assert result.stdout == ""


def test_no_drift_when_both_empty():
    result = _run_idp_drift_report("", "")
    assert result.returncode == 0
    assert result.stdout == ""


def test_flags_undeclared_live_idp():
    """The #370/#371-class shadow pattern: an IdP added live (e.g. via
    scripts/setup-idp-federation.sh) that was never committed anywhere.
    """
    result = _run_idp_drift_report("google-test", "google-test\nshadow-idp")
    assert result.returncode == 1
    assert "UNDECLARED: shadow-idp is live but not committed" in result.stdout


def test_flags_missing_committed_idp():
    """The #403-class pattern: committed in the realm JSON, but a cold
    reimport would not find it live (or it was manually deleted live)."""
    result = _run_idp_drift_report("google-test\nentra-acme", "google-test")
    assert result.returncode == 1
    assert "MISSING: entra-acme is committed but not present" in result.stdout


def test_flags_both_directions_simultaneously():
    result = _run_idp_drift_report("entra-acme", "google-test")
    assert result.returncode == 1
    assert "UNDECLARED: google-test" in result.stdout
    assert "MISSING: entra-acme" in result.stdout


def test_multiple_matching_aliases_is_not_drift():
    result = _run_idp_drift_report(
        "entra-acme\ngoogle-test\nokta-test", "entra-acme\ngoogle-test\nokta-test"
    )
    assert result.returncode == 0
    assert result.stdout == ""


# ── falsifiability demo: neutering the diff must go RED ────────────────────


def test_neutered_diff_would_miss_undeclared_idp(tmp_path):
    """Falsifiability demo (`feedback_vacuous_neuter_test_antipattern`): a
    variant of idp_drift_report that skips the live-only ("extra") side of
    the comparison lets an undeclared live IdP pass SILENTLY — reproducing
    the exact #370/#371 shadow-client blind spot this guard exists to
    close. This proves `test_flags_undeclared_live_idp` is not vacuous: a
    broken diff is demonstrably distinguishable from a correct one.
    """
    neutered_path = tmp_path / "idp-drift-check-neutered.sh"
    neutered_path.write_text(_NEUTERED_LIB, encoding="utf-8")

    result = _run_idp_drift_report(
        "google-test", "google-test\nshadow-idp", lib_path=neutered_path
    )

    assert result.returncode == 0, (
        "neutered diff should have MISSED the undeclared IdP (returned "
        f"{result.returncode}, stdout={result.stdout!r})"
    )
    assert "UNDECLARED" not in result.stdout


def test_neutered_diff_still_catches_missing_committed_idp(tmp_path):
    """The neutered variant only drops the live-only side; the
    committed-only ("missing") side is untouched, so this direction still
    fires. Confirms the neuter is surgical (isolates exactly one failure
    mode) rather than a blanket no-op that would prove nothing either way.
    """
    neutered_path = tmp_path / "idp-drift-check-neutered.sh"
    neutered_path.write_text(_NEUTERED_LIB, encoding="utf-8")

    result = _run_idp_drift_report(
        "google-test\nentra-acme", "google-test", lib_path=neutered_path
    )

    assert result.returncode == 1
    assert "MISSING: entra-acme" in result.stdout


# ── property-pinning: Check 12 in post-deploy-verify.sh itself ─────────────
# Mirrors TestPostDeployVerifyKeycloakScopeGuard (Check 11, #370) in
# test_chart_drift_guards.py. These tests cannot prove the LIVE detection —
# that needs a cluster (see the PR's Validation section) — they pin the
# properties that make the check trustworthy, same as Check 11's tests.


class TestPostDeployVerifyIdpDriftGuard:
    @staticmethod
    def _script() -> str:
        return VERIFY_SCRIPT.read_text(encoding="utf-8")

    def test_check_exists(self) -> None:
        assert "Keycloak identity-provider drift" in self._script(), (
            "post-deploy-verify.sh lost its IdP drift check. This is the "
            "only place a live-only-in-the-DB broker like `google-test` is "
            "compared against the committed realm; without it, a cold "
            "reimport silently wipes it (#403)."
        )

    def test_sources_the_pure_comparison_library(self) -> None:
        script = self._script()
        assert "idp-drift-check.sh" in script, (
            "Check 12 must source scripts/idp-drift-check.sh rather "
            "than inlining the comparison — the whole point of the split "
            "is that the comparison is independently unit-testable."
        )
        assert "idp_drift_report" in script

    def test_admin_password_never_reaches_argv(self) -> None:
        script = self._script()
        offenders = [
            line.strip()
            for line in script.splitlines()
            if "env " in line and "KEYCLOAK_ADMIN_PASSWORD" in line
        ]
        assert not offenders, (
            "Keycloak admin password passed via `env VAR=` in Check 12 — "
            "it lands in the pod's process table. Pipe it on stdin. Sites: "
            + " | ".join(offenders)
        )

    def test_skips_rather_than_fails_without_credential(self) -> None:
        script = self._script()
        # Both Check 11 and Check 12 share this literal skip message; assert
        # it appears at least twice (once per check) rather than once.
        assert script.count('skip "no Keycloak admin credential') >= 2, (
            "Check 12 must SKIP (not FAIL) when no admin credential is "
            "available — unprivileged post-deploy runs are supported."
        )

    def test_is_read_only(self) -> None:
        """Check 12 only GETs identity-provider/instances — it must never
        create, update, or delete a live IdP. Scoped narrowly to the Check
        12 block (between its header and the next one) so this doesn't
        false-positive on Check 11's own writes, if any were ever added.
        """
        script = self._script()
        start = script.index('header "(12/14)')
        end = script.index('header "(13/14)')
        block = script[start:end]
        assert "identity-provider/instances" in block
        for verb in ("kcadm create", "kcadm update", "kcadm delete", "-X DELETE"):
            assert verb not in block, f"Check 12 must be read-only; found {verb!r}"
        # The one POST in the block is the admin token exchange itself
        # (grant_type=password against /token) — not a write to the realm.
        post_lines = [line for line in block.splitlines() if "-X POST" in line]
        assert all("openid-connect/token" in line for line in post_lines), (
            "Unexpected POST in the read-only Check 12 block: " + str(post_lines)
        )

    def test_reports_both_drift_directions_by_label(self) -> None:
        script = self._script()
        start = script.index('header "(12/14)')
        end = script.index('header "(13/14)')
        block = script[start:end]
        # The labels themselves live in idp-drift-check.sh; Check 12 must
        # surface idp_drift_output verbatim rather than re-summarising it,
        # so both UNDECLARED and MISSING findings reach the operator.
        assert "idp_drift_output" in block
