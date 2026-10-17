#!/usr/bin/env bash
# Pure comparison logic for post-deploy-verify.sh Check (Keycloak
# identity-provider drift, SPEC #403). Deliberately free of kubectl/kcadm
# calls so it can be unit-tested against mocked declared/live alias lists
# without a cluster — see tests/test_idp_drift_guard.py.
#
# This is the same shape gap that let earlier drift classes through: a
# comparison that can only be exercised against a live cluster gives a
# reviewer no way to prove the DIFF ITSELF is correct, only that the
# surrounding script text looks plausible
# (`feedback_vacuous_neuter_test_antipattern`). Splitting the pure
# set-comparison out of the kubectl/kcadm orchestration in
# post-deploy-verify.sh closes that gap: the function below is invoked
# directly, with fixture input, from a Python test via a bash subprocess.
#
# Usage (sourced, not executed):
#   . scripts/idp-drift-check.sh
#   idp_drift_report "$declared_aliases_newline_separated" \
#                     "$live_aliases_newline_separated"
#
# Prints one drift line per finding to stdout:
#   "UNDECLARED: <alias> is live but not committed to the realm ConfigMap"
#   "MISSING: <alias> is committed but not present in the live realm"
# Returns 0 when the two lists match exactly (no drift), 1 otherwise.
idp_drift_report() {
    local declared="$1"
    local live="$2"
    local extra missing rc=0

    # comm requires sorted, blank-free input on both sides.
    local declared_sorted live_sorted
    declared_sorted=$(printf '%s\n' "$declared" | sort -u | grep -v '^$' || true)
    live_sorted=$(printf '%s\n' "$live" | sort -u | grep -v '^$' || true)

    # comm -13 = in live only (an IdP present live, declared nowhere — the
    # #370/#371-class "shadow" pattern: nothing else can see it).
    extra=$(comm -13 <(printf '%s\n' "$declared_sorted") \
                      <(printf '%s\n' "$live_sorted") || true)
    # comm -23 = in declared only (committed, but a cold reimport would not
    # find it live — the #403-class "wiped on reimport" pattern).
    missing=$(comm -23 <(printf '%s\n' "$declared_sorted") \
                        <(printf '%s\n' "$live_sorted") || true)

    for alias in $extra; do
        echo "UNDECLARED: ${alias} is live but not committed to the realm ConfigMap"
        rc=1
    done
    for alias in $missing; do
        echo "MISSING: ${alias} is committed but not present in the live realm"
        rc=1
    done

    return "$rc"
}
