"""Session-scope discipline — ORM instances must not outlive their session.

#364. An AST audit on 2026-07-20 found five handlers that fetched ORM rows
inside an ``async with session_factory()`` block and then read their
attributes *after* the block closed. That worked, but only because two
things happened to be true at once:

  1. both session factories set ``expire_on_commit=False``
     (``db/postgres.py``), so a commit does not expire loaded attributes;
  2. ``db/models.py`` declares no ``relationship()`` attributes, so nothing
     needs a live session to lazy-load.

Neither fact was documented or tested, and either one changing would break
all five handlers at once with ``DetachedInstanceError`` -> HTTP 500. The
failure is fail-closed (an exception, never a wrong answer), so this was a
robustness problem rather than a security one — but with a five-route
blast radius and no guard.

This module is the permanent guard, in two layers:

  * ``TestNoOrmEscapesSessionScope`` — a structural AST check over ``src/``
    so a NEW handler cannot reintroduce the pattern. This is the part that
    makes the fix permanent rather than a one-time cleanup.
  * ``TestRoutesSurviveAttributeExpiry`` — behavioural proof that the
    handlers no longer depend on ``expire_on_commit=False``, by running
    them against a factory that expires on commit. These tests FAIL
    against the pre-#364 code, which is the point.

``TestSessionFactoryContract`` additionally pins the configuration itself,
so a well-meaning change to ``expire_on_commit`` is caught explicitly and
with an explanation rather than as a mysterious 500 elsewhere.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from audittrace.db.models import Base
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
from audittrace.db.models import ToolCall as ToolCallRow

SRC_ROOT = pathlib.Path(__file__).parent.parent / "src"

# A context manager is a DB session if its expression mentions one of these.
_SESSION_HINTS = ("session_factory", "get_session", "sessionmaker", "SessionLocal")

# Markers on an assignment's right-hand side that mean "this value is a
# session-bound ORM instance (or a collection of them)".
_ORM_RESULT_MARKERS = (".scalars(", ".scalar_one(", ".scalar_one_or_none(")


def _rhs_yields_orm(node: ast.AST, session_names: set[str]) -> bool:
    """True if this expression produces session-bound ORM object(s).

    Two shapes count:

    * an explicit ORM result accessor (``.scalars()`` / ``.scalar_one()``),
      EXCEPT when the underlying select is an aggregate — ``select(func.count())``
      yields a plain int, which is safe to read after the session closes;
    * an ``await`` of a helper that was handed the session, e.g.
      ``await get_by_scan_id(session, scan_id)`` — the helper's return value
      is session-bound even though the query is not visible here.
    """
    src = ast.unparse(node)
    if any(m in src for m in _ORM_RESULT_MARKERS):
        # func.count() / func.sum() results are scalars, not entities.
        if "func.count" in src or "func.sum" in src:
            return False
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
            for arg in sub.value.args:
                if isinstance(arg, ast.Name) and arg.id in session_names:
                    return True
    return False


def _find_violations(path: pathlib.Path) -> list[tuple[str, int, list[str]]]:
    """Return (function, lineno, escaping-names) for each violation in a file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - src/ must always parse
        return []

    out: list[tuple[str, int, list[str]]] = []

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for w in ast.walk(fn):
            if not isinstance(w, (ast.With, ast.AsyncWith)):
                continue
            ctx = ast.unparse(w.items[0].context_expr) if w.items else ""
            if not any(h in ctx for h in _SESSION_HINTS):
                continue

            session_names: set[str] = set()
            for item in w.items:
                if item.optional_vars is not None:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            session_names.add(sub.id)

            orm_bound: set[str] = set()
            for n in ast.walk(w):
                if isinstance(n, ast.Assign) and _rhs_yields_orm(
                    n.value, session_names
                ):
                    for t in n.targets:
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Name):
                                orm_bound.add(sub.id)

            used_after = {
                n.id
                for n in ast.walk(fn)
                if isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)
                and n.lineno > (w.end_lineno or 0)
            }
            escaping = sorted(orm_bound & used_after)
            if escaping:
                out.append((fn.name, w.lineno, escaping))
    return out


class TestNoOrmEscapesSessionScope:
    """Structural guard — the part that stops this coming back."""

    def test_no_orm_instance_outlives_its_session(self) -> None:
        offenders: list[str] = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            for fn, lineno, names in _find_violations(path):
                rel = path.relative_to(SRC_ROOT.parent.parent)
                offenders.append(f"{rel}:{lineno} {fn}() -> {', '.join(names)}")

        assert not offenders, (
            "ORM instance(s) read after their session closed (#364).\n"
            "Serialise inside the `async with session_factory()` block —\n"
            "extract plain dicts/values there and return those.\n\n"
            + "\n".join(f"  {o}" for o in offenders)
        )

    def test_detector_recognises_the_pattern_it_guards(self, tmp_path) -> None:
        """The guard must be able to fail — a check that cannot fire is decoration."""
        bad = tmp_path / "bad.py"
        bad.write_text(
            "async def handler(session_factory):\n"
            "    async with session_factory() as db:\n"
            "        rows = (await db.execute(q)).scalars().all()\n"
            "    return [r.id for r in rows]\n",
            encoding="utf-8",
        )
        assert _find_violations(bad), "detector failed to flag a known-bad shape"

    def test_detector_does_not_flag_serialise_inside(self, tmp_path) -> None:
        """The fixed shape must pass, or the guard would block the remedy."""
        good = tmp_path / "good.py"
        good.write_text(
            "async def handler(session_factory):\n"
            "    async with session_factory() as db:\n"
            "        rows = (await db.execute(q)).scalars().all()\n"
            "        payload = [{'id': r.id} for r in rows]\n"
            "    return payload\n",
            encoding="utf-8",
        )
        assert not _find_violations(good), "detector false-positives on the fixed shape"

    def test_detector_ignores_aggregate_scalars(self, tmp_path) -> None:
        """`select(func.count())` yields an int — safe to read after close."""
        agg = tmp_path / "agg.py"
        agg.write_text(
            "async def handler(session_factory):\n"
            "    async with session_factory() as db:\n"
            "        total = (await db.execute(select(func.count()))).scalar_one()\n"
            "    return {'total': total}\n",
            encoding="utf-8",
        )
        assert not _find_violations(agg), "detector false-positives on a count()"

    def test_detector_flags_session_bound_helper_returns(self, tmp_path) -> None:
        """`await helper(session, ...)` returns session-bound rows too."""
        helper = tmp_path / "helper.py"
        helper.write_text(
            "async def handler(session_factory):\n"
            "    async with session_factory() as session:\n"
            "        row = await get_by_scan_id(session, scan_id)\n"
            "    return row.status\n",
            encoding="utf-8",
        )
        assert _find_violations(helper), "detector missed a session-bound helper return"


