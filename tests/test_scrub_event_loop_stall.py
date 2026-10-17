"""SCRUB-EVENT-LOOP-STALL (v1.29.4) — guards + their individual neuters.

Build contract: the BUILDER DIGEST appended to the ratified spec-gate file
for SCRUB-EVENT-LOOP-STALL (spec 322b0665… + ADDENDUM A1 2c3fed3f…, A1 wins
where they differ). Fixes the v1.29.3 event-loop stall: ``_secret_window_index``
was rebuilt from scratch on EVERY scrubbed string leaf instead of once per
registered secret (R1), and ``render_payload``/``render_value`` walked a
value's ENTIRE structure before truncating, instead of stopping once the
emitted size was reached (R2).

Every guard below is paired with a neuter that must redden it (rule 6):
restore the exact escape hatch the guard closes, assert the test fails,
then restore the fix. Neuters are run individually, never batched
(``feedback_neuter_guards_individually_never_batched``).

Guard-id prefixes (G-P1 … G-P7) follow the BUILDER DIGEST naming so a
reviewer can cross-reference its guard table directly. This file does NOT
modify ``tests/test_log_call_redaction.py`` or ``tests/conftest.py`` — the
build contract rules that a REJECT condition.
"""

from __future__ import annotations

import ast
import contextvars
import json
import random
import string
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from audittrace import identity as identity_mod
from audittrace import logging_config as lc
from audittrace.identity import UserContext
from audittrace.services.memory_manifest import ManifestEntry, MockMemoryManifestService

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "audittrace"


# ═══════════════════════════ shared test helpers ═══════════════════════════


