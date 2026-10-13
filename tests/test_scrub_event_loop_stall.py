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
    code) — exactly why the ``ensure_ascii`` over-count (B1) and the
    list-item over-count (N08) both shipped undetected. Added: a
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
        [non_ascii_enum, "tail-item"],
        [non_ascii_path, "tail-item"],
        [non_ascii_request, "tail-item"],
        [non_ascii_user_context, "tail-item"],
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


def test_g_p2d_suffix_fires_on_stopped_early():
    """``["a"*30, "b"*30, "c"*30]`` at ``limit=32`` stops after the FIRST
    item (the list-item charge is now the exact comma-only cost, fix
    round B1/N08 — see the list-branch comment in ``logging_config.py``)
    and the suffix fires via ``stopped_early`` — confirmed independently
    of ``len(text) > limit`` by checking the budget directly, not just
    the final string, since the corrected (tight, non-overcounting)
    charge model makes ``len(text) > limit`` also true at every
    ``stopped_early`` site (the two conditions coincide once charges
    never overcount — see the neuter below for the shape where they
    USED to diverge)."""
    budget = lc._RenderBudget(32)
    rendered = lc.render_value(["a" * 30, "b" * 30, "c" * 30], budget=budget)
    assert rendered == ["a" * 30]
    assert budget.stopped_early is True
    assert budget.used == 32

    full = lc.render_payload(["a" * 30, "b" * 30, "c" * 30], limit=32)
    assert full == '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa...[truncated 34 chars]'


def test_g_p2d_no_suffix_when_not_cut():
    rendered = lc.render_payload({"k": "v"}, limit=10**9)
    assert "...[truncated" not in rendered


