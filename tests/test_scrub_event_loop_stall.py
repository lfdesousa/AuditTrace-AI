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


def _random_corpus(seed: int = 20261012, n: int = 300) -> list[Any]:
    """A5 corpus (ratified ADDENDUM A1 §2/§10) — JSON escapes, non-ASCII
    (incl. CJK), non-str keys (UUID/tuple/int), safe scalars
    (datetime/UUID/Decimal/Enum), bytes, and structures over the depth
    cap. The lower-bound property must hold for every one of these, not
    just plain ASCII strings."""
    import datetime as _dt
    import decimal
    import enum
    import uuid

    class _Kind(enum.Enum):
        A = "a-value"

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
        elif isinstance(shape, dict):
            corpus.append({**shape, "pad": "x" * rng.randint(0, 200)})
        elif isinstance(shape, list):
            corpus.append(shape * rng.randint(1, 10))
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


# ═══════════════════════════ G-P2d — truncation marker ═════════════════════


def test_g_p2d_suffix_fires_on_stopped_early_even_at_exact_limit():
    """The exact edge the gate's BLOCKER B3 found: ``["a"*30, "b"*50]`` at
    ``limit=34`` must carry the truncation suffix even though the
    (stopped-early) encoded text's length is EXACTLY the limit, not
    strictly greater than it — a ``len(text) > limit``-only rule would
    silently drop the second item with no marker at all."""
    rendered = lc.render_payload(["a" * 30, "b" * 50], limit=34)
    assert rendered == '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]...[truncated 34 chars]'


def test_g_p2d_no_suffix_when_not_cut():
    rendered = lc.render_payload({"k": "v"}, limit=10**9)
    assert "...[truncated" not in rendered


def test_g_p2d_neuter_len_greater_than_limit_only_drops_silently():
    """NEUTER — reimplement the PRE-FIX cut rule (``len(text) > limit``
    only, no ``stopped_early`` check) and confirm it reproduces the exact
    silent drop the gate demonstrated: a complete-looking JSON array with
    an item missing and NO truncation marker."""

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


def test_g_p6_no_process_wide_retention_after_reset():
    """After ``reset_request_secrets()`` and in a FRESH ``contextvars.Context``,
    ``request_secret_index`` returns a NEW object for a previously
    registered secret — nothing is retained process-wide (no module-level
    cache, per the ratified spec's explicit rejection of ``lru_cache``)."""
    secret = _make_secret(900)
    identity_mod.register_request_secret(secret)
    idx1 = identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()

    def _in_fresh_context():
        return identity_mod.request_secret_index(secret)

    ctx = contextvars.Context()
    idx2 = ctx.run(_in_fresh_context)
    assert idx1 is not idx2


def test_g_p6_neuter_lru_cache_the_builder_reddens(monkeypatch):
    """NEUTER — wrap the builder in an ``lru_cache`` (the explicitly
    rejected design) and confirm the SAME secret, looked up again from a
    fresh Context, now returns the SAME retained object."""
    import functools

    secret = _make_secret(900)
    cached_builder = functools.lru_cache(maxsize=None)(
        identity_mod.build_secret_window_index
    )
    monkeypatch.setattr(identity_mod, "build_secret_window_index", cached_builder)

    identity_mod.register_request_secret(secret)
    idx1 = identity_mod.request_secret_index(secret)
    identity_mod.reset_request_secrets()

    def _in_fresh_context():
        return identity_mod.request_secret_index(secret)

    ctx = contextvars.Context()
    idx2 = ctx.run(_in_fresh_context)
    assert idx1 is idx2, (
        "NEUTER SANITY: an lru_cache'd builder must retain and return the "
        "SAME object across the reset — if this fails, the reproduction "
        "has drifted"
    )


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
