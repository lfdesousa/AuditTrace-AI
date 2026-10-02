"""Review round 5 should-fix: ``EXIT_DOCKER_UNAVAILABLE`` (``pg.py``)
collided with ``EXIT_DRIFT_UNACKNOWLEDGED`` (``pool.py``) -- both were 14.
A caller branching on this package's exit codes must be able to tell every
DISTINCT meaning apart; two different names sharing one integer value is a
bug every time it happens, not just the one instance found live.

Several names ARE legitimately re-declared identically across modules for
import convenience (e.g. ``EXIT_LOCK_HELD = 8`` exists in ``lock.py``,
``pool.py`` AND ``runner.py`` -- the SAME meaning, re-exported, not a
collision) -- this test groups by NAME first, so re-declaring the SAME
name/value pair in multiple modules is fine; only two DIFFERENT names
sharing one value is flagged.
"""

from __future__ import annotations

from scripts.neuter import lock, pg, pool, report, runner, spec

_MODULES = (lock, pg, pool, report, runner, spec)


def _collect_exit_codes() -> dict[str, int]:
    """name -> value, across every ``EXIT_*`` constant in every neuter
    module. If the SAME name is re-declared with a DIFFERENT value in two
    modules, that is itself a bug this collection surfaces as a plain
    AssertionError (a name must mean one thing everywhere)."""
    codes: dict[str, int] = {}
    for module in _MODULES:
        for name in dir(module):
            if not name.startswith("EXIT_"):
                continue
            value = getattr(module, name)
            if not isinstance(value, int):
                continue
            if name in codes:
                assert codes[name] == value, (
                    f"{name} is {codes[name]} in one module but {value} in "
                    f"{module.__name__} -- the SAME exit-code name must mean "
                    "the same thing everywhere"
                )
            else:
                codes[name] = value
    return codes


def test_every_named_exit_code_is_pairwise_distinct():
    codes = _collect_exit_codes()
    assert len(codes) >= 10, "sanity: expected at least 10 named exit codes"
    by_value: dict[int, list[str]] = {}
    for name, value in codes.items():
        by_value.setdefault(value, []).append(name)
    collisions = {v: names for v, names in by_value.items() if len(names) > 1}
    assert not collisions, (
        "two DIFFERENT exit-code names share the same value -- a caller can "
        f"never tell them apart: {collisions}"
    )


def test_docker_unavailable_no_longer_collides_with_drift_unacknowledged():
    """The EXACT collision found live in review round 4/5."""
    assert pg.EXIT_DOCKER_UNAVAILABLE != pool.EXIT_DRIFT_UNACKNOWLEDGED