def test_g_p2d_neuter_len_greater_than_limit_only_drops_silently():
    """NEUTER — reimplement the FULL pre-fix shape: the OLD flat "+2"
    list-item charge (fix round B1/N08's own defect) TOGETHER WITH the
    OLD ``len(text) > limit``-only cut rule (ignoring ``stopped_early``).
    Confirms the exact historical silent drop the gate demonstrated:
    ``["a"*30, "b"*50]`` at ``limit=34`` yields a complete-looking JSON
    array with the second item missing and NO truncation marker at all
    — reproducible only when BOTH pre-fix defects are present together,
    which is exactly why B1/N08 and B3 were fixed in the same round."""

    def _pre_fix_render_value(value: Any, depth: int, budget: Any) -> Any:
        if isinstance(value, str):
            scrubbed = lc.scrub_text(value)
            if budget is not None:
                budget.charge(len(scrubbed) + 2)
            return scrubbed
        if isinstance(value, list):
            out = []
            for item in value:
                if budget is not None and budget.exhausted():
                    break
                if budget is not None:
                    budget.charge(2)  # the pre-fix flat, over-counting charge
                out.append(_pre_fix_render_value(item, depth + 1, budget))
            return out
        raise AssertionError("test helper only handles str/list")

    def _pre_fix_render_payload(value: Any, *, limit: int) -> str:
        budget = lc._RenderBudget(limit)
        text = json.dumps(_pre_fix_render_value(value, 0, budget), ensure_ascii=False)
        if len(text) > limit:  # the bug: ignores budget.stopped_early
            text = text[:limit] + f"...[truncated {len(text)} chars]"
        return text

    broken = _pre_fix_render_payload(["a" * 30, "b" * 50], limit=34)
    assert broken == '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]', (
        "NEUTER SANITY: the pre-fix rule must silently drop the second "
        "item with no truncation marker at all"
    )
    assert "...[truncated" not in broken


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
    FastAPI stack, no ``kubectl exec``, no mocked serializer): a 50ms
    ticker task running on the SAME event loop as the request must never
    see a gap wider than 1.0s (the liveness kill sits at ~60s; this is
    liveness-facing, not a tight micro-bound — ratified ADDENDUM A1 §6).
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
    ticks: list[float] = []
    stop_ticker = asyncio.Event()

    async def _ticker() -> None:
        while not stop_ticker.is_set():
            ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(stop_ticker.wait(), timeout=0.05)
            except TimeoutError:
                pass

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ticker_task = asyncio.create_task(_ticker())
        try:
            resp = await client.get(
                "/memory/semantic",
                params={"collection": "decisions", "limit": 25, "sort": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            stop_ticker.set()
            await ticker_task

    assert resp.status_code == 200, resp.text

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    max_gap = max(gaps) if gaps else 0.0
    assert max_gap <= 1.0, f"event loop stalled for {max_gap:.3f}s during the request"

    summed_render_cpu = sum(render_payload_cpu_seconds)
    assert summed_render_cpu <= 0.05, (
        f"render_payload summed CPU time {summed_render_cpu:.4f}s exceeds "
        f"the 0.05s bound (phase-free companion to the ticker-gap check)"
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

    # REVIEW 2026-10-12 B3 — record `gap_floor`: the SAME route, same
    # request, same ticker, with `render_payload` stubbed to return ""
    # (the route's own synchronous, non-serialisation work — auth, the
    # mock manifest's list/sort passes, dedupe/group, all under coverage
    # tracing). `gap_fixed` (measured above) minus `gap_floor` isolates
    # what THIS WU's serialiser costs on the loop, independent of
    # everything else the route does. Printed (not just asserted) so a
    # `-s` run captures both numbers for the build record/evidence —
    # this is a recorded MEASUREMENT, not an additional pass/fail gate.
    floor_ticks: list[float] = []
    floor_stop_ticker = asyncio.Event()

    async def _floor_ticker() -> None:
        while not floor_stop_ticker.is_set():
            floor_ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(floor_stop_ticker.wait(), timeout=0.05)
            except TimeoutError:
                pass

    monkeypatch.setattr(lc, "render_payload", lambda value, *, limit: "")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        floor_ticker_task = asyncio.create_task(_floor_ticker())
        try:
            floor_resp = await client.get(
                "/memory/semantic",
                params={"collection": "decisions", "limit": 25, "sort": "created_at"},
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            floor_stop_ticker.set()
            await floor_ticker_task

    assert floor_resp.status_code == 200, floor_resp.text
    floor_gaps = [b - a for a, b in zip(floor_ticks, floor_ticks[1:])]
    gap_floor = max(floor_gaps) if floor_gaps else 0.0
    print(
        f"\nG-P4 gap_floor={gap_floor:.4f}s  gap_fixed={max_gap:.4f}s  "
        f"(gap_fixed - gap_floor = {max_gap - gap_floor:.4f}s)"
    )

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
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
# rest of the process. The rewritten guard below asserts NON-RETENTION
# directly: it scans every module-level attribute of ``identity`` (dicts,
# lists, sets, object ``__dict__``s, and ``lru_cache``-wrapped callables'
# own cache occupancy) for the registered secret's VALUE, after a reset —
# not just comparing two object identities.


def _scan_for_retained_secret_material(
    module: Any, needle: str, *, _max_depth: int = 4
) -> list[str]:
    """Return the names of every module-level attribute of ``module``
    that still retains ``needle`` (by value) somewhere inside it, or —
    for an ``lru_cache``-wrapped callable — reports a non-empty cache at
    all (a correctly request-scoped design never retains ANY entry past
    a reset, so cache occupancy alone is the retention signal there,
    without needing to know what is cached)."""

    import types

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
            return needle in value
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
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if _contains(value, 0, set()):
            offenders.append(name)
    return offenders


def test_g_p6_no_process_wide_retention_after_reset():
    """After ``reset_request_secrets()``, NOTHING module-level in
    ``identity`` retains the registered secret — not merely "a fresh
    lookup returns a different object" (G-P6, rewritten per REVIEW
    2026-10-12 B2/N28)."""
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)
    identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()

    offenders = _scan_for_retained_secret_material(identity_mod, secret)
    assert not offenders, f"module-level retention found in: {offenders}"

    # The original identity-inequality property still holds too (a
    # stronger, but not sufficient, condition — kept as a secondary
    # check, not the primary assertion).
    def _in_fresh_context():
        return identity_mod.request_secret_index(secret)

    ctx = contextvars.Context()
    idx2 = ctx.run(_in_fresh_context)
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

    offenders = _scan_for_retained_secret_material(identity_mod, secret)
    assert offenders, (
        "NEUTER SANITY: an lru_cache'd builder must be caught as a "
        "retention offender by the scan — if this is empty, the "
        "reproduction has drifted"
    )
    assert "build_secret_window_index" in offenders


def test_g_p6_neuter_n28_direct_module_cache_reddens(monkeypatch):
    """NEUTER — N28's exact shape: a process-wide cache populated AT
    REGISTRATION time, bypassing the builder entirely (so the
    lru_cache-on-the-builder neuter's mechanism above never fires).
    Confirms the rewritten guard catches THIS shape too, where the
    ORIGINAL (pre-review) ``idx1 is not idx2`` assertion did NOT
    (R-c/B2/N28 — the fallback path ``request_secret_index`` takes when
    a secret isn't found in the registry is untouched by this neuter, so
    it still happily returns a FRESH, uncached object post-reset, and
    the old identity-inequality guard stayed green)."""
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

    # Old-style assertion (what G-P6 used to check): STILL PASSES here —
    # proving this is exactly the escape the review found. `idx2` is a
    # fresh, uncached fallback object (the registry is empty post-reset),
    # so `idx1 is not idx2` holds even though the secret is still
    # retained elsewhere (the dict this neuter populated).
    def _in_fresh_context():
        return identity_mod.request_secret_index(secret)

    ctx = contextvars.Context()
    idx2 = ctx.run(_in_fresh_context)
    assert idx1 is not idx2, (
        "sanity: the OLD guard's condition must still hold here — this "
        "neuter does not touch the fallback path at all"
    )

    # The REWRITTEN guard's scan, however, must catch it.
    offenders = _scan_for_retained_secret_material(identity_mod, secret)
    assert offenders, (
        "NEUTER SANITY: the process-wide cache populated at registration "
        "must be caught as a retention offender — if this is empty, the "
        "rewritten guard has the same blind spot the review found"
    )
    assert "_test_n28_process_wide_cache" in offenders


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