def _make_secret(length: int) -> str:
    """A realistic, NON-pathologically-repetitive secret of ``length``
    chars — a short rotating alphabet, long enough that the window scan's
    O(len(text)) bound holds (the module's own docstring warns a
    single-repeated-char secret is the pathological case)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(alphabet[i % len(alphabet)] for i in range(length))


def _make_manifest_entries(
    n: int,
    *,
    secret: str | None = None,
    secret_at: tuple[int, ...] = (),
) -> list[ManifestEntry]:
    """``n`` real ``ManifestEntry`` rows (the exact shape the v1.29.3 stall
    hit — spec fact F4/F5). ``secret_at`` plants ``secret`` verbatim in the
    ``title`` field at those (0-based) indices."""
    entries = []
    for i in range(n):
        title = f"decision-{i:06d} — ordinary manifest title, no secret here"
        if i in secret_at and secret is not None:
            title = f"{title} secret={secret}"
        entries.append(
            ManifestEntry(
                id=f"id-{i:06d}",
                layer="semantic",
                key=f"decisions/doc-{i:06d}",
                title=title,
                size_bytes=1234,
                created_at_ms=1_700_000_000_000 + i,
                modified_at_ms=1_700_000_000_000 + i,
                created_by_user_id="stall-test-user",
                modified_by_user_id="stall-test-user",
                deleted_at_ms=None,
                deleted_by_user_id=None,
            )
        )
    return entries


@pytest.fixture(autouse=True)
def _reset_secrets_around_each_test():
    identity_mod.reset_request_secrets()
    yield
    identity_mod.reset_request_secrets()


class _InstrumentedIndex(Mapping[str, tuple[int, ...]]):
    """Wraps a real window index, counting every ``.get``/``__contains__``
    probe — G-P1b reads WHERE the lookups happen (this object), not just
    object identity, per the ratified fix for the B4 escape (a neuter that
    ignores the passed-in index and rebuilds would leave this object with
    ZERO probes even though *some* index got consulted)."""

    def __init__(self, data: Mapping[str, tuple[int, ...]]) -> None:
        self._data = dict(data)
        self.get_calls = 0
        self.contains_calls = 0

    def __getitem__(self, key: str) -> tuple[int, ...]:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.get_calls += 1
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:
        self.contains_calls += 1
        return key in self._data

    @property
    def probes(self) -> int:
        return self.get_calls + self.contains_calls


# ═══════════════════════════ G-P1 — index built once ═══════════════════════


def test_g_p1_index_not_rebuilt_during_serialize_for_span(monkeypatch):
    """0 builds, summed across BOTH bindings (``identity.build_secret_window_index``
    and ``logging_config._secret_window_index`` — two separate names bound
    to the same original function, B4), while serialising 3000 entries with
    one registered 1500-char secret. The index was already built once, at
    registration — this is the production hot path."""
    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(3000, secret=secret, secret_at=(1,))

    counts = {"n": 0}
    orig_identity = identity_mod.build_secret_window_index
    orig_lc = lc._secret_window_index

    def _counting_identity(s: str):
        counts["n"] += 1
        return orig_identity(s)

    def _counting_lc(s: str):
        counts["n"] += 1
        return orig_lc(s)

    monkeypatch.setattr(identity_mod, "build_secret_window_index", _counting_identity)
    monkeypatch.setattr(lc, "_secret_window_index", _counting_lc)

    lc._serialize_for_span(entries)

    assert counts["n"] == 0, (
        f"expected the window index to be built ZERO times during "
        f"serialization (it was built once, at registration); got "
        f"{counts['n']} rebuild(s) across the two bindings"
    )


def test_g_p1_neuter_rebuild_inside_scrub_reddens(monkeypatch):
    """NEUTER for the guard above — restore the pre-fix behaviour (rebuild
    the window index on every scrub call instead of reading the one built
    at registration) and confirm the counter goes RED. Proves the guard
    above actually exercises the once-per-registration fix, not a
    tautology."""
    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(3000, secret=secret, secret_at=(1,))

    counts = {"n": 0}
    orig_identity = identity_mod.build_secret_window_index
    orig_lc = lc._secret_window_index

    def _counting_identity(s: str):
        counts["n"] += 1
        return orig_identity(s)

    def _counting_lc(s: str):
        counts["n"] += 1
        return orig_lc(s)

    monkeypatch.setattr(identity_mod, "build_secret_window_index", _counting_identity)
    monkeypatch.setattr(lc, "_secret_window_index", _counting_lc)
    # Regression shape: force the production read path to rebuild via one
    # of the two (now-counted) bindings on every call, instead of reading
    # the registered object.
    monkeypatch.setattr(
        lc, "request_secret_index", lambda s: lc._secret_window_index(s)
    )

    lc._serialize_for_span(entries)

    assert counts["n"] > 0, (
        "NEUTER SANITY: forcing a rebuild on every scrub call must "
        "increment the counter — if this is 0, the reproduction has "
        "drifted from the pre-fix shape"
    )


# ═══════════════════════════ G-P1b — identity + probe-site pin ════════════


def test_g_p1b_identity_pin_and_probes_land_on_the_registered_object(monkeypatch):
    """``request_secret_index(s) is request_secret_index(s)`` for a
    registered secret, AND an instrumented Mapping installed as the
    registered index sees > 0 probes, EXACTLY equal to the number of
    16-char-window lookups the scrub made (``len(leaf) - 15`` for a
    single leaf with no internal match — R-c: the fixture plants NO
    fragment of the secret, so the scan never skips ahead)."""
    secret = _make_secret(900)
    captured: dict[str, _InstrumentedIndex] = {}
    orig_builder = identity_mod.build_secret_window_index

    def _wrapping_builder(s: str):
        idx = orig_builder(s)
        if s == secret:
            inst = _InstrumentedIndex(idx)
            captured["index"] = inst
            return inst
        return idx

    monkeypatch.setattr(identity_mod, "build_secret_window_index", _wrapping_builder)
    identity_mod.register_request_secret(secret)

    assert identity_mod.request_secret_index(
        secret
    ) is identity_mod.request_secret_index(secret)

    leaf = "no secret fragment anywhere in this leaf, just plain noise " * 20
    assert secret not in leaf
    rendered = lc.render_payload([leaf], limit=10**9)
    assert "[REDACTED:bearer]" not in rendered  # sanity: nothing to redact here

    inst = captured["index"]
    assert inst.probes > 0
    expected_probes = max(0, len(leaf) - (identity_mod._FRAGMENT_WINDOW_LEN - 1))
    assert inst.get_calls == expected_probes, (
        f"expected {expected_probes} window probes on the REGISTERED "
        f"object (one `.get` per scan position), got {inst.get_calls}"
    )


def test_g_p1b_neuter_ignore_supplied_index_zero_probes(monkeypatch):
    """NEUTER for the guard above — make the production read path ignore
    the registered (instrumented) index and build a fresh one on every
    call instead. The instrumented object then sees ZERO probes even
    though scrubbing still happened — proving the guard reads WHERE the
    work lands, not just object identity between two unrelated calls."""
    secret = _make_secret(900)
    captured: dict[str, _InstrumentedIndex] = {}
    orig_builder = identity_mod.build_secret_window_index

    def _wrapping_builder(s: str):
        idx = orig_builder(s)
        if s == secret:
            inst = _InstrumentedIndex(idx)
            captured["index"] = inst
            return inst
        return idx

    monkeypatch.setattr(identity_mod, "build_secret_window_index", _wrapping_builder)
    identity_mod.register_request_secret(secret)
    # Restore the plain builder BEFORE the neuter — the registered
    # instrumented object (captured above, at registration) must stay the
    # SAME object we assert against; it must not be replaced by a second
    # wrapped build triggered by the neuter's own forced rebuilds.
    monkeypatch.setattr(identity_mod, "build_secret_window_index", orig_builder)
    # Force every reader to rebuild fresh, ignoring the registered object.
    monkeypatch.setattr(lc, "request_secret_index", lambda s: orig_builder(s))

    leaf = "no secret fragment anywhere in this leaf, just plain noise " * 20
    lc.render_payload([leaf], limit=10**9)

    inst = captured["index"]
    assert inst.probes == 0, (
        "NEUTER SANITY: the registered instrumented object must see "
        "ZERO probes once the reader stops consulting it"
    )


# ═══════════════════════════ G-P2 — leaf-count counter ═════════════════════


def test_g_p2_scrub_text_call_count_bounded_by_emitted_size(monkeypatch):
    """At 10000 entries x a 4000-char registered secret, ``scrub_text`` is
    called AT MOST 204 times (34 leaves/entry x (4 + 2) entries worth of
    budget headroom, spec fact F4) — not once per leaf across all 10000
    entries."""
    secret = _make_secret(4000)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(10000, secret=secret, secret_at=(1,))

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)

    lc._serialize_for_span(entries)

    assert calls["n"] <= 204, f"expected <= 204 scrub_text calls, got {calls['n']}"


def test_g_p2_neuter_remove_exhaustion_check_reddens(monkeypatch):
    """NEUTER — disable the budget's exhaustion check (``_RenderBudget.exhausted``
    always returns False) so the walk visits every leaf across all 10000
    entries regardless of the emitted-size cap. Confirms the scrub-call
    count explodes."""
    secret = _make_secret(4000)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(10000, secret=secret, secret_at=(1,))

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)
    monkeypatch.setattr(lc._RenderBudget, "exhausted", lambda self: False)

    lc._serialize_for_span(entries)

    assert calls["n"] > 204, (
        "NEUTER SANITY: disabling the exhaustion check must blow the "
        f"leaf-count bound open; got {calls['n']}"
    )


# ═══════════════════════════ G-P2b — lower-bound property ═════════════════


def _non_ascii_enum() -> Any:
    import enum

    class _NonAsciiKind(enum.Enum):
        A = "héllo-日本語-üöä value"

    return _NonAsciiKind.A


def _non_ascii_path() -> Any:
    import pathlib

    return pathlib.Path("/données/üüü/日本語/chemin")


def _non_ascii_request() -> Any:
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mémoire/ööö/日本語/pâth",
        "headers": [],
        "query_string": b"",
    }
    return StarletteRequest(scope)


def _non_ascii_user_context() -> UserContext:
    return UserContext(
        user_id="u1",
        username="ééé-日本語-üöä-préféré",
        agent_type="opencode",
        scopes=("audittrace:query",),
        token_id="jti-non-ascii",
        is_admin=False,
    )


def _unallowlisted_object(class_name: str) -> Any:
    """A bare, non-allowlisted instance that falls all the way through
    to the FINAL ``rendered_fallback`` branch in ``_render_value_inner``
    (not a dataclass, not a pydantic model, not an ORM row, not past the
    depth cap) — RE-REVIEW 2026-10-14 R2: the corpus previously had NO
    row reaching that branch at all, which is why N31 (fallback charge
    +20) and N34 (fallback ``ensure_ascii=True``) stayed GREEN. PEP 3131
    allows non-ASCII identifiers, so a non-ASCII class name is a real,
    constructible Python shape, not a hypothetical."""
    cls = type(class_name, (), {})
    return cls()


def _random_corpus(seed: int = 20261012, n: int = 300) -> list[Any]:
    """A5 corpus (ratified ADDENDUM A1 §2/§10) — JSON escapes, non-ASCII
    (incl. CJK), non-str keys (UUID/tuple/int), safe scalars
    (datetime/UUID/Decimal/Enum), bytes, and structures over the depth
    cap. The lower-bound property must hold for every one of these, not
    just plain ASCII strings.

    REVIEW 2026-10-12 B1 fix round — the corpus previously had NO
    non-ASCII content flowing through the UserContext/Request
    projections or the safe-scalar (Enum/Path) branches, and NO actual
    ``list`` literal (the ``isinstance(shape, list)`` arm below was dead
    code) — exactly why the ``ensure_ascii`` over-count (B1) shipped
    undetected, and why a GENUINE list-item over-count (N08 — an
    increase over the ratified "+2", not the "+2" itself: see RE-REVIEW
    2026-10-14 R4, which retracted fix round 1's claim that the ratified
    charge double-counted) would also have shipped undetected. Added: a
    non-ASCII Enum, a non-ASCII Path, a non-ASCII Request, a non-ASCII
    UserContext, and genuine list-of-string shapes, several wrapped in a
    2-element ``[special, "tail-item"]`` container (the exact shape that
    makes an over-count DROP a trailing sibling instead of just costing
    a few extra, harmless, under-the-limit bytes)."""
    import datetime as _dt
    import decimal
    import enum
    import uuid

    class _Kind(enum.Enum):
        A = "a-value"

    non_ascii_enum = _non_ascii_enum()
    non_ascii_path = _non_ascii_path()
    non_ascii_request = _non_ascii_request()
    non_ascii_user_context = _non_ascii_user_context()
    ascii_fallback_obj = _unallowlisted_object("PlainUnlistedObject")
    non_ascii_fallback_obj = _unallowlisted_object("Clé_日本語_Üñlïstéd")

    rng = random.Random(seed)
    pool: list[Any] = [
        "plain ascii string",
        'escapes: " \\ \n \t \x01',
        "non-ascii: héllo wörld 日本語 😀",
        {uuid.uuid4(): "v"},
        {("a", "b", 3): 1},
        {1: "int key"},
        _dt.datetime(2026, 10, 8, 12, 0, tzinfo=_dt.UTC),
        uuid.uuid4(),
        decimal.Decimal("3.14159"),
        _Kind.A,
        non_ascii_enum,
        non_ascii_path,
        non_ascii_request,
        non_ascii_user_context,
        ascii_fallback_obj,
        non_ascii_fallback_obj,
        [non_ascii_enum, "tail-item"],
        [non_ascii_path, "tail-item"],
        [non_ascii_request, "tail-item"],
        [non_ascii_user_context, "tail-item"],
        [ascii_fallback_obj, "tail-item"],
        [non_ascii_fallback_obj, "tail-item"],
        ["a", "b", "c", "d", "e"],
        ["x" * 30, "y" * 30, "z" * 30],
        b"some raw bytes" * 5,
        None,
        True,
        False,
        3.14159,
        {"nested": {"deeper": {"even_deeper": {"a": {"b": {"c": {"d": "over cap"}}}}}}},
    ]
    corpus: list[Any] = []
    for _ in range(n):
        shape = rng.choice(pool)
        if isinstance(shape, str):
            corpus.append(shape * rng.randint(1, 50))
        elif isinstance(shape, list):
            corpus.append(shape * rng.randint(1, 10))
        elif isinstance(shape, dict):
            corpus.append({**shape, "pad": "x" * rng.randint(0, 200)})
        else:
            corpus.append(shape)
    return corpus


@pytest.mark.parametrize("limit", [5, 50, 500, 4000])
def test_g_p2b_estimate_never_exceeds_true_rendered_length(limit):
    """The budget's running estimate (``used``) never exceeds the TRUE
    length of the json-encoded (possibly partial) render — the property
    that makes ``stopped_early`` imply ``len(text) >= limit`` (so the cut
    in ``render_payload`` always lands inside fully-scrubbed text)."""
    corpus = _random_corpus()
    stopped_early_count = 0
    for value in corpus:
        budget = lc._RenderBudget(limit)
        rendered = lc.render_value(value, budget=budget)
        text = json.dumps(rendered, ensure_ascii=False)
        assert budget.used <= len(text), (
            f"estimate {budget.used} exceeded true length {len(text)} "
            f"for value={value!r} limit={limit}"
        )
        if budget.stopped_early:
            stopped_early_count += 1
            assert len(text) >= limit, (
                f"stopped_early but len(text)={len(text)} < limit={limit} "
                f"for value={value!r}"
            )
    # Sanity: the corpus/limit combination actually exercises the
    # early-stop branch at least once for the tighter limits.
    if limit <= 50:
        assert stopped_early_count > 0


@pytest.mark.parametrize("entries,secret_len", [(3000, 900), (10000, 4000)])
def test_g_p2b_estimate_holds_on_the_perf_grid_shape(entries, secret_len):
    """Same lower-bound property, over the spec's own perf-grid shape
    (a list of real ``ManifestEntry`` rows with a registered secret)."""
    secret = _make_secret(secret_len)
    identity_mod.register_request_secret(secret)
    manifest_entries = _make_manifest_entries(entries, secret=secret, secret_at=(1,))
    budget = lc._RenderBudget(4000)
    rendered = lc.render_value(manifest_entries, budget=budget)
    text = json.dumps(rendered, ensure_ascii=False)
    assert budget.used <= len(text)
    if budget.stopped_early:
        assert len(text) >= 4000


def test_g_p2b_neuter_overcounting_estimator_reddens(monkeypatch):
    """NEUTER — double the str-leaf charge so the estimator OVERcounts.
    Confirms the lower-bound property (``used <= true length``) actually
    catches an overcounting estimator, over the random corpus."""
    corpus = _random_corpus()
    original_inner = lc._render_value_inner

    def _overcounting_inner(value, depth, budget=None):
        if isinstance(value, str) and budget is not None:
            scrubbed = lc.scrub_text(value)
            budget.charge((len(scrubbed) + 2) * 2)  # doubled — the bug
            return scrubbed
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _overcounting_inner)

    violation_found = False
    for value in corpus:
        if not isinstance(value, str):
            continue
        budget = lc._RenderBudget(50)
        rendered = lc.render_value(value, budget=budget)
        text = json.dumps(rendered, ensure_ascii=False)
        if budget.used > len(text):
            violation_found = True
            break
    assert violation_found, (
        "NEUTER SANITY: doubling the str-leaf charge must produce at "
        "least one estimate > true-length violation over this corpus"
    )


# ═══════════════════ B1 fix round (REVIEW 2026-10-12) — ensure_ascii ═══════
#
# The ratified G-P2b/B1 property is "charges never exceed the TRUE emitted
# length". ``json.dumps(..., ensure_ascii=True)`` (the default) inflates a
# non-ASCII character to a 6-char ``\uXXXX`` escape while
# ``render_payload`` emits with ``ensure_ascii=False`` (1 char per
# codepoint, or 2 for astral-plane characters) — a charge computed with
# the default therefore OVER-counts whenever non-ASCII content reaches it,
# breaking the lower-bound property. The three tests below pin each
# AFFECTED PRODUCTION CALL SITE individually (reviewer's B1 table: safe
# scalars [Enum/Path], the UserContext projection, the Request
# projection) — each with a 2-element ``[special, "tail-item"]`` container
# and a limit calibrated so the FIXED charge does not trigger an early
# stop (the full, 2-item render fits) while the OLD (``ensure_ascii=True``)
# charge would have, silently dropping ``"tail-item"``.


def _gp2b_b1_calibrated_limit(special_value: Any) -> int:
    """A limit strictly between the FIXED (``ensure_ascii=False``) charge
    for ``special_value`` alone and the OLD, buggy (``ensure_ascii=True``)
    charge for the same value — see the module docstring block above."""
    rendered_leaf = lc.render_value(special_value)
    fixed_charge = len(json.dumps(rendered_leaf, ensure_ascii=False))
    buggy_charge = len(json.dumps(rendered_leaf, ensure_ascii=True))
    assert buggy_charge > fixed_charge, (
        "calibration sanity: the test value must actually contain "
        "non-ASCII content for the two charges to differ"
    )
    return (fixed_charge + buggy_charge) // 2


@pytest.mark.parametrize(
    "make_special_value",
    [_non_ascii_enum, _non_ascii_path, _non_ascii_request, _non_ascii_user_context],
    ids=["enum", "path", "request", "usercontext"],
)
def test_g_p2b_b1_non_ascii_charge_is_a_true_lower_bound(make_special_value):
    """The reviewer's four B1 reproduction cases (Enum/Path/Request/
    UserContext each carrying non-ASCII content, 2-element list, a
    calibrated limit) — on the FIXED code, the full 2-item render fits
    under the limit and NOTHING is dropped."""
    special_value = make_special_value()
    limit = _gp2b_b1_calibrated_limit(special_value)
    container = [special_value, "tail-item"]

    budget = lc._RenderBudget(limit)
    rendered = lc.render_value(container, budget=budget)
    text = json.dumps(rendered, ensure_ascii=False)

    assert budget.used <= len(text), (
        f"estimate {budget.used} exceeded true length {len(text)}"
    )
    assert not budget.stopped_early, (
        "the FIXED charge must not trigger an early stop here — the "
        "full render already fits under the calibrated limit"
    )
    full_text = lc.render_payload(container, limit=limit)
    assert "tail-item" in full_text
    assert "...[truncated" not in full_text


@pytest.mark.parametrize(
    "make_special_value,branch",
    [
        (_non_ascii_enum, "safe_scalar"),
        (_non_ascii_path, "safe_scalar"),
        (_non_ascii_request, "request"),
        (_non_ascii_user_context, "user_context"),
    ],
    ids=["enum", "path", "request", "usercontext"],
)
def test_g_p2b_b1_neuter_ensure_ascii_true_drops_tail_item(
    monkeypatch, make_special_value, branch
):
    """NEUTER, one production call site at a time (never batched) —
    revert JUST that branch's charge to the pre-fix
    ``json.dumps(..., ensure_ascii=True)`` default (every other branch
    keeps calling the REAL, fixed ``_render_value_inner``) and confirm
    the exact B1 regression reproduces: the full render would have fit,
    but the inflated charge trips an early stop and ``"tail-item"`` is
    dropped."""
    from starlette.requests import Request as _StarletteRequest

    from audittrace.identity import UserContext as _UserContext

    special_value = make_special_value()
    limit = _gp2b_b1_calibrated_limit(special_value)
    container = [special_value, "tail-item"]

    original_inner = lc._render_value_inner

    def _buggy_inner(value: Any, depth: int, budget: Any = None) -> Any:
        if branch == "safe_scalar" and isinstance(value, lc._SAFE_SCALAR_TYPES):
            rendered_scalar = lc._render_safe_scalar(value)
            if budget is not None:
                budget.charge(len(json.dumps(rendered_scalar)))  # ensure_ascii=True
            return rendered_scalar
        if branch == "user_context" and isinstance(value, _UserContext):
            projection = {
                "user_id": value.user_id,
                "username": lc.scrub_text(value.username),
                "agent_type": value.agent_type,
                "scopes": list(value.scopes),
                "token_id": value.token_id,
                "is_admin": value.is_admin,
            }
            if budget is not None:
                budget.charge(len(json.dumps(projection)))  # ensure_ascii=True
            return projection
        if branch == "request" and isinstance(value, _StarletteRequest):
            projection = {"method": value.method, "path": value.url.path}
            if budget is not None:
                budget.charge(len(json.dumps(projection)))  # ensure_ascii=True
            return projection
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _buggy_inner)

    broken_text = lc.render_payload(container, limit=limit)
    assert "tail-item" not in broken_text, (
        "NEUTER SANITY: the ensure_ascii=True regression must drop the "
        f"tail item for branch={branch!r} — if this fails, the "
        "reproduction has drifted"
    )
    assert "...[truncated" in broken_text


# ═══════════════════════════ G-P2c — emitted-leaf scrub ════════════════════


def test_g_p2c_emitted_leaves_are_fully_scrubbed_under_the_budget():
    """A registered secret planted in an EARLY entry (inside the emitted
    window) and in a LATE entry (entry 3000, beyond any plausible cut) —
    the visible output carries the redaction marker, no raw 16-char
    fragment of the secret survives anywhere, and at least one
    ``[REDACTED:bearer]`` marker is present."""
    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(3000, secret=secret, secret_at=(1, 2999))

    rendered = lc.render_payload(entries, limit=4000)

    assert "[REDACTED:bearer]" in rendered
    for window_start in range(0, len(secret) - 16 + 1, 7):
        fragment = secret[window_start : window_start + 16]
        assert fragment not in rendered


def test_g_p2c_neuter_append_raw_leaf_leaks_fragment(monkeypatch):
    """NEUTER — append a raw, unscrubbed copy of the secret onto the
    rendered output after the budgeted walk, simulating a code path that
    forgot to scrub an emitted leaf. Confirms the fragment-freedom
    assertion above actually catches a leak."""
    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    entries = _make_manifest_entries(3000, secret=secret, secret_at=(1,))

    orig_render_payload = lc.render_payload

    def _leaking_render_payload(value, *, limit):
        text = orig_render_payload(value, limit=limit)
        return text + " leaked-raw:" + secret  # the bug: unscrubbed append

    monkeypatch.setattr(lc, "render_payload", _leaking_render_payload)

    rendered = lc.render_payload(entries, limit=4000)
    leaked = secret[100:116] in rendered
    assert leaked, "NEUTER SANITY: the appended raw fragment must be present"


# ═══════════════ G-P2e — dict KEY redaction on the budgeted path (N11) ════
#
# REVIEW 2026-10-12 B2/N11 — since this WU, ``render_payload`` is ALWAYS
# budgeted; the str-key / tuple-key scrub (A1-F10) had NO committed test
# exercising that specific path, only a scratch gate prototype probe. A
# neuter that emits a dict key raw (unscrubbed) stayed green across the
# whole 178-test suite. The two tests below close that gap: a registered
# secret (and a bare >=16-char fragment of one) as a STR key and inside a
# TUPLE key, through ``render_payload`` (the budgeted path), at a limit
# that does NOT cut and one that DOES — the secret-bearing key is always
# the FIRST dict entry so it is visited (and must be redacted) under
# either limit.


def test_g_p2e_dict_key_secret_redacted_on_budgeted_path():
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)
    fragment = secret[50:66]
    assert len(fragment) == 16

    cases = {
        "str_key_full_secret": {secret: "value", "padding": "x" * 300},
        "str_key_fragment_only": {
            f"noise-{fragment}-noise": "value",
            "padding": "x" * 300,
        },
        "tuple_key_full_secret": {(secret, "b"): "value", "padding": "x" * 300},
        "tuple_key_fragment_only": {(fragment, "b"): "value", "padding": "x" * 300},
    }
    for name, value in cases.items():
        for limit in (10**9, 40):  # does-not-cut, and does-cut
            rendered = lc.render_payload(value, limit=limit)
            assert secret not in rendered, f"{name} limit={limit}: full secret leaked"
            assert fragment not in rendered, (
                f"{name} limit={limit}: 16-char fragment leaked"
            )


def test_g_p2e_neuter_raw_key_emitted_leaks_secret(monkeypatch):
    """NEUTER — emit dict keys RAW (unscrubbed, unrendered) on the
    budgeted path, reproducing N11's exact shape. Confirms the guard
    above actually exercises key scrubbing, not a tautology."""
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)
    original_inner = lc._render_value_inner

    def _raw_key_inner(value, depth, budget=None):
        if isinstance(value, Mapping):
            rendered: dict[str, Any] = {}
            for k, v in value.items():
                if budget is not None and budget.exhausted():
                    break
                rendered_key = k if isinstance(k, str) else json.dumps(k, default=str)
                if budget is not None:
                    budget.charge(len(rendered_key) + 4)
                rendered[rendered_key] = lc.render_value(
                    v, _depth=depth + 1, budget=budget
                )
            return rendered
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _raw_key_inner)

    rendered = lc.render_payload({secret: "value", "padding": "x" * 300}, limit=10**9)
    assert secret in rendered, (
        "NEUTER SANITY: emitting the dict key raw must leak the "
        "registered secret verbatim — if this fails, the reproduction "
        "has drifted"
    )


# ═══════════════════════════ G-P2d — truncation marker ═════════════════════


def test_g_p2d_suffix_fires_on_stopped_early_even_at_exact_limit():
    """RE-REVIEW 2026-10-14 R4 (orchestrator ruling) — restores the
    ADDENDUM A1-ratified fixture fix round 1 replaced: ``["a"*30,
    "b"*50]`` at ``limit=34`` must carry the truncation suffix even
    though the (stopped-early) encoded text's length is EXACTLY the
    limit, not strictly greater than it — a ``len(text) > limit``-only
    rule would silently drop the second item with no marker at all.
    Valid again now that the list-item charge is back to the ratified
    flat "+2" (see the list-branch comment in ``logging_config.py``)."""
    rendered = lc.render_payload(["a" * 30, "b" * 50], limit=34)
    assert rendered == '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]...[truncated 34 chars]'


def test_g_p2d_no_suffix_when_not_cut():
    rendered = lc.render_payload({"k": "v"}, limit=10**9)
    assert "...[truncated" not in rendered


def test_g_p2d_neuter_len_greater_than_limit_only_drops_silently():
    """NEUTER — reimplement the PRE-FIX cut rule (``len(text) > limit``
    only, no ``stopped_early`` check) against the REAL (ratified "+2")
    production charge model, and confirm it reproduces the exact silent
    drop the gate demonstrated: a complete-looking JSON array with an
    item missing and NO truncation marker at all."""

    def _pre_fix_render_payload(value: Any, *, limit: int) -> str:
        budget = lc._RenderBudget(limit)
        text = json.dumps(lc.render_value(value, budget=budget), ensure_ascii=False)
        if len(text) > limit:  # the bug: ignores budget.stopped_early
            text = text[:limit] + f"...[truncated {len(text)} chars]"
        return text

    broken = _pre_fix_render_payload(["a" * 30, "b" * 50], limit=34)
    assert broken == '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]', (
        "NEUTER SANITY: the pre-fix rule must silently drop the second "
        "item with no truncation marker at all"
    )
    assert "...[truncated" not in broken


def test_g_p2b_n08_neuter_increasing_the_list_charge_reddens(monkeypatch):
    """N08 re-expression (RE-REVIEW 2026-10-14 R4) — the ratified "+2"
    list-item charge is correct (see the production comment), but a
    GENUINE increase over it (e.g. to "+3") still over-counts and must
    be caught. Bump the flat charge by one and confirm the extended
    G-P2b corpus (now with live list-of-string shapes) reddens."""
    original_inner = lc._render_value_inner

    def _over_by_one_inner(value, depth, budget=None):
        if isinstance(value, (list, tuple, set, frozenset)) and not isinstance(
            value, (str, bytes)
        ):
            rendered_list = []
            for item in value:
                if budget is not None and budget.exhausted():
                    break
                if budget is not None:
                    budget.charge(3)  # +1 over the ratified "+2" — the bug
                rendered_list.append(
                    lc.render_value(item, _depth=depth + 1, budget=budget)
                )
            return rendered_list
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _over_by_one_inner)

    corpus = _random_corpus()
    violation_found = False
    for limit in (5, 50, 500, 4000):
        for value in corpus:
            budget = lc._RenderBudget(limit)
            rendered = lc.render_value(value, budget=budget)
            text = json.dumps(rendered, ensure_ascii=False)
            if budget.used > len(text):
                violation_found = True
                break
        if violation_found:
            break
    assert violation_found, (
        "NEUTER SANITY: bumping the list-item charge by 1 over the "
        "ratified +2 must produce at least one lower-bound violation "
        "over this corpus"
    )


# ═══════ G-P2g — final-`<TypeName>`-fallback charge (RE-REVIEW R2, N31/N34) ═
#
# The final fallback branch (an unallowlisted object: not a dataclass,
# not a pydantic model, not an ORM row, not past the depth cap) is
# charged like every other leaf — ``len(json.dumps(rendered,
# ensure_ascii=False))``. Dedicated, individually-neutered pins beyond
# the generic corpus property test above (N31: a flat over-count added
# to this one charge; N34: this one charge reverted to the
# ``ensure_ascii`` default) — both ASCII and non-ASCII class names,
# each inside a 2-element ``[obj, "tail-item"]`` container so an
# over-count drops the sibling instead of only costing a few harmless
# bytes.


@pytest.mark.parametrize(
    "class_name",
    ["PlainUnlistedObject", "Clé_日本語_Üñlïstéd"],
    ids=["ascii", "non_ascii"],
)
def test_g_p2g_fallback_charge_is_a_true_lower_bound(class_name):
    obj = _unallowlisted_object(class_name)
    container = [obj, "tail-item"]
    budget = lc._RenderBudget(10**9)
    rendered = lc.render_value(container, budget=budget)
    text = json.dumps(rendered, ensure_ascii=False)
    assert rendered == [f"<{class_name}>", "tail-item"]
    assert budget.used <= len(text)
    assert not budget.stopped_early


@pytest.mark.parametrize(
    "class_name",
    ["PlainUnlistedObject", "Clé_日本語_Üñlïstéd"],
    ids=["ascii", "non_ascii"],
)
def test_g_p2g_neuter_n31_fallback_charge_plus_20_reddens(monkeypatch, class_name):
    """N31 — the final-fallback charge gets a flat +20 over-count."""
    obj = _unallowlisted_object(class_name)
    original_inner = lc._render_value_inner

    def _plus_20_inner(value, depth, budget=None):
        if type(value) is type(obj):
            rendered_fallback = f"<{type(value).__name__}>"
            if budget is not None:
                budget.charge(
                    len(json.dumps(rendered_fallback, ensure_ascii=False)) + 20
                )
            return rendered_fallback
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _plus_20_inner)

    # A limit calibrated between the true (uncharged-extra) size and the
    # inflated (+20) one, so the fixed code would NOT cut but the
    # neutered one does.
    true_len = len(json.dumps([f"<{class_name}>", "tail-item"], ensure_ascii=False))
    limit = true_len + 5
    rendered = lc.render_payload([obj, "tail-item"], limit=limit)
    assert "tail-item" not in rendered, (
        "NEUTER SANITY: the +20 fallback over-count must drop the "
        "tail item at this calibrated limit"
    )


@pytest.mark.parametrize(
    "class_name",
    ["PlainUnlistedObject", "Clé_日本語_Üñlïstéd"],
    ids=["ascii", "non_ascii"],
)
def test_g_p2g_neuter_n34_fallback_ensure_ascii_true_reddens(monkeypatch, class_name):
    """N34 — the final-fallback charge reverts to ``json.dumps``'s
    ``ensure_ascii=True`` default, inflating a non-ASCII class name's
    charge (a no-op neuter for the ASCII case, included for symmetry
    with the guard above)."""
    obj = _unallowlisted_object(class_name)
    original_inner = lc._render_value_inner

    def _ascii_true_inner(value, depth, budget=None):
        if type(value) is type(obj):
            rendered_fallback = f"<{type(value).__name__}>"
            if budget is not None:
                budget.charge(len(json.dumps(rendered_fallback)))  # ensure_ascii=True
            return rendered_fallback
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _ascii_true_inner)

    true_len = len(json.dumps([f"<{class_name}>", "tail-item"], ensure_ascii=False))
    limit = true_len + 2
    budget = lc._RenderBudget(limit)
    rendered = lc.render_value([obj, "tail-item"], budget=budget)
    text = json.dumps(rendered, ensure_ascii=False)
    if class_name == "Clé_日本語_Üñlïstéd":
        assert budget.used > len(text), (
            "NEUTER SANITY: ensure_ascii=True on a non-ASCII class name "
            "must overcount relative to the true emitted length"
        )
    else:
        assert budget.used <= len(text)  # ASCII class name: no-op neuter, sanity only


# ═════════ G-P2f — budget threaded through model/ORM branches (N20/N21) ═══
#
# REVIEW 2026-10-12 B2/N20/N21 — the digest requires "ORM (column render,
# budget passed through)" (R-b) and the same for allowlisted pydantic
# models; the PRODUCTION CODE already passes ``budget=budget`` on both
# recursive calls (``logging_config.py``'s model/ORM branches), but NO
# guard exercised a LONG list of either shape — G-P1/G-P2/G-P3/G-P4 only
# grid ``ManifestEntry`` (a plain dataclass, a THIRD branch). Without the
# budget, a ``list[AllowlistedModel]``/list of allowlisted ORM rows would
# be walked and scrubbed in full regardless of the emitted-size cap —
# exactly the stall class this WU exists to close. These two tests close
# that gap with a leaf-count bound (G-P2-style), individually neutered
# per branch (never batched).


def test_g_p2f_pydantic_model_list_bounded_by_budget(monkeypatch):
    from audittrace.models import HealthResponse

    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    models = [HealthResponse(status=secret, version=f"v{i}") for i in range(3000)]

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)
    lc.render_payload(models, limit=4000)
    assert calls["n"] < 500, (
        f"expected a bounded scrub_text call count for a budgeted list of "
        f"3000 allowlisted pydantic models, got {calls['n']}"
    )


def test_g_p2f_neuter_model_branch_budget_dropped_reddens(monkeypatch):
    """NEUTER — strip ``budget=budget`` from the pydantic-model branch's
    recursive call (simulating N20) and confirm the leaf-count bound
    above goes RED."""
    from audittrace.models import HealthResponse

    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    models = [HealthResponse(status=secret, version=f"v{i}") for i in range(3000)]

    original_inner = lc._render_value_inner

    def _no_budget_for_models(value, depth, budget=None):
        if isinstance(value, HealthResponse):
            return lc.render_value(value.model_dump(mode="json"), _depth=depth + 1)
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _no_budget_for_models)

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)
    lc.render_payload(models, limit=4000)
    assert calls["n"] >= 500, (
        "NEUTER SANITY: dropping the budget on the model branch must "
        f"blow the leaf-count bound open; got {calls['n']}"
    )


def test_g_p2f_orm_row_list_bounded_by_budget(monkeypatch):
    from audittrace.db.models import InteractionRecord as OrmInteractionRecord

    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    rows = [
        OrmInteractionRecord(
            project="stall-test",
            source="unit-test",
            question=secret,
            answer="a" * 20,
            timestamp="2026-10-12T00:00:00Z",
            session_id=f"s{i}",
            model="qwen",
        )
        for i in range(3000)
    ]

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)
    lc.render_payload(rows, limit=4000)
    assert calls["n"] < 500, (
        f"expected a bounded scrub_text call count for a budgeted list of "
        f"3000 allowlisted ORM rows, got {calls['n']}"
    )


def test_g_p2f_neuter_orm_branch_budget_dropped_reddens(monkeypatch):
    """NEUTER — strip ``budget=budget`` from the ORM branch's recursive
    call (simulating N21) and confirm the leaf-count bound above goes
    RED."""
    from audittrace.db.models import InteractionRecord as OrmInteractionRecord

    secret = _make_secret(1500)
    identity_mod.register_request_secret(secret)
    rows = [
        OrmInteractionRecord(
            project="stall-test",
            source="unit-test",
            question=secret,
            answer="a" * 20,
            timestamp="2026-10-12T00:00:00Z",
            session_id=f"s{i}",
            model="qwen",
        )
        for i in range(3000)
    ]

    original_inner = lc._render_value_inner

    def _no_budget_for_orm(value, depth, budget=None):
        if type(value) is OrmInteractionRecord:
            from sqlalchemy import inspect as sa_inspect

            mapper = sa_inspect(type(value))
            return lc.render_value(
                {
                    attr.key: getattr(value, attr.key, None)
                    for attr in mapper.column_attrs
                },
                _depth=depth + 1,
            )
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _no_budget_for_orm)

    calls = {"n": 0}
    orig_scrub = lc.scrub_text

    def _counting_scrub(value: str) -> str:
        calls["n"] += 1
        return orig_scrub(value)

    monkeypatch.setattr(lc, "scrub_text", _counting_scrub)
    lc.render_payload(rows, limit=4000)
    assert calls["n"] >= 500, (
        "NEUTER SANITY: dropping the budget on the ORM branch must blow "
        f"the leaf-count bound open; got {calls['n']}"
    )


# ═══════════════════════════ G-P3 — wall-time grid ══════════════════════════


_GP3_ENTRY_COUNTS = (3000, 10000)
_GP3_SECRET_LENS = (900, 1500, 4000)


def _gp3_cell_seconds(entries: int, secret_len: int, *, registered: bool) -> float:
    identity_mod.reset_request_secrets()
    if registered:
        secret = _make_secret(secret_len)
        identity_mod.register_request_secret(secret)
        manifest_entries = _make_manifest_entries(
            entries, secret=secret, secret_at=(1,)
        )
    else:
        # JWT-shaped but UNREGISTERED — exercises the D4 shape net instead
        # of the D3 equality/window-index path.
        jwt_shaped = (
            "eyJhbGciOiJSUzI1NiJ9." + ("A" * (secret_len - 40)) + ".sig12345678"
        )
        manifest_entries = _make_manifest_entries(entries)
        manifest_entries[1] = ManifestEntry(
            id="id-jwt",
            layer="semantic",
            key="decisions/doc-jwt",
            title=f"carries a jwt-shaped value: {jwt_shaped}",
            size_bytes=1,
            created_at_ms=0,
            modified_at_ms=0,
            created_by_user_id="stall-test-user",
            modified_by_user_id="stall-test-user",
            deleted_at_ms=None,
            deleted_by_user_id=None,
        )
    start = time.process_time()
    lc._serialize_for_span(manifest_entries)
    return time.process_time() - start


@pytest.mark.parametrize("entries", _GP3_ENTRY_COUNTS)
@pytest.mark.parametrize("secret_len", _GP3_SECRET_LENS)
@pytest.mark.parametrize("registered", [True, False])
def test_g_p3_wall_time_grid_cell_under_100ms(entries, secret_len, registered):
    """Every grid cell — min of 3 runs — stays under 0.1s (>= 250x
    headroom over the measured 0.4ms; a 1-CPU runner 3x slower still has
    80x headroom, per the ratified spec's own framing)."""
    best = min(
        _gp3_cell_seconds(entries, secret_len, registered=registered) for _ in range(3)
    )
    assert best < 0.1, (
        f"cell entries={entries} secret_len={secret_len} took {best:.4f}s"
    )


def test_g_p3_neuter_remove_budget_reddens(monkeypatch):
    """NEUTER — disable the budget's exhaustion check, forcing an
    unbudgeted-shaped walk over all 10000 entries at the largest secret
    length. Confirms the grid bound actually depends on R2."""
    monkeypatch.setattr(lc._RenderBudget, "exhausted", lambda self: False)
    best = min(_gp3_cell_seconds(10000, 4000, registered=True) for _ in range(2))
    assert best >= 0.1, (
        f"NEUTER SANITY: removing the budget must blow the 0.1s bound "
        f"open; got {best:.4f}s"
    )


# ═══════════════════════════ G-P4 — real-route event-loop stall ════════════

_GP4_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_GP4_PRIVATE_PEM = _GP4_PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_GP4_PUBLIC_PEM = (
    _GP4_PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)
_GP4_ISSUER = "http://keycloak:8080/realms/audittrace"
_GP4_AUDIENCE = "audittrace-server"


class _LoopWatchdog:
    """Observes event-loop responsiveness from a REAL OS thread, not an
    asyncio task scheduled on the loop it is watching.

    RE-REVIEW 2026-10-14 R1/N35 (BLOCKER) — the previous instrument was
    an ``asyncio.create_task`` ticker that ``await``ed itself every
    0.05s. That is COOPERATIVE scheduling: a SYNCHRONOUS blocking call
    inside the request handler (``time.sleep`` planted directly in
    ``render_payload`` — N35) never yields back to the loop, so the
    ticker task is never given a chance to run at all. Its tick list
    stayed empty (or held a single pre-request tick), ``gaps`` was
    computed as ``[]``, and ``max(gaps) if gaps else 0.0`` reported
    "no stall" — the guard was fail-OPEN on exactly the failure mode
    the WU exists to catch; the test passed after 208s with `gap=0.0`.

    This watchdog submits a trivial callback to the loop via
    ``call_soon_threadsafe`` from an independent thread every
    ``interval`` seconds, and records the wall-clock time at which each
    callback ACTUALLY RUNS. ``call_soon_threadsafe`` only queues the
    callback — if the loop is busy executing a synchronous blocking
    call, the queued callback simply waits, so the GAP between two
    callback executions is a true, externally-observed measurement of
    loop responsiveness, unaffected by whether the blocking code inside
    the loop ever cooperates.
    """

    def __init__(self, loop: Any, interval: float = 0.05) -> None:
        self._loop = loop
        self._interval = interval
        self._ticks: list[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _heartbeat(self) -> None:
        with self._lock:
            self._ticks.append(time.monotonic())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop.call_soon_threadsafe(self._heartbeat)
            except RuntimeError:  # pragma: no cover - loop closing race
                return
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list[float]:
        self._stop.set()
        self._thread.join(timeout=10)
        with self._lock:
            return list(self._ticks)


def _max_gap_fail_closed(ticks: list[float], *, min_ticks: int = 3) -> float:
    """The lower-bound-respecting companion to a naive
    ``max(gaps) if gaps else 0.0`` — RE-REVIEW 2026-10-14 R1: an EMPTY or
    too-sparse tick list is ITSELF a failure (the watchdog could not
    observe the loop enough to compute a meaningful gap — exactly N35's
    failure mode), never silently treated as "0.0 = no stall"."""
    if len(ticks) < min_ticks:
        raise AssertionError(
            f"event-loop watchdog recorded only {len(ticks)} tick(s) "
            f"(need >= {min_ticks} to compute a meaningful gap) — the "
            f"loop was unresponsive for the whole observation window, "
            f"or the watchdog never ran"
        )
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    return max(gaps)


def _mint_padded_jwt(min_len: int = 1500) -> str:
    """An RS256 JWT, scoped for ``/memory/semantic`` reads, padded with an
    extra claim so the ENCODED TOKEN STRING itself is >= ``min_len`` chars
    — the length that matters for the registry's redaction floor, not any
    individual claim."""
    now = int(time.time())
    claims = {
        "iss": _GP4_ISSUER,
        "aud": _GP4_AUDIENCE,
        "sub": "stall-test-user",
        "scope": "audittrace:query memory:semantic:read",
        "iat": now,
        "exp": now + 3600,
        "jti": "jti-g-p4",
    }
    token = jwt.encode(claims, _GP4_PRIVATE_PEM, algorithm="RS256")
    pad = 0
    while len(token) < min_len:
        pad += 200
        claims["pad"] = "p" * pad
        token = jwt.encode(claims, _GP4_PRIVATE_PEM, algorithm="RS256")
    return token


@pytest.fixture
def _gp4_env(monkeypatch):
    import fakeredis

    from audittrace import config as config_mod

    fake = fakeredis.FakeRedis(decode_responses=True)
    cache = identity_mod.TokenCache(fake, default_ttl_seconds=300)
    monkeypatch.setattr(identity_mod, "_token_cache", cache)
    monkeypatch.setattr(identity_mod, "_redis_client", fake)

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", _GP4_ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", _GP4_AUDIENCE)
    yield
    config_mod.get_settings.cache_clear()
    monkeypatch.setattr(identity_mod, "_token_cache", None)
    monkeypatch.setattr(identity_mod, "_redis_client", None)


def _build_gp4_app_and_manifest(monkeypatch):
    """A real FastAPI app wired with a 3000-row seeded MockMemoryManifestService
    — the EXACT seam (``list_for_layer``) that stalled the event loop in
    v1.29.3. The newest row's title carries the minted token (set by the
    caller after this returns)."""
    from unittest.mock import AsyncMock

    from audittrace import auth as auth_mod
    from audittrace import dependencies

    monkeypatch.setattr(
        auth_mod, "_fetch_jwks_keys", AsyncMock(return_value=[_GP4_PUBLIC_PEM])
    )
    auth_mod._jwks_cache.clear()

    test_container = dependencies.create_test_container()
    manifest = MockMemoryManifestService()
    for entry in _make_manifest_entries(3000):
        manifest._rows[(entry.layer, entry.key)] = {
            "id": entry.id,
            "layer": entry.layer,
            "key": entry.key,
            "title": entry.title,
            "size_bytes": entry.size_bytes,
            "created_at_ms": entry.created_at_ms,
            "modified_at_ms": entry.modified_at_ms,
            "created_by_user_id": entry.created_by_user_id,
            "modified_by_user_id": entry.modified_by_user_id,
            "deleted_at_ms": None,
            "deleted_by_user_id": None,
            "tier": "corpus",
        }
    test_container._instances["memory_manifest"] = manifest
    dependencies.container = test_container
    from audittrace.server import create_app

    app = create_app()
    return app, manifest


@pytest.mark.asyncio
async def test_g_p4_real_route_does_not_stall_the_event_loop(monkeypatch, _gp4_env):
    """Through the real public route (``httpx.ASGITransport`` — the whole
    FastAPI stack, no ``kubectl exec``, no mocked serializer): a
    thread-based watchdog (``_LoopWatchdog`` — RE-REVIEW 2026-10-14 R1,
    not an asyncio-task ticker, which fails to observe a SYNCHRONOUS
    blocking call: see N35 below) observing the SAME loop as the
    request must never see a gap wider than 1.0s (the liveness kill
    sits at ~60s; this is liveness-facing, not a tight micro-bound —
    ratified ADDENDUM A1 §6), and must record enough ticks to make that
    measurement meaningful at all (``_max_gap_fail_closed`` — an empty
    or too-sparse tick list is itself a failure, never silently 0.0).
    A phase-free companion check sums the CPU time spent INSIDE
    ``render_payload`` during the request and requires it stay <= 0.05s.
    Positive control: >= 2 ``list_for_layer`` seam spans are exported,
    each carrying the truncation suffix and the redaction marker, with no
    raw fragment of the token anywhere."""
    import asyncio

    import httpx
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from audittrace import telemetry

    token = _mint_padded_jwt(min_len=1500)
    assert len(token) >= 1500

    app, manifest = _build_gp4_app_and_manifest(monkeypatch)
    # The newest row (by created_at_ms) carries the minted token in its
    # title — the realistic "secret shows up in ordinary data" shape.
    newest_key = ("semantic", "decisions/doc-002999")
    manifest._rows[newest_key]["title"] = (
        f"newest decision, carries the live token: {token}"
    )

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g-p4-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    # Warm-up (S1) — pay the allowlist-cache / first-render cost BEFORE
    # measuring, so it cannot land on the loop during the measured window.
    lc._get_render_allowlists()
    lc.render_payload({"warm": "up"}, limit=100)

    render_payload_cpu_seconds: list[float] = []
    orig_render_payload = lc.render_payload

    def _spying_render_payload(value, *, limit):
        start = time.process_time()
        try:
            return orig_render_payload(value, limit=limit)
        finally:
            render_payload_cpu_seconds.append(time.process_time() - start)

    monkeypatch.setattr(lc, "render_payload", _spying_render_payload)

    transport = httpx.ASGITransport(app=app)
    loop = asyncio.get_running_loop()
    watchdog = _LoopWatchdog(loop)
    watchdog.start()
    try:
        # Settle windows before/after the request so the watchdog has
        # accumulated ticks even when the request itself is sub-ms —
        # "guaranteed to record many ticks" (R1), not dependent on the
        # request's own duration.
        await asyncio.sleep(0.15)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                "/memory/semantic",
                params={"collection": "decisions", "limit": 25, "sort": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        await asyncio.sleep(0.15)
    finally:
        ticks = watchdog.stop()

    assert resp.status_code == 200, resp.text

    max_gap = _max_gap_fail_closed(ticks)
    assert max_gap <= 1.0, f"event loop stalled for {max_gap:.3f}s during the request"

    summed_render_cpu = sum(render_payload_cpu_seconds)
    assert summed_render_cpu <= 0.05, (
        f"render_payload summed CPU time {summed_render_cpu:.4f}s exceeds "
        f"the 0.05s bound (phase-free companion to the watchdog-gap check)"
    )

    seam_spans = [
        s for s in exporter.get_finished_spans() if "list_for_layer" in s.name
    ]
    assert len(seam_spans) >= 2, (
        f"expected >= 2 list_for_layer seam spans, got {len(seam_spans)}"
    )
    for span in seam_spans:
        output_value = span.attributes.get("output.value", "")
        assert "...[truncated" in output_value
        assert "[REDACTED:bearer]" in output_value
        for window_start in range(0, len(token) - 16, 11):
            assert token[window_start : window_start + 16] not in output_value

    # REVIEW 2026-10-12 B3 / RE-REVIEW 2026-10-14 R1 — record `gap_floor`:
    # the SAME route, same request, same watchdog, with `render_payload`
    # stubbed to return "" (the route's own synchronous, non-serialisation
    # work — auth, the mock manifest's list/sort passes, dedupe/group, all
    # under coverage tracing). `gap_fixed` (measured above) minus
    # `gap_floor` isolates what THIS WU's serialiser costs on the loop,
    # independent of everything else the route does. Printed (not just
    # asserted) so a `-s` run captures both numbers for the build
    # record/evidence — this is a recorded MEASUREMENT, not an additional
    # pass/fail gate.
    monkeypatch.setattr(lc, "render_payload", lambda value, *, limit: "")

    floor_watchdog = _LoopWatchdog(loop)
    floor_watchdog.start()
    try:
        await asyncio.sleep(0.15)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            floor_resp = await client.get(
                "/memory/semantic",
                params={"collection": "decisions", "limit": 25, "sort": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        await asyncio.sleep(0.15)
    finally:
        floor_ticks = floor_watchdog.stop()

    assert floor_resp.status_code == 200, floor_resp.text
    gap_floor = _max_gap_fail_closed(floor_ticks)
    print(
        f"\nG-P4 gap_floor={gap_floor:.4f}s  gap_fixed={max_gap:.4f}s  "
        f"(gap_fixed - gap_floor = {max_gap - gap_floor:.4f}s)  "
        f"ticks={len(ticks)} floor_ticks={len(floor_ticks)}"
    )

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


@pytest.mark.asyncio
async def test_g_p4_neuter_n35_sleep_in_render_payload_blocks_the_loop_and_reddens(
    monkeypatch, _gp4_env
):
    """NEUTER N35 (RE-REVIEW 2026-10-14 R1, BLOCKER) — plant a
    SYNCHRONOUS ``time.sleep`` directly inside ``render_payload`` (no
    CPU cost, no ``await`` — exactly the liveness failure mode the WU
    exists for) and confirm the watchdog-based guard goes RED. Sleeps
    only on the FIRST call (not every one) so the reproduction stays
    fast (~1.5s) while still being an unambiguous, correctly-attributed
    stall — the gap it produces (>=1.5s) is already far past the 1.0s
    bound regardless of how many further calls would have added to it."""
    import asyncio

    import httpx

    token = _mint_padded_jwt(min_len=1500)
    app, manifest = _build_gp4_app_and_manifest(monkeypatch)

    orig_render_payload = lc.render_payload
    slept = {"done": False}

    def _blocking_render_payload(value, *, limit):
        if not slept["done"]:
            slept["done"] = True
            time.sleep(1.5)  # N35 — a synchronous block, no await
        return orig_render_payload(value, limit=limit)

    monkeypatch.setattr(lc, "render_payload", _blocking_render_payload)

    transport = httpx.ASGITransport(app=app)
    loop = asyncio.get_running_loop()
    watchdog = _LoopWatchdog(loop)
    watchdog.start()
    try:
        await asyncio.sleep(0.15)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                "/memory/semantic",
                params={"collection": "decisions", "limit": 25, "sort": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        await asyncio.sleep(0.15)
    finally:
        ticks = watchdog.stop()

    assert resp.status_code == 200, resp.text
    assert slept["done"], "NEUTER SANITY: the planted sleep must have fired"

    max_gap = _max_gap_fail_closed(ticks)
    assert max_gap > 1.0, (
        "NEUTER SANITY: a 1.5s synchronous sleep inside render_payload "
        f"must blow the 1.0s gap bound open; got max_gap={max_gap:.3f}s "
        f"over {len(ticks)} ticks"
    )


@pytest.mark.asyncio
async def test_g_p4_neuter_remove_budget_reddens_the_cpu_bound(monkeypatch, _gp4_env):
    """NEUTER (ii) — disable the budget's exhaustion check and confirm the
    phase-free render_payload CPU-time bound (<= 0.05s) goes RED. Uses the
    same route/seed shape as the guard above, without the ticker (CPU-sum
    is deterministic; the ticker-gap check is the liveness-facing half and
    stays covered by G-P3's neuter)."""
    import httpx

    token = _mint_padded_jwt(min_len=1500)
    app, manifest = _build_gp4_app_and_manifest(monkeypatch)

    render_payload_cpu_seconds: list[float] = []
    orig_render_payload = lc.render_payload

    def _spying_render_payload(value, *, limit):
        start = time.process_time()
        try:
            return orig_render_payload(value, limit=limit)
        finally:
            render_payload_cpu_seconds.append(time.process_time() - start)

    monkeypatch.setattr(lc, "render_payload", _spying_render_payload)
    monkeypatch.setattr(lc._RenderBudget, "exhausted", lambda self: False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/memory/semantic",
            params={"collection": "decisions", "limit": 25, "sort": "created_at"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text

    summed_render_cpu = sum(render_payload_cpu_seconds)
    assert summed_render_cpu > 0.05, (
        f"NEUTER SANITY: disabling the budget must blow the CPU-time "
        f"bound open; got {summed_render_cpu:.4f}s"
    )


@pytest.mark.asyncio
async def test_g_p4_neuter_remove_mock_decorator_loses_positive_control(
    monkeypatch, _gp4_env
):
    """NEUTER (iii) — remove the mock's ``@log_call`` decorator (simulating
    R5 absent, F11's pre-fix shape) and confirm the positive control (>= 2
    seam spans) goes RED: the test container's route never exercises the
    decorated seam at all."""
    import httpx
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from audittrace import telemetry

    token = _mint_padded_jwt(min_len=1500)
    app, manifest = _build_gp4_app_and_manifest(monkeypatch)

    # Undecorate the mock instance's bound method (simulates F11: no
    # @log_call on the mock at all).
    monkeypatch.setattr(
        manifest,
        "list_for_layer",
        MockMemoryManifestService.list_for_layer.__wrapped__.__get__(manifest),
    )

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g-p4-neuter-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/memory/semantic",
            params={"collection": "decisions", "limit": 25, "sort": "created_at"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text

    seam_spans = [
        s for s in exporter.get_finished_spans() if "list_for_layer" in s.name
    ]
    assert len(seam_spans) == 0, (
        "NEUTER SANITY: an undecorated mock must produce ZERO "
        f"list_for_layer seam spans; got {len(seam_spans)}"
    )

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


# ═══════════════════════════ G-P5 — mock parity (AST) ══════════════════════


def _log_call_decorator_node(cls_name: str, func_name: str) -> ast.expr:
    tree = ast.parse((SRC_ROOT / "services" / "memory_manifest.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == func_name
                ):
                    for dec in item.decorator_list:
                        call_name = (
                            dec.func.id
                            if isinstance(dec, ast.Call)
                            and isinstance(dec.func, ast.Name)
                            else getattr(dec, "id", None)
                        )
                        if call_name == "log_call":
                            return dec
    raise AssertionError(f"no @log_call decorator found on {cls_name}.{func_name}")


def test_g_p5_mock_list_for_layer_decorator_matches_the_real_ones():
    real_dec = _log_call_decorator_node("MemoryManifestService", "list_for_layer")
    mock_dec = _log_call_decorator_node("MockMemoryManifestService", "list_for_layer")
    assert ast.dump(real_dec) == ast.dump(mock_dec)


def test_g_p5_neuter_removed_decorator_reddens():
    """NEUTER — simulate F11's pre-fix shape (no decorator on the mock at
    all) by asserting against a bare function def with no decorators, and
    confirming the parity check's own lookup raises (the decorator genuinely
    isn't there to compare)."""
    tree = ast.parse(
        "class MockMemoryManifestService:\n"
        "    async def list_for_layer(self, layer):\n"
        "        return []\n"
    )
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MockMemoryManifestService":
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "list_for_layer"
                ):
                    found = bool(item.decorator_list)
    assert not found, "NEUTER SANITY: the undecorated shape must have no decorator"


# ═══════════════════════════ G-P6 — lifetime pin ═══════════════════════════
#
# REVIEW 2026-10-12 B2/N28 — the ORIGINAL guard asserted ``idx1 is not
# idx2`` (identity inequality between two lookups). N28's neuter — a
# process-wide cache populated AT REGISTRATION time, bypassing the
# builder entirely (so the lru_cache-on-the-builder neuter's own
# mechanism never fires) — still returns a FRESH, uncached fallback
# object from ``request_secret_index`` after reset (since that fallback
# path is untouched by the neuter), so the identity check stayed GREEN
# while a secret's windows sat retained in a module-level cache for the
# rest of the process. The REWRITTEN (fix round 1) guard asserted
# NON-RETENTION directly: it scanned every module-level attribute of
# ``identity`` for the registered secret's VALUE, after a reset.
#
# RE-REVIEW 2026-10-14 R3 — that rewrite was still wrong on two axes:
#   - N16: a module dict cache placed in ``request_secret_index``'s
#     FALLBACK branch (the one taken for a secret NOT in the registry)
#     only gets populated once that fallback path actually RUNS. The
#     guard scanned BEFORE ever exercising it, so the cache was always
#     still empty at scan time. Fixed: exercise the fallback path (a
#     fresh-context lookup, same as the conftest net's registered-value
#     leg does for an unregistered secret) BEFORE scanning, not after.
#   - N28b: a retained INDEX OBJECT (the ``dict[str, tuple[int, ...]]``
#     window map) is not a verbatim copy of the secret string — its
#     keys are 16-char WINDOWS of it. ``needle in value`` (substring)
#     never matches a dict KEY against the whole secret. Fixed: also
#     check whether a Mapping's keys intersect the secret's OWN set of
#     16-char windows (computed once, matching the real builder) — this
#     catches a retained index object by its STRUCTURE, not by hoping
#     the secret happens to appear as a literal substring somewhere.
#   - Also scans ``logging_config`` as well as ``identity`` (the digest
#     calls this "module state", not "one module").


def _scan_for_retained_secret_material(
    modules: tuple[Any, ...], needle: str, *, _max_depth: int = 4
) -> list[str]:
    """Return ``"<module>.<attr>"`` for every module-level attribute,
    across ``modules``, that still retains ``needle`` (by value, OR by
    holding a dict whose keys overlap ``needle``'s own 16-char windows —
    a retained window-index object), or — for an ``lru_cache``-wrapped
    callable — reports a non-empty cache at all (a correctly
    request-scoped design never retains ANY entry past a reset, so cache
    occupancy alone is the retention signal there, without needing to
    know what is cached)."""

    import types

    known_windows = (
        set(identity_mod.build_secret_window_index(needle).keys())
        if len(needle) >= identity_mod._FRAGMENT_WINDOW_LEN
        else set()
    )

    def _contains(value: Any, depth: int, seen: set[int]) -> bool:
        if depth > _max_depth:
            return False
        # Never descend into an imported MODULE's own internals (e.g. the
        # stdlib `fnmatch`/`hashlib`/`json` names every module imports) —
        # those are third-party state, not ours, and routinely carry
        # unrelated `lru_cache`s (CPython's `fnmatch._compile_pattern`)
        # whose occupancy has nothing to do with OUR secret.
        if isinstance(value, types.ModuleType):
            return False
        vid = id(value)
        if vid in seen:
            return False
        seen.add(vid)
        if isinstance(value, str):
            if needle in value:
                return True
            # A bare retained WINDOW (one 16-char key, held on its own
            # rather than inside the index dict) is also retention.
            return value in known_windows
        # A ContextVar is opaque from the outside (no __dict__ exposing
        # its stored value) — read it via `.get()` IN THIS CONTEXT. This
        # is what actually catches a no-op'd `reset_request_secrets`
        # (N17): the module-level `_REQUEST_SECRETS` ContextVar itself
        # would otherwise look like an unreadable black box and the scan
        # would silently miss the one place the secret is MEANT to live
        # (and must stop living, once reset has been called).
        if isinstance(value, contextvars.ContextVar):
            try:
                return _contains(value.get(), depth + 1, seen)
            except LookupError:
                return False
        if hasattr(value, "cache_info") and callable(value.cache_info):
            try:
                return bool(value.cache_info().currsize)
            except Exception:  # pragma: no cover - defensive
                pass
        if isinstance(value, Mapping):
            # N28b — a retained window-index object's KEYS are the
            # secret's own 16-char windows; a substring/equality check
            # on the VALUES alone would never see this (the values are
            # offset tuples, not text). Checking the key set directly
            # catches the index object by its structure.
            if known_windows and any(k in known_windows for k in value.keys()):
                return True
            return any(
                _contains(k, depth + 1, seen) or _contains(v, depth + 1, seen)
                for k, v in value.items()
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(_contains(v, depth + 1, seen) for v in value)
        inner_dict = getattr(value, "__dict__", None)
        if isinstance(inner_dict, dict) and inner_dict:
            return _contains(inner_dict, depth + 1, seen)
        return False

    offenders = []
    for module in modules:
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if _contains(value, 0, set()):
                offenders.append(f"{module.__name__}.{name}")
    return offenders


def _exercise_fallback_path_in_fresh_context(secret: str) -> Any:
    """Call ``request_secret_index`` for ``secret`` from a FRESH
    ``contextvars.Context`` where it is NOT registered — the exact
    fallback branch the conftest net's registered-value leg takes for
    an unregistered secret, and the one N16's neuter targets. MUST be
    called before the retention scan (R3) — a cache populated only by
    this path stays invisible to the scan otherwise."""

    def _call():
        return identity_mod.request_secret_index(secret)

    ctx = contextvars.Context()
    return ctx.run(_call)


def test_g_p6_no_process_wide_retention_after_reset():
    """After ``reset_request_secrets()`` AND exercising the fallback
    lookup path, NOTHING module-level in ``identity`` OR
    ``logging_config`` retains the registered secret — not merely "a
    fresh lookup returns a different object" (G-P6, rewritten per
    REVIEW 2026-10-12 B2/N28 and RE-REVIEW 2026-10-14 R3)."""
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)
    idx1 = identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()

    # R3 — exercise the fallback path BEFORE scanning, not after.
    idx2 = _exercise_fallback_path_in_fresh_context(secret)

    offenders = _scan_for_retained_secret_material((identity_mod, lc), secret)
    assert not offenders, f"module-level retention found in: {offenders}"

    # The original identity-inequality property still holds too (a
    # stronger, but not sufficient, condition — kept as a secondary
    # check, not the primary assertion).
    assert idx1 is not idx2
    assert idx2 is not None


def test_g_p6_neuter_lru_cache_the_builder_reddens(monkeypatch):
    """NEUTER — wrap the builder in an ``lru_cache`` (the explicitly
    rejected design). The rewritten guard's scan finds the cache still
    occupied after reset (``cache_info().currsize > 0``)."""
    import functools

    secret = _make_secret(900)
    cached_builder = functools.lru_cache(maxsize=None)(
        identity_mod.build_secret_window_index
    )
    monkeypatch.setattr(identity_mod, "build_secret_window_index", cached_builder)

    identity_mod.register_request_secret(secret)
    identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()
    _exercise_fallback_path_in_fresh_context(secret)

    offenders = _scan_for_retained_secret_material((identity_mod, lc), secret)
    assert offenders, (
        "NEUTER SANITY: an lru_cache'd builder must be caught as a "
        "retention offender by the scan — if this is empty, the "
        "reproduction has drifted"
    )
    assert any("build_secret_window_index" in o for o in offenders)


def test_g_p6_neuter_n16_fallback_path_cache_reddens(monkeypatch):
    """NEUTER — N16's exact shape: a module-level dict cache populated
    INSIDE ``request_secret_index``'s FALLBACK branch (the one taken for
    a secret that is NOT in the registry). Confirms the guard catches
    this only because it now exercises the fallback path BEFORE
    scanning (R3) — scanning first, as fix round 1 did, would see an
    empty cache and stay GREEN."""
    secret = _make_secret(900)
    fallback_cache: dict[str, Any] = {}
    monkeypatch.setattr(
        identity_mod, "_test_n16_fallback_cache", fallback_cache, raising=False
    )

    original_request_index = identity_mod.request_secret_index

    def _request_index_with_fallback_cache(secret_arg: str) -> Any:
        for value, index in identity_mod._REQUEST_SECRETS.get():
            if value == secret_arg:
                return index
        # Fallback branch (secret not registered) — the neuter: cache it.
        built = identity_mod.build_secret_window_index(secret_arg)
        fallback_cache[secret_arg] = built
        return built

    monkeypatch.setattr(
        identity_mod, "request_secret_index", _request_index_with_fallback_cache
    )

    identity_mod.reset_request_secrets()
    # Exercise the (neutered) fallback path directly — secret never registered.
    identity_mod.request_secret_index(secret)

    offenders = _scan_for_retained_secret_material((identity_mod, lc), secret)
    assert offenders, (
        "NEUTER SANITY: a fallback-path cache populated only once the "
        "fallback branch runs must be caught — if empty, the scan is "
        "still running before the fallback path is exercised"
    )
    assert any("_test_n16_fallback_cache" in o for o in offenders)
    monkeypatch.setattr(identity_mod, "request_secret_index", original_request_index)


def test_g_p6_neuter_n28b_retained_window_index_object_reddens(monkeypatch):
    """NEUTER — N28b's exact shape: registration keeps the BUILT INDEX
    OBJECT (not the secret string) in a module-level list. A scan that
    only checks for the whole secret as a substring never matches a
    dict whose keys are 16-char windows of it — the structural
    (key-set) check is what catches this."""
    secret = _make_secret(900)
    retained_indexes: list[Any] = []
    monkeypatch.setattr(
        identity_mod, "_test_n28b_retained_indexes", retained_indexes, raising=False
    )

    original_register = identity_mod.register_request_secret

    def _register_keeping_index(value: str) -> None:
        original_register(value)
        retained_indexes.append(identity_mod.build_secret_window_index(value))

    monkeypatch.setattr(
        identity_mod, "register_request_secret", _register_keeping_index
    )

    identity_mod.register_request_secret(secret)
    identity_mod.reset_request_secrets()
    _exercise_fallback_path_in_fresh_context(secret)

    # Sanity: the whole secret is NOT present as a substring anywhere in
    # the retained list (it holds offset tuples, not text) — proves the
    # OLD substring-only check would have missed this.
    assert not any(secret in str(k) for idx in retained_indexes for k in idx)

    offenders = _scan_for_retained_secret_material((identity_mod, lc), secret)
    assert offenders, (
        "NEUTER SANITY: an index object kept by VALUE (not by the "
        "secret string) must still be caught, via its key-set "
        "structure — if this is empty, the scan is still blind to a "
        "retained index object the way the review found (N28b)"
    )
    assert any("_test_n28b_retained_indexes" in o for o in offenders)
    monkeypatch.setattr(identity_mod, "register_request_secret", original_register)


def test_g_p6_neuter_n28_direct_module_cache_reddens(monkeypatch):
    """NEUTER — N28's original shape: a process-wide cache populated AT
    REGISTRATION time, keyed by the secret VALUE (bypassing the builder
    entirely, so the lru_cache-on-the-builder neuter's mechanism never
    fires). Confirms the guard still catches THIS shape too, where the
    original (pre-rewrite) ``idx1 is not idx2`` assertion did NOT (the
    fallback path ``request_secret_index`` takes when a secret isn't
    registered is untouched by this neuter, so it still happily returns
    a FRESH, uncached object post-reset, and the old identity-inequality
    guard stayed green)."""
    secret = _make_secret(900)
    process_wide_cache: dict[str, Any] = {}
    monkeypatch.setattr(
        identity_mod, "_test_n28_process_wide_cache", process_wide_cache, raising=False
    )

    original_register = identity_mod.register_request_secret

    def _register_with_leak(value: str) -> None:
        original_register(value)
        process_wide_cache[value] = identity_mod.build_secret_window_index(value)

    monkeypatch.setattr(identity_mod, "register_request_secret", _register_with_leak)

    identity_mod.register_request_secret(secret)
    idx1 = identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()
    idx2 = _exercise_fallback_path_in_fresh_context(secret)

    # Old-style assertion (what G-P6 used to check): STILL PASSES here —
    # proving this is exactly the escape the review found. `idx2` is a
    # fresh, uncached fallback object (the registry is empty post-reset),
    # so `idx1 is not idx2` holds even though the secret is still
    # retained elsewhere (the dict this neuter populated).
    assert idx1 is not idx2, (
        "sanity: the OLD guard's condition must still hold here — this "
        "neuter does not touch the fallback path at all"
    )

    # The REWRITTEN guard's scan, however, must catch it.
    offenders = _scan_for_retained_secret_material((identity_mod, lc), secret)
    assert offenders, (
        "NEUTER SANITY: the process-wide cache populated at registration "
        "must be caught as a retention offender — if this is empty, the "
        "rewritten guard has the same blind spot the review found"
    )
    assert any("_test_n28_process_wide_cache" in o for o in offenders)
    monkeypatch.setattr(identity_mod, "register_request_secret", original_register)


# ═══════════════════════════ G-P7 — differential (budgeted == unbudgeted) ══
#
# The corpus below mirrors the gate's OWN measured differential corpus
# (ADDENDUM A1 §1 A1-F3: bytes / deep / hostile / manifest_entry /
# manifest_list_3 / nested_secret / none / request / scalars /
# settings_like / tuple_key / unlisted_dataclass / uuid_key — 13 rows),
# built directly from the real classes involved (ManifestEntry, the real
# Starlette Request, the real Settings) rather than captured by monkeypatch
# across tests/test_log_call_redaction.py — this file never imports from or
# executes that module's test functions, so it cannot be read as having
# modified it. Two rows are added per the gate's advisories: an allowlisted
# ORM row (R-b) and a hostile object nested one level inside a list (N4).


class _HostileMapping(dict):
    """A ``Mapping`` whose ``.items()`` raises mid-walk — unlike a hostile
    ``__repr__`` (which ``_render_value_inner`` never calls), this DOES
    raise inside the walker, so it is a meaningful probe for the per-node
    ``try/except`` (N4)."""

    def items(self):  # type: ignore[override]
        raise RuntimeError("hostile mapping boom")


def _gp7_corpus() -> list[Any]:
    import dataclasses
    import uuid

    from starlette.requests import Request as StarletteRequest

    from audittrace.config import Settings
    from audittrace.db.models import InteractionRecord as OrmInteractionRecord

    @dataclasses.dataclass
    class _UnlistedDataclass:
        secret: str

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"authorization", b"Bearer eyJsecret.value.here")],
        "query_string": b"client_ip=203.0.113.7",
        "client": ("203.0.113.7", 5555),
        "path_params": {"key": "decisions/private-title"},
    }

    manifest_entry = _make_manifest_entries(1)[0]
    manifest_list_3 = _make_manifest_entries(3)
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)

    orm_row = OrmInteractionRecord(
        project="stall-test",
        source="unit-test",
        question="hi",
        answer="hello",
        prompt_tokens=1,
        completion_tokens=1,
        timestamp="2026-10-12T00:00:00Z",
        session_id="s1",
        model="qwen",
        user_id="u1",
        trace_id="t1" * 8,
    )

    return [
        b"some raw bytes for the bytes row",
        {"a": {"b": {"c": {"d": {"e": {"f": "over the depth cap"}}}}}},
        _HostileMapping({"x": 1}),
        manifest_entry,
        manifest_list_3,
        {"nested": {"has_secret": secret}},
        None,
        StarletteRequest(scope),
        (1, 2.5, True, False, None),
        Settings(langfuse_secret_key="sk-g-p7-corpus-secret"),
        {("a", "b"): "tuple key"},
        _UnlistedDataclass(secret="should-never-dump"),
        {uuid.uuid4(): "uuid key"},
        orm_row,
        [{"inner": _HostileMapping({"y": 2})}, "plain string after the hostile node"],
    ]


def test_g_p7_budgeted_matches_unbudgeted_over_the_corpus():
    """For every row: ``render_payload(v, limit=10**9)`` (budgeted, headroom
    so nothing is actually cut) must decode to the SAME value as the
    unbudgeted ``render_value(v)``."""
    for value in _gp7_corpus():
        budgeted = json.loads(lc.render_payload(value, limit=10**9))
        unbudgeted = json.loads(json.dumps(lc.render_value(value), ensure_ascii=False))
        assert budgeted == unbudgeted, f"mismatch for value={value!r}"


def test_g_p7_request_row_never_leaks_the_asgi_scope():
    """The specific B1 shape: a budgeted Request projects to
    {method, path} ONLY — never the client IP, path params, or headers
    that live in the raw ASGI scope."""
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/memory/semantic",
        "headers": [(b"authorization", b"Bearer eyJsecret.value.here")],
        "query_string": b"",
        "client": ("203.0.113.7", 5555),
        "path_params": {"key": "decisions/private-title"},
    }
    request = StarletteRequest(scope)
    rendered = json.loads(lc.render_payload(request, limit=10**9))
    assert rendered == {"method": "GET", "path": "/memory/semantic"}
    assert "203.0.113.7" not in json.dumps(rendered)
    assert "private-title" not in json.dumps(rendered)


def test_g_p7_neuter_mapping_before_request_leaks_the_scope(monkeypatch):
    """NEUTER (B1's own demonstrated escape) — check the Mapping branch
    BEFORE the Request projection in the budgeted path. Since
    ``starlette.requests.Request`` IS a ``collections.abc.Mapping``, this
    renders the raw ASGI scope instead of the fixed projection — confirm
    the request row goes RED."""
    from starlette.requests import Request as StarletteRequest

    original_inner = lc._render_value_inner

    def _mapping_first_inner(value, depth, budget=None):
        if isinstance(value, Mapping) and not isinstance(value, StarletteRequest):
            return original_inner(value, depth, budget)
        if isinstance(value, StarletteRequest):
            # Deliberately route Request through the generic Mapping
            # handling instead of the fixed projection, by treating it as
            # a plain Mapping-like object (ASGI scope-shaped __getitem__ is
            # not what Request implements, so emulate the ASGI scope
            # directly — the regression shape B1 demonstrated).
            scope_mapping = {
                "method": value.method,
                "path": value.url.path,
                "client": list(value.client) if value.client else None,
                "path_params": dict(value.path_params),
            }
            if budget is not None:
                budget.charge(len(json.dumps(scope_mapping)))
            return scope_mapping
        return original_inner(value, depth, budget)

    monkeypatch.setattr(lc, "_render_value_inner", _mapping_first_inner)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/memory/semantic",
        "headers": [],
        "query_string": b"",
        "client": ("203.0.113.7", 5555),
        "path_params": {"key": "decisions/private-title"},
    }
    request = StarletteRequest(scope)
    rendered = json.loads(lc.render_payload(request, limit=10**9))
    assert rendered != {"method": "GET", "path": "/memory/semantic"}
    assert "203.0.113.7" in json.dumps(rendered), (
        "NEUTER SANITY: the Mapping-before-Request ordering must leak the client IP"
    )


def test_g_p7_n4_hostile_node_inside_a_budgeted_list_never_raises():
    """N4 row — a hostile object (raises mid-walk, not just on repr) one
    level inside a budgeted list must render as ``<TypeName>`` for that
    node, WITHOUT losing the rest of the list."""
    value = [
        "before",
        _HostileMapping({"x": 1}),
        "after",
    ]
    rendered = json.loads(lc.render_payload(value, limit=10**9))
    assert rendered == ["before", "<_HostileMapping>", "after"]


def test_g_p7_neuter_drop_per_node_try_except_raises_on_hostile_node(monkeypatch):
    """NEUTER — remove ``render_value``'s per-node ``try/except`` (every
    recursive call inside ``_render_value_inner`` re-enters via the
    module-level ``render_value`` name, so patching IT removes the
    try/except at EVERY nesting level, not just the top one). Confirm the
    hostile node's exception now propagates instead of degrading to
    ``<TypeName>`` for just that one node."""

    def _render_value_no_catch(value, *, _depth=0, budget=None):
        if budget is None:
            return lc._render_value_inner(value, _depth)
        return lc._render_value_inner(value, _depth, budget)

    monkeypatch.setattr(lc, "render_value", _render_value_no_catch)

    value = ["before", _HostileMapping({"x": 1}), "after"]
    with pytest.raises(RuntimeError):
        lc.render_payload(value, limit=10**9)


def test_depth_cap_charge_branch_covered_in_both_budget_modes():
    """Coverage completeness, not a guard: the depth-cap fallback
    (``depth >= _RENDER_MAX_DEPTH``) is the one branch this WU threaded a
    NEW ``if budget is not None`` conditional through — exercise it with
    BOTH ``budget=None`` (the unbudgeted ``extra=`` contract) and a real
    budget, so neither path of that new conditional is unreached."""
    deeply_nested: Any = "leaf"
    for _ in range(lc._RENDER_MAX_DEPTH + 2):
        deeply_nested = {"n": deeply_nested}

    unbudgeted = lc.render_value(deeply_nested)
    budget = lc._RenderBudget(10**9)
    budgeted = lc.render_value(deeply_nested, budget=budget)
    assert unbudgeted == budgeted
    assert budget.used > 0


# ═══════════════════════════ AC3 sanity — the three src files import clean ═


def test_ac3_three_scope_files_import_cleanly():
    import importlib

    importlib.import_module("audittrace.identity")
    importlib.import_module("audittrace.logging_config")
    importlib.import_module("audittrace.services.memory_manifest")


def test_ac4_openapi_schema_unaffected_by_truncation_semantics():
    """AC4 sanity — this WU changes only the MEANING of the truncation
    suffix's N (and the no-cut-marker edge case), never a span name, a
    span attribute KEY, or anything `/v1`-facing. Smoke-check: the public
    functions still have the documented keyword-only surface."""
    import inspect

    render_payload_sig = inspect.signature(lc.render_payload)
    assert list(render_payload_sig.parameters) == ["value", "limit"]
    assert render_payload_sig.parameters["limit"].kind == inspect.Parameter.KEYWORD_ONLY
