"""THE VERDICT RULE -- from junit per-phase outcomes (SPEC v3 §4; SF-A, SF-B, SF-4).

Every mapped run is classified, in this exact order, first match wins for
``error_reason``; ``error_reasons[]`` records every condition met:

    collected -> nocompile -> pathcheck -> db_leak -> foreign_pg_container ->
    chokepoint_marker_missing -> watch_unproven -> timeout ->
    setup_or_teardown -> skipped -> missing -> call_exception -> junit ->
    exit_code

RED iff no ERROR condition holds and at least one mapped test is ``failed``
(necessarily assertion-shaped by then). GREEN iff every mapped test
``passed``. ``vacuous := (verdict == GREEN)``.

SF-4: on ``nocompile`` the run never starts at all -- ``tests_collected``
is ``None`` and *no other condition is evaluated*: :func:`classify` returns
immediately in that case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scripts.neuter.junit import JunitResult

VALID_EXIT_CODES = frozenset({0, 1})


@dataclass(frozen=True)
class VerdictResult:
    verdict: str  # "RED" | "GREEN" | "ERROR"
    error_reason: str | None
    error_reasons: list[str] = field(default_factory=list)
    tests_collected: int | None = None
    outcomes: dict[str, str] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failure_msgs: dict[str, str] = field(default_factory=dict)
    failure_types: list[str] = field(default_factory=list)

    @property
    def vacuous(self) -> bool:
        return self.verdict == "GREEN"


def classify(
    mapped_ids: list[str],
    junit_result: JunitResult | None,
    *,
    tests_expected: int,
    nocompile: bool,
    pathcheck_ok: bool,
    db_leak: bool,
    timed_out: bool,
    exit_code: int | None,
    foreign_pg_container: bool = False,
    chokepoint_marker_ok: bool = True,
    watch_attached: bool = True,
) -> VerdictResult:
    """Apply the verdict rule. ``junit_result`` may be ``None`` when the run
    never reached pytest (``nocompile``, or the caller has nothing to parse)."""
    if nocompile:
        # SF-4: no other condition is evaluated.
        return VerdictResult(
            verdict="ERROR",
            error_reason="nocompile",
            error_reasons=["nocompile"],
            tests_collected=None,
        )

    jr = junit_result or JunitResult(tests_collected=None, parse_failed=True)
    reasons: list[str] = []

    if jr.tests_collected is not None and jr.tests_collected != tests_expected:
        reasons.append("collected")
    if not pathcheck_ok:
        reasons.append("pathcheck")
    if db_leak:
        reasons.append("db_leak")
    if foreign_pg_container:
        reasons.append("foreign_pg_container")
    if not chokepoint_marker_ok:
        # Review round 4, requirement E2: the plugin never ran at all this
        # invocation (e.g. a stray `-p no:neuter_pathcheck` reached
        # pytest_args) -- `run_pytest()` detected this from the OUTSIDE,
        # after the child exited, since the plugin can never detect its
        # own absence. Never a silent pass, regardless of exit code.
        reasons.append("chokepoint_marker_missing")
    if not watch_attached:
        # Review round 6, fixing the round-5 fail-open regression: the
        # continuous foreign-container watch's own readiness probe could
        # never PROVE the `docker events` stream was live before this
        # call's work began -- `foreign_pg_container` above can never be
        # trusted from an unproven watch, so this is its own ERROR
        # condition, never a silent pass.
        reasons.append("watch_unproven")
    if timed_out:
        reasons.append("timeout")

    setup_or_teardown = any(
        jr.outcomes.get(i, None) and jr.outcomes[i].outcome == "error"
        for i in mapped_ids
    )
    skipped = any(
        jr.outcomes.get(i, None) and jr.outcomes[i].outcome == "skipped"
        for i in mapped_ids
    )
    missing = any(i not in jr.outcomes for i in mapped_ids)
    call_exception = any(
        jr.outcomes.get(i) is not None
        and jr.outcomes[i].outcome == "failed"
        and not jr.outcomes[i].assertion_shaped
        for i in mapped_ids
    )

    if setup_or_teardown:
        reasons.append("setup_or_teardown")
    if skipped:
        reasons.append("skipped")
    if missing:
        reasons.append("missing")
    if call_exception:
        reasons.append("call_exception")
    if jr.parse_failed:
        reasons.append("junit")
    if exit_code is None or exit_code not in VALID_EXIT_CODES:
        reasons.append("exit_code")

    outcomes = {i: o.outcome for i, o in jr.outcomes.items()}
    failed = sorted(
        i
        for i in mapped_ids
        if jr.outcomes.get(i) and jr.outcomes[i].outcome == "failed"
    )
    errors = sorted(
        i
        for i in mapped_ids
        if jr.outcomes.get(i) and jr.outcomes[i].outcome == "error"
    )
    failure_msgs = {i: jr.outcomes[i].failure_msg or "" for i in failed}
    failure_types = sorted({jr.outcomes[i].failure_type or "" for i in failed})

    if reasons:
        return VerdictResult(
            verdict="ERROR",
            error_reason=reasons[0],
            error_reasons=reasons,
            tests_collected=jr.tests_collected,
            outcomes=outcomes,
            failed=failed,
            errors=errors,
            failure_msgs=failure_msgs,
            failure_types=failure_types,
        )

    if failed:
        verdict = "RED"
    elif all(
        jr.outcomes.get(i) and jr.outcomes[i].outcome == "passed" for i in mapped_ids
    ):
        verdict = "GREEN"
    else:  # pragma: no cover - unreachable: covered by `missing`/`skipped` above
        verdict = "ERROR"

    return VerdictResult(
        verdict=verdict,
        error_reason=None if verdict != "ERROR" else "junit",
        error_reasons=reasons if verdict != "ERROR" else ["junit"],
        tests_collected=jr.tests_collected,
        outcomes=outcomes,
        failed=failed,
        errors=errors,
        failure_msgs=failure_msgs,
        failure_types=failure_types,
    )