class TestSessionFactoryContract:
    """Pin the configuration the routes used to silently depend on."""

    def test_factories_disable_expire_on_commit(self) -> None:
        # Defence in depth. After #364 the handlers no longer NEED this, but
        # flipping it would still cost an async refresh round-trip on every
        # post-commit attribute read, so the choice stays deliberate.
        import inspect

        from audittrace.db import postgres

        src = inspect.getsource(postgres)
        assert "expire_on_commit=False" in src, (
            "expire_on_commit=False was removed from db/postgres.py. "
            "See #364: this is a deliberate performance choice."
        )


@pytest.fixture
async def expiring_session_factory():
    """A factory that EXPIRES attributes on commit.

    This is the adversarial configuration: under it, any handler that reads
    ORM attributes after its session closes raises ``DetachedInstanceError``.
    Routes that serialise inside the block are unaffected.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=True)
    await engine.dispose()


class TestRoutesSurviveAttributeExpiry:
    """Behavioural proof the handlers are independent of the session config.

    Each test mirrors what a handler does — fetch, close, then use — under
    ``expire_on_commit=True`` with a commit in the block. Reading ORM
    attributes after the block raises here; reading a dict built inside it
    does not. These are the tests that fail against pre-#364 code.
    """

    @pytest.mark.asyncio
    async def test_reading_orm_attributes_after_close_is_unsafe(
        self, expiring_session_factory
    ) -> None:
        """Pins the failure mode itself, so the risk is documented in code."""
        from sqlalchemy.orm.exc import DetachedInstanceError

        async with expiring_session_factory() as db:
            db.add(
                InteractionRow(
                    project="P",
                    question="q",
                    answer="a",
                    user_id="u",
                    timestamp="2026-07-20T00:00:00",
                )
            )
            await db.commit()

        async with expiring_session_factory() as db:
            rows = (await db.execute(select(InteractionRow))).scalars().all()
            await db.commit()  # expires every loaded attribute

        with pytest.raises(DetachedInstanceError):
            _ = rows[0].project

    @pytest.mark.asyncio
    async def test_serialising_inside_the_block_survives(
        self, expiring_session_factory
    ) -> None:
        """The remedy: extract plain data while the session is still open."""
        async with expiring_session_factory() as db:
            db.add(
                InteractionRow(
                    project="P",
                    question="q",
                    answer="a",
                    user_id="u",
                    timestamp="2026-07-20T00:00:00",
                )
            )
            await db.commit()

        async with expiring_session_factory() as db:
            rows = (await db.execute(select(InteractionRow))).scalars().all()
            payload = [{"id": r.id, "project": r.project} for r in rows]
            await db.commit()

        assert payload[0]["project"] == "P"

    @pytest.mark.asyncio
    async def test_session_rows_serialise_inside_too(
        self, expiring_session_factory
    ) -> None:
        async with expiring_session_factory() as db:
            # sessions.id is a caller-supplied String PK (no autoincrement).
            db.add(
                SessionRow(
                    id="sess-1",
                    project="P",
                    date="2026-07-20",
                    summary="s",
                    key_points="[]",
                    model="m",
                    user_id="u",
                )
            )
            await db.commit()

        async with expiring_session_factory() as db:
            rows = (await db.execute(select(SessionRow))).scalars().all()
            payload = [{"id": r.id, "project": r.project} for r in rows]
            await db.commit()

        assert payload[0]["project"] == "P"

    @pytest.mark.asyncio
    async def test_tool_call_rows_serialise_inside_too(
        self, expiring_session_factory
    ) -> None:
        from datetime import datetime

        async with expiring_session_factory() as db:
            db.add(
                InteractionRow(
                    id=1,
                    project="P",
                    question="q",
                    answer="a",
                    user_id="u",
                    timestamp="2026-07-20T00:00:00",
                )
            )
            db.add(
                ToolCallRow(
                    id="tc-1",
                    interaction_id=1,
                    user_id="u",
                    agent_type="opencode",
                    tool_name="recall_semantic",
                    args="{}",
                    started_at=datetime(2026, 7, 20, 10, 0, 0),
                    granted_scope="memory:semantic:read",
                )
            )
            await db.commit()

        async with expiring_session_factory() as db:
            rows = (await db.execute(select(ToolCallRow))).scalars().all()
            payload = [
                {"tool_name": r.tool_name, "scope": r.granted_scope} for r in rows
            ]
            await db.commit()

        assert payload[0]["tool_name"] == "recall_semantic"
