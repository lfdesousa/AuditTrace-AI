"""**AC-T-HIST** — the clock-seeded ``(lifetime × gap)`` grid over the
ACL write path's expire predicate. ACL **2b-core-A1, Y round**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` + ADDENDA Y/Z/AA/AB/
AC/AD/AE/AF).

**Why this file exists.** ADDENDUM Y-0 measured a live authorization
downgrade: a time-limited grant (``expired_at_ms`` in the future)
survived a re-grant untouched, because the write path's expire
predicate (``expired_at_ms IS NULL``) was narrower than the read
path's own active predicate (``expired_at_ms IS NULL OR expired_at_ms
> now``). ADDENDUM AB's fix makes the write path DERIVE the read
path's predicate instead of re-spelling it — but successive
STRUCTURAL pins on that derivation each fell to an escape that
preserved the code's SHAPE while breaking its BEHAVIOUR: a divergent
spelling, deleting the predicate outright, keeping the derived call
but discarding its result, a lifetime threshold, and a lapse grace —
recorded in ADDENDUM AD-1/AE-1/AF-1. **This grid is the correctness
guard those neuters proved a structural pin cannot be** (ADDENDUM
AD-1(3)): every future escape on this property is tested against THIS
file, never answered with a sixth structural pin.

**The grid.** The predicate under test reads ONLY ``expired_at_ms``
against ``now_ms``, so the grid walks both axes it can read:

* lifetime ``L`` = ``R1.expired_at_ms - R1.created_at_ms`` in
  ``{1 ms, 1 s, 1 h, 1 d, 30 d, 10 y}``
* gap ``G`` = ``s2 - R1.expired_at_ms`` in ``{0, 1 ms, 1 h, 10 y}`` —
  **lapsed**: R1 has already lapsed by the time W2 runs, and must stay
  UNTOUCHED by every later write at the same key
* near-future ``G = -1``: R1 is still active by exactly 1 ms when W2
  runs, and must be SUPERSEDED at W2's clock (ADDENDUM Y-1) — this
  samples the BOUNDARY only (one ms before R1's own scheduled expiry,
  regardless of its lifetime). It does NOT sample a predecessor that
  is SOLIDLY inside its active window with a long time still to run —
  that is the separate "still active" variant below, required
  alongside this grid, not replaced by it.

History at one key, the SITE under test (``grant`` or ``bulk``)
performs W2 and W3:

* W1 ``grant(15, expired_at_ms=T0+L)`` -> R1, at clock ``T0``
* W2 ``site(7)`` -> R2, at clock ``T0+L+G`` (``s2`` := ``R2.created_at_ms``,
  read from the EMITTED rows)
* W3 ``site(3)`` -> R3, at clock ``s2+1`` (``s3`` := ``R3.created_at_ms``,
  likewise read from the emitted rows)

**No sleep anywhere.** The write stamp's clock seam
(``audittrace.services.console_store._context.now_ms``, read at call
time by ``build_write_stamp``) is monkeypatched; every timestamp the
assertions use is read back from the EMITTED rows (mock ``_entries``;
a fresh Postgres session as the owner, under RLS) — never from the
test's own clock variable. A precondition assert on every cell —
``s2 - R1.expired_at_ms == G`` and ``R1.expired_at_ms -
R1.created_at_ms == L`` and ``s3 == s2 + 1`` — makes each cell a
MEASUREMENT of what was actually WRITTEN: a fixed sleep delta bounds
the lifetime a variant exercises (every threshold above it escapes
undetected), which is exactly why the grid walks the lifetime axis
with a clock seam instead of a sleep.

**Provenance.** Derived from the reviewer's own probe used during the
independent spec-gate review of this property's chain (verified
byte-for-byte against the reviewer's evidence before use, per that
review's own instruction to carry it "byte-for-byte or re-derive it
cell for cell") — re-derived here cell for cell against THIS repo's
own fixtures and naming conventions.

**The three required predecessor variants (ADDENDUM AD-1(1), kept
through AF):**

1. **NULL-expiry** — R1 seeded with ``expired_at_ms=None``
   (``test_ac_t_hist_null_variant_*`` below): the grid above never
   produces this (every grid cell seeds a CONCRETE ``expired_at_ms``).
   The EXISTING ``TestExpireAndInsert`` tests in
   ``tests/test_console_acl_write_path.py`` prove the analogous (a)/(b)
   shape for a TWO-write history (W1 then W2 only) — they do NOT prove
   that R1 stays untouched by a THIRD write once already expired, which
   is exactly the property the required neuters break. Per ADDENDUM
   AD-1(1)'s ORIGINAL history (not the grid's later "site performs W2
   AND W3" widening, which is specific to the clock grid): W2 is
   ALWAYS ``grant(7)``; only W3 is the site under test.
2. **The clock-seeded grid**, above (time-limited/lapsed + the
   near-future boundary sample).
3. **Time-limited, SOLIDLY still active (``now+1h``)** — R1 seeded with
   a concrete future ``expired_at_ms`` a full hour out, W2 running
   immediately (not one ms before the scheduled expiry): the grid's
   near-future cells sample the BOUNDARY only, so a predicate that
   narrows "still active" to "within a short window of its own
   scheduled expiry" (an escape that keeps the derived call, and every
   `AB-G`-visible spelling intact) passes the near-future cells — R1's
   remaining lifetime there is always exactly 1 ms — while still
   reproducing ADDENDUM Y-0's original downgrade for anything with more
   than that window left to run. Same AD-1(1) history shape as the
   NULL variant (W2 always ``grant(7)``; W3 is the site under test).
   ``test_ac_t_hist_time_limited_variant_*`` below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import sqlalchemy as sa

from audittrace.db.models import ConsoleAclEntry
from audittrace.db.rls import set_current_user_id
from audittrace.identity import UserContext
from audittrace.services.console_acl import AclGrantOp
from audittrace.services.console_acl._mock import MockConsoleAclEntriesService
from audittrace.services.console_store import _context as _clock_module
from tests.test_acl_write_path_rls import (  # noqa: F401 - write_harness is a fixture
    _ADMIN_URL,
    _OWNER,
    _SKIP_REASON,
    write_harness,
)

# The key: _OWNER (write_harness seeds "agent-1" as theirs) grants to a
# third principal, "viewer-sub-0003" — the same shape spec's Y-T1 uses.
_KEY: dict[str, Any] = {
    "principal_type": "user",
    "principal_id": "viewer-sub-0003",
    "resource_type": "agent",
    "resource_id": "agent-1",
}
_SECOND = 1_000
_HOUR = 3_600_000
_DAY = 86_400_000
_TEN_YEARS = 3653 * _DAY
_LIFETIMES: dict[str, int] = {
    "L1ms": 1,
    "L1s": _SECOND,
    "L1h": _HOUR,
    "L1d": _DAY,
    "L30d": 30 * _DAY,
    "L10y": _TEN_YEARS,
}
_GAPS: dict[str, int] = {"g0": 0, "g1": 1, "g1h": _HOUR, "g10y": _TEN_YEARS, "near": -1}
_CASES = [f"{lifetime}-{gap}" for lifetime in _LIFETIMES for gap in _GAPS]
_T0 = 1_790_000_000_000

_Row = tuple[
    int, int | None, int, int
]  # (perm_bits, expired_at_ms, updated_at_ms, created_at_ms)
_ReadFn = Callable[[], Awaitable[dict[str, _Row]]]


def _ctx(sub: str) -> UserContext:
    return UserContext(user_id=sub, username=sub, agent_type="test", scopes=())


async def _pg_rows(factory: Any, uid: str) -> dict[str, _Row]:
    """Read every ``console_acl_entries`` row visible to ``uid`` under
    RLS, from a FRESH session — never the harness's own write session,
    so a row's post-write state is read back, not merely assumed
    committed."""
    async with factory() as session:
        await session.execute(
            sa.text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": uid},
        )
        query = sa.select(
            ConsoleAclEntry.id,
            ConsoleAclEntry.perm_bits,
            ConsoleAclEntry.expired_at_ms,
            ConsoleAclEntry.updated_at_ms,
            ConsoleAclEntry.created_at_ms,
        )
        rows = (await session.execute(query)).all()
        return {
            row.id: (
                row.perm_bits,
                row.expired_at_ms,
                row.updated_at_ms,
                row.created_at_ms,
            )
            for row in rows
        }


async def _site(
    service: Any, ctx: UserContext, site: str, bits: int
) -> tuple[dict[str, Any], list[str] | None]:
    """Perform one write at ``_KEY`` through the named site. Returns the
    new row's id and, for ``bulk``, the op's own ``expired_ids`` (``None``
    for ``grant`` — the grant site's ``expired_ids`` is a single call's
    worth, read separately by the caller via the emitted rows)."""
    if site == "grant":
        row = await service.grant_permission(ctx, perm_bits=bits, **_KEY)
        return row, None
    result = await service.bulk_write_acl_entries(
        ctx, [AclGrantOp(perm_bits=bits, **_KEY)]
    )
    return {"id": result["acl_entry_ids"][0]}, list(result["expired_ids"])


async def _run_cell(
    monkeypatch: pytest.MonkeyPatch,
    service: Any,
    ctx: UserContext,
    site: str,
    case: str,
    read: _ReadFn,
    *,
    pg: bool,
) -> None:
    lifetime_name, gap_name = case.split("-")
    lifetime, gap = _LIFETIMES[lifetime_name], _GAPS[gap_name]
    clock = {"t": _T0}
    monkeypatch.setattr(_clock_module, "now_ms", lambda: clock["t"])

    if pg:
        set_current_user_id(ctx.user_id)
    try:
        r1_id = (
            await service.grant_permission(
                ctx, perm_bits=15, expired_at_ms=_T0 + lifetime, **_KEY
            )
        )["id"]
        rows = await read()
        created = rows[r1_id][1:3]
        lifetime_written = rows[r1_id][1] - rows[r1_id][3]

        clock["t"] = _T0 + lifetime + gap
        r2, ids_w2 = await _site(service, ctx, site, 7)
        r2_id = r2["id"]
        rows = await read()
        s2 = rows[r2_id][3]
        after_w2 = rows[r1_id][1:3]
        set_s2 = {row_id for row_id, values in rows.items() if values[1] == s2}

        clock["t"] = s2 + 1
        r3, ids_w3 = await _site(service, ctx, site, 3)
        r3_id = r3["id"]
        rows = await read()
        s3 = rows[r3_id][3]
        after_w3 = rows[r1_id][1:3]
        r2_after_w3 = rows[r2_id][1:3]
        set_s3 = {row_id for row_id, values in rows.items() if values[1] == s3}
    finally:
        if pg:
            set_current_user_id(None)

    gap_written = s2 - created[0]
    precondition = gap_written == gap and lifetime_written == lifetime and s3 == s2 + 1
    assert precondition, (
        f"the cell's identity is what was WRITTEN, not requested "
        f"(case={case!r} site={site!r} pg={pg}): gap_written={gap_written} "
        f"(want {gap}) lifetime_written={lifetime_written} (want {lifetime}) "
        f"s3-s2={s3 - s2} (want 1)"
    )

    if gap >= 0:
        # Lapsed: R1 has already lapsed before W2 runs, and must stay
        # UNTOUCHED by both W2 and W3 — a written history is never
        # rewritten by a later write at the same key.
        assert after_w2 == created, "RED — R1 rewritten by W2 (lapsed cell)"
        assert after_w3 == created, "RED — R1 rewritten by W3 (lapsed cell)"
        if ids_w2 is not None:
            assert ids_w2 == [], "RED — bulk expired_ids at W2 must be empty"
        if ids_w3 is not None:
            assert ids_w3 == [r2_id], "RED — bulk expired_ids at W3 must name only R2"
        assert set_s3 == {r2_id}
    else:
        # Near-future: R1 is still active by exactly 1 ms when W2 runs
        # and must be SUPERSEDED at W2's clock (ADDENDUM Y-1); R2 is
        # then superseded, in turn, by W3.
        assert after_w2 == (s2, s2), "RED — R1 must be superseded by W2 (near-future)"
        assert after_w3 == (s2, s2), "RED — R1 must not be touched again by W3"
        assert r2_after_w3 == (s3, s3), "RED — R2 must be superseded by W3"
        if ids_w2 is not None:
            assert ids_w2 == [r1_id]
        if ids_w3 is not None:
            assert ids_w3 == [r2_id]
        assert set_s2 == {r1_id}
        assert set_s3 == {r2_id}


@pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)
@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_grid_postgres(
    write_harness: Any,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    case: str,
) -> None:
    owner = _ctx(_OWNER)
    await _run_cell(
        monkeypatch,
        write_harness.service,
        owner,
        site,
        case,
        lambda: _pg_rows(write_harness.factory, owner.user_id),
        pg=True,
    )


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_grid_mock(
    client: Any, monkeypatch: pytest.MonkeyPatch, site: str, case: str
) -> None:
    service = MockConsoleAclEntriesService()

    async def _read() -> dict[str, _Row]:
        return {
            entry.id: (
                entry.perm_bits,
                entry.expired_at_ms,
                entry.updated_at_ms,
                entry.created_at_ms,
            )
            for entry in service._entries
        }

    await _run_cell(monkeypatch, service, _ctx(_OWNER), site, case, _read, pg=False)


# ── The NULL-expiry variant — AC-T-HIST's first required predecessor
# (ADDENDUM AD-1(1), "unchanged" through AF). Same three-write history,
# seeded with expired_at_ms=None instead of a future timestamp — the
# case the clock-seeded grid above never produces (every grid cell
# seeds a CONCRETE expired_at_ms). **W2 is ALWAYS grant(7)** (AD-1(1)'s
# ORIGINAL shape — never site-dependent; only W3 is the site under
# test — unlike the clock grid above, which AF widened specifically for
# ITS OWN cells). ─────────────────────────────────────────────────────


async def _run_null_cell(
    monkeypatch: pytest.MonkeyPatch,
    service: Any,
    ctx: UserContext,
    site: str,
    read: _ReadFn,
    *,
    pg: bool,
) -> None:
    clock = {"t": _T0}
    monkeypatch.setattr(_clock_module, "now_ms", lambda: clock["t"])

    if pg:
        set_current_user_id(ctx.user_id)
    try:
        r1_id = (await service.grant_permission(ctx, perm_bits=15, **_KEY))["id"]
        rows = await read()
        created = rows[r1_id][1:3]
        assert created == (None, _T0), "R1 must start NULL-expiry, updated at T0"

        clock["t"] = _T0 + 1
        r2 = await service.grant_permission(ctx, perm_bits=7, **_KEY)
        r2_id = r2["id"]
        rows = await read()
        s2 = rows[r2_id][3]
        after_w2 = rows[r1_id][1:3]

        clock["t"] = s2 + 1
        r3, ids_w3 = await _site(service, ctx, site, 3)
        r3_id = r3["id"]
        rows = await read()
        s3 = rows[r3_id][3]
        after_w3 = rows[r1_id][1:3]
        r2_after_w3 = rows[r2_id][1:3]
        set_s3 = {row_id for row_id, values in rows.items() if values[1] == s3}
    finally:
        if pg:
            set_current_user_id(None)

    assert s3 == s2 + 1, f"s3-s2={s3 - s2} (want 1)"
    assert after_w2 == (s2, s2), "RED — R1 (NULL-expiry) must be superseded by W2"
    assert after_w3 == (s2, s2), "RED — R1 must not be touched again by W3"
    assert r2_after_w3 == (s3, s3), "RED — R2 must be superseded by W3"
    if ids_w3 is not None:
        assert ids_w3 == [r2_id]
    assert set_s3 == {r2_id}


@pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)
@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_null_variant_postgres(
    write_harness: Any,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    owner = _ctx(_OWNER)
    await _run_null_cell(
        monkeypatch,
        write_harness.service,
        owner,
        site,
        lambda: _pg_rows(write_harness.factory, owner.user_id),
        pg=True,
    )


@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_null_variant_mock(
    client: Any, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    service = MockConsoleAclEntriesService()

    async def _read() -> dict[str, _Row]:
        return {
            entry.id: (
                entry.perm_bits,
                entry.expired_at_ms,
                entry.updated_at_ms,
                entry.created_at_ms,
            )
            for entry in service._entries
        }

    await _run_null_cell(monkeypatch, service, _ctx(_OWNER), site, _read, pg=False)


# ── The "still active" variant — AC-T-HIST's THIRD required
# predecessor (ADDENDUM AE-1(1), kept and NOT replaced by the grid's
# near-future boundary sample — ADDENDUM AF-1(1): "at the boundary
# Y-T1 does not sample"). R1 has a full hour left to run when W2
# supersedes it — nothing about R1 is close to lapsing. Same AD-1(1)
# history shape as the NULL variant (W2 is ALWAYS grant(7)). ─────────


async def _run_time_limited_cell(
    monkeypatch: pytest.MonkeyPatch,
    service: Any,
    ctx: UserContext,
    site: str,
    read: _ReadFn,
    *,
    pg: bool,
) -> None:
    clock = {"t": _T0}
    monkeypatch.setattr(_clock_module, "now_ms", lambda: clock["t"])

    if pg:
        set_current_user_id(ctx.user_id)
    try:
        r1_id = (
            await service.grant_permission(
                ctx, perm_bits=15, expired_at_ms=_T0 + _HOUR, **_KEY
            )
        )["id"]
        rows = await read()
        created = rows[r1_id][1:3]
        assert created == (_T0 + _HOUR, _T0), "R1 must start with a full hour left"

        clock["t"] = _T0 + 1  # W2 runs immediately — R1 still has ~1h left
        r2 = await service.grant_permission(ctx, perm_bits=7, **_KEY)
        r2_id = r2["id"]
        rows = await read()
        s2 = rows[r2_id][3]
        after_w2 = rows[r1_id][1:3]

        clock["t"] = s2 + 1
        r3, ids_w3 = await _site(service, ctx, site, 3)
        r3_id = r3["id"]
        rows = await read()
        s3 = rows[r3_id][3]
        after_w3 = rows[r1_id][1:3]
        r2_after_w3 = rows[r2_id][1:3]
        set_s3 = {row_id for row_id, values in rows.items() if values[1] == s3}
    finally:
        if pg:
            set_current_user_id(None)

    assert s3 == s2 + 1, f"s3-s2={s3 - s2} (want 1)"
    assert after_w2 == (s2, s2), (
        "RED — R1 (still an hour from its scheduled expiry) must be "
        "superseded by W2 regardless — Y-1 supersedes every active row, "
        "not only one close to lapsing"
    )
    assert after_w3 == (s2, s2), "RED — R1 must not be touched again by W3"
    assert r2_after_w3 == (s3, s3), "RED — R2 must be superseded by W3"
    if ids_w3 is not None:
        assert ids_w3 == [r2_id]
    assert set_s3 == {r2_id}


@pytest.mark.skipif(_ADMIN_URL is None, reason=_SKIP_REASON)
@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_time_limited_variant_postgres(
    write_harness: Any,  # noqa: F811 - reused fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    owner = _ctx(_OWNER)
    await _run_time_limited_cell(
        monkeypatch,
        write_harness.service,
        owner,
        site,
        lambda: _pg_rows(write_harness.factory, owner.user_id),
        pg=True,
    )


@pytest.mark.parametrize("site", ["grant", "bulk"])
async def test_ac_t_hist_time_limited_variant_mock(
    client: Any, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    service = MockConsoleAclEntriesService()

    async def _read() -> dict[str, _Row]:
        return {
            entry.id: (
                entry.perm_bits,
                entry.expired_at_ms,
                entry.updated_at_ms,
                entry.created_at_ms,
            )
            for entry in service._entries
        }

    await _run_time_limited_cell(
        monkeypatch, service, _ctx(_OWNER), site, _read, pg=False
    )
