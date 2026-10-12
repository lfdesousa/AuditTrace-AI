"""SEC-JWT-IN-LANGFUSE-SPAN — guards + their individual neuters.

Every guard below is paired with a neuter that must redden it (rule 6):
restore the exact escape hatch the guard closes, assert the test fails,
then restore the fix. Neuters are run individually, never batched — a
batch neuter cannot tell WHICH mechanism actually caught the plant
(`feedback_neuter_guards_individually_never_batched`).

Guard-id prefixes follow the spec + addenda (A/B/C) naming so a reviewer
can cross-reference §3 of each document directly.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt

from audittrace import telemetry
from audittrace.identity import (
    UserContext,
    register_request_secret,
    request_secrets,
    reset_request_secrets,
)
from audittrace.logging_config import (
    StructuredFormatter,
    _get_render_allowlists,
    _record_span_error,
    log_call,
    render_payload,
    render_value,
    scrub_text,
)

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "audittrace"

# A 40-char registered secret shaped like NOTHING a shape-net regex would
# catch on its own — isolates the D3 equality pin from the D4 shape net.
REGISTERED_SECRET = "r3g" + "Q" * 37
assert len(REGISTERED_SECRET) >= 16 and "eyJ" not in REGISTERED_SECRET

# A synthetic JWT-shaped (but not registered) value for D4-only rows.
UNREGISTERED_JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_request_secrets()
    yield
    reset_request_secrets()


# ═══════════════════════════ G1 — structural allowlist ════════════════════


class _PlainClass:
    def __init__(self, secret: str) -> None:
        self.secret = secret


def test_g1_unknown_object_renders_as_type_name_only():
    """A plain, non-allowlisted object never dumps its attributes."""
    obj = _PlainClass(secret="should-never-appear")
    rendered = render_value(obj)
    assert rendered == "<_PlainClass>"
    assert "should-never-appear" not in rendered


def test_g1_settings_like_object_renders_as_type_name_only():
    """A Settings-shaped object with a secret field never dumps it (F5)."""
    from audittrace.config import Settings

    settings = Settings(langfuse_secret_key="sk-super-secret-value")
    rendered = render_payload(settings, limit=4000)
    assert "<Settings>" in rendered
    assert "sk-super-secret-value" not in rendered


def test_g1_request_renders_method_and_path_only():
    """starlette.Request renders {method, path} — never headers/cookies."""
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"authorization", b"Bearer eyJsecret.value.here")],
        "query_string": b"",
    }
    request = StarletteRequest(scope)
    rendered = render_value(request)
    assert rendered == {"method": "POST", "path": "/v1/chat/completions"}
    assert "authorization" not in str(rendered).lower()
    assert "eyJsecret" not in str(rendered)


def test_g1_user_context_never_renders_extra_pii():
    ctx = UserContext(
        user_id="u1",
        username="luis",
        agent_type="opencode",
        scopes=("audittrace:query",),
        token_id="jti-1",
        is_admin=False,
        extra={"email": "luis@example.com", "name": "Luis"},
    )
    rendered = render_value(ctx)
    assert rendered == {
        "user_id": "u1",
        "username": "luis",
        "agent_type": "opencode",
        "scopes": ["audittrace:query"],
        "token_id": "jti-1",
        "is_admin": False,
    }
    assert "luis@example.com" not in str(rendered)


def test_g1_non_allowlisted_dataclass_renders_as_type_name_only():
    """V5 (gate REJECT round 2, should-fix) — a plain, non-allowlisted
    DATACLASS must render exactly like the plain-object case above: bare
    type name, nothing dumped. ``render_value``'s dataclass branch
    (``is_dataclass(value) and type(value) in render_dataclasses``)
    already guards this; this closes the missing-TEST gap the
    reviewer's neuter table found GREEN."""
    import dataclasses

    @dataclasses.dataclass
    class _UnlistedDataclass:
        secret: str
        other: int = 0

    obj = _UnlistedDataclass(secret="should-never-dump", other=7)
    rendered = render_value(obj)
    assert rendered == "<_UnlistedDataclass>"
    assert "should-never-dump" not in str(rendered)


def test_g1_neuter_dataclass_allowlist_check_removed_dumps_secret(monkeypatch):
    """NEUTER for the guard above — restore the pre-allowlist behaviour
    (dump EVERY dataclass's fields unconditionally) and confirm the
    secret leaks, proving the test above actually exercises the
    allowlist gate, not a tautology."""
    import dataclasses

    import audittrace.logging_config as lc

    @dataclasses.dataclass
    class _UnlistedDataclass:
        secret: str

    original = lc._render_value_inner

    def _dump_every_dataclass(value: Any, depth: int) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        return original(value, depth)

    monkeypatch.setattr(lc, "_render_value_inner", _dump_every_dataclass)
    obj = _UnlistedDataclass(secret="should-never-dump")
    rendered = lc.render_value(obj)
    assert "should-never-dump" in str(rendered), (
        "NEUTER SANITY: an unconditional dataclass dump must leak the "
        "secret — if this fails, the reproduction has drifted"
    )


# ═══════════════════════════ G1b/G1c — model allowlist ════════════════════


def test_g1b_settings_passed_by_keyword_still_renders_type_name_only():
    """A Settings instance passed BY KEYWORD to a decorated stub must still
    render as <Settings> — the self-detection heuristic that USED to mask
    this (treating a positional Settings as `self`) never fires for a
    keyword-passed one (spec fact F5's exact escape)."""
    from audittrace.config import Settings

    calls: dict[str, Any] = {}

    @log_call(logger=logging.getLogger("test.g1b"))
    def bootstrap(*, settings: Settings) -> None:
        return None

    secret_settings = Settings(langfuse_secret_key="sk-keyword-secret")
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True

    captured = {}

    def _capture_set_current_span_attributes(attrs):
        captured.update(attrs)

    import audittrace.telemetry as telemetry_mod

    orig = telemetry_mod.set_current_span_attributes
    telemetry_mod.set_current_span_attributes = _capture_set_current_span_attributes
    try:
        bootstrap(settings=secret_settings)
    finally:
        telemetry_mod.set_current_span_attributes = orig

    assert "sk-keyword-secret" not in str(captured)
    assert "<Settings>" in captured.get("input.value", "")
    del calls


def _secret_name_substrings() -> tuple[str, ...]:
    return ("token", "secret", "password", "api_key", "authorization", "cookie")


_SECRET_FIELD_NAMES_ALLOWED = {"token_id"}  # a jti, not a credential (D2)


def _field_name_is_secret_shaped(name: str) -> bool:
    """Substring match, NOT a ``\\b``-bounded regex (gate REJECT round 1
    BLOCKER 4) — ``\\btoken\\b`` never matches inside ``access_token``
    because ``_`` is a regex WORD character, so the old check had no
    boundary on token's left. A plain substring search has no such gap.

    One deliberate carve-out: LLM usage-token COUNTS (``token_count``,
    ``prompt_tokens``, ``completion_tokens`` — real fields on allowlisted
    models) are legitimate, non-credential integers. They are
    distinguished from a credential ``token`` by being PLURAL or a
    ``_count`` compound; a credential token field is always singular
    (``access_token``, ``refresh_token``, bare ``token``)."""
    if name in _SECRET_FIELD_NAMES_ALLOWED:
        return False
    lowered = name.lower()
    other_secret_hit = any(
        s in lowered for s in _secret_name_substrings() if s != "token"
    )
    if lowered.endswith("tokens") or lowered.endswith("token_count"):
        return other_secret_hit
    return other_secret_hit or "token" in lowered


def _field_annotation_is_secret(annotation: Any) -> bool:
    """True for ``SecretStr`` AND the ``SecretStr | None`` union shape
    (gate REJECT round 1 BLOCKER 4 — derive from type annotations, not
    identity-only against the bare ``SecretStr`` type, which misses the
    union)."""
    import typing

    from pydantic import SecretStr

    if annotation is SecretStr:
        return True
    return SecretStr in typing.get_args(annotation)


def test_g1c_allowlisted_classes_carry_no_secret_shaped_field():
    """Every allowlisted model/dataclass must have zero SecretStr-typed
    fields and zero secret-shaped-NAMED fields — the allowlist's own
    safety invariant. Uses substring name-matching + type-annotation
    introspection (see helpers above), not a word-bounded regex."""
    render_models, render_dataclasses, render_orm = _get_render_allowlists()
    violations: list[str] = []
    for cls in (*render_models, *render_dataclasses):
        fields = getattr(cls, "model_fields", None)
        if fields is not None:
            for name, field in fields.items():
                if _field_annotation_is_secret(field.annotation):
                    violations.append(f"{cls.__name__}.{name}: SecretStr-typed")
                if _field_name_is_secret_shaped(name):
                    violations.append(f"{cls.__name__}.{name}: secret-shaped name")
        else:
            import dataclasses as _dc

            for f in _dc.fields(cls):
                if _field_name_is_secret_shaped(f.name):
                    violations.append(f"{cls.__name__}.{f.name}: secret-shaped name")
    for cls in render_orm:
        from sqlalchemy import inspect as _sa_inspect

        for attr in _sa_inspect(cls).column_attrs:
            if _field_name_is_secret_shaped(attr.key):
                violations.append(f"{cls.__name__}.{attr.key}: secret-shaped name")
    assert not violations, (
        f"allowlisted classes carry secret-shaped fields: {violations}"
    )


def test_g1c_detection_helpers_catch_every_known_settings_secret_shape():
    """Gate REJECT round 1 BLOCKER 4 — directly prove the two detection
    helpers G1c relies on are not blind to any of the shapes the
    coordinator named: the 7 real ``Settings`` secret fields (by NAME —
    ``Settings`` itself is never allowlisted, so this is a test of the
    DETECTION LOGIC's reach, not of ``Settings`` membership), plus the
    hypothetical ``access_token``/``refresh_token``/``client_secret``
    names and the ``SecretStr | None`` union TYPE shape."""
    import typing

    from pydantic import SecretStr

    known_settings_secret_fields = (
        "chroma_token",
        "postgres_password",
        "langfuse_secret_key",
        "redis_password",
        "minio_secret_key",
        "aws_secret_access_key",
        "decision_api_key",
    )
    assert len(known_settings_secret_fields) == 7
    for name in known_settings_secret_fields:
        assert _field_name_is_secret_shaped(name), f"{name} not detected by name"

    for name in ("access_token", "refresh_token", "client_secret"):
        assert _field_name_is_secret_shaped(name), f"{name} not detected by name"

    assert _field_annotation_is_secret(SecretStr)
    assert _field_annotation_is_secret(SecretStr | None)
    assert _field_annotation_is_secret(typing.Optional[SecretStr])  # noqa: UP045
    assert not _field_annotation_is_secret(str | None)
    assert not _field_annotation_is_secret(str)


# ═══════════════════════════ G1d — safe scalars, never raises ═════════════


def test_g1d_safe_scalars_render_by_shape():
    import datetime as _dt
    import decimal
    import enum
    import pathlib
    import uuid

    class _Kind(enum.Enum):
        A = "a-value"

    assert render_value(_dt.datetime(2026, 10, 8, 12, 0, tzinfo=_dt.UTC)) == (
        "2026-10-08T12:00:00+00:00"
    )
    assert render_value(_dt.date(2026, 10, 8)) == "2026-10-08"
    assert render_value(uuid.UUID(int=0)) == "00000000-0000-0000-0000-000000000000"
    assert render_value(decimal.Decimal("1.50")) == "1.50"
    assert render_value(pathlib.Path("/x/y")) == "/x/y"
    assert render_value(_Kind.A) == "a-value"


def test_g1d_render_never_raises_on_hostile_repr():
    class _Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

        def __str__(self) -> str:
            raise RuntimeError("boom")

    rendered = render_value(_Hostile())
    assert rendered == "<_Hostile>"


# ═══════════════════════════ F1 — non-str dict keys (regression) ══════════
#
# Gate REJECT round 1 BLOCKER 5 — ``render_value``'s Mapping branch used to
# leave a non-str key (a ``UUID``, a ``tuple`` — both valid Python dict
# keys, neither a valid JSON key) UNCHANGED, so the final ``json.dumps``
# downstream (no ``default=``, by design — N4) raised ``TypeError: keys
# must be str...`` and the WHOLE log line / span attribute was lost, not
# just the one field — exactly the fail-OPEN-via-exception N4 promises
# never happens. Keys now route through ``render_value`` too (so a UUID
# key is scrubbed like any other string) and are stringified for JSON-key
# safety.


def test_f1_render_value_handles_uuid_dict_key():
    import uuid

    rendered = render_value({uuid.UUID(int=1): "v"})
    assert rendered == {"00000000-0000-0000-0000-000000000001": "v"}


def test_f1_render_value_handles_tuple_dict_key():
    rendered = render_value({("a", "b"): 1})
    assert rendered == {'["a", "b"]': 1}


def test_f1_render_payload_does_not_raise_on_non_str_dict_keys():
    import uuid

    payload = {uuid.UUID(int=2): "v", ("a", "b"): 1}
    rendered = render_payload(payload, limit=4000)  # must not raise
    assert "TypeError" not in rendered
    assert isinstance(rendered, str)


def test_f1_log_call_decorated_function_survives_a_non_str_dict_key_argument():
    """End to end through the real @log_call seam (not just render_value
    in isolation) — the exact shape of the reviewer's probe_render.py
    repro: a decorated function called with a UUID-keyed mapping
    argument must still run its body and log successfully."""
    import uuid

    called: list[int] = []

    @log_call(logger=logging.getLogger("test.f1"))
    def business(mapping):
        called.append(1)
        return "done"

    result = business(mapping={uuid.UUID(int=2): 1})
    assert result == "done"
    assert called == [1]


# ═══════════════════════════ G2 — request-credential registry ═════════════


@pytest.mark.parametrize(
    "position",
    ["positional", "kwarg", "nested_dict", "list_element", "return_value"],
)
def test_g2_registered_secret_scrubbed_everywhere(position, monkeypatch):
    """A registered, non-JWT-shaped secret is scrubbed by VALUE EQUALITY
    regardless of where it appears — isolating D3 from D4 by disabling
    the shape net (the secret here is deliberately not JWT-shaped, so
    this also works without the monkeypatch; the monkeypatch makes the
    isolation explicit and future-proof)."""
    import audittrace.logging_config as lc

    monkeypatch.setattr(lc, "_JWT_SHAPE_RE", __import__("re").compile(r"(?!x)x"))
    monkeypatch.setattr(lc, "_AUTHZ_SHAPE_RE", __import__("re").compile(r"(?!x)x"))

    register_request_secret(REGISTERED_SECRET)

    if position == "positional":
        rendered = render_payload((REGISTERED_SECRET,), limit=4000)
    elif position == "kwarg":
        rendered = render_payload({"token": REGISTERED_SECRET}, limit=4000)
    elif position == "nested_dict":
        rendered = render_payload({"outer": {"inner": REGISTERED_SECRET}}, limit=4000)
    elif position == "list_element":
        rendered = render_payload(["a", REGISTERED_SECRET, "c"], limit=4000)
    else:  # return_value
        rendered = render_payload(REGISTERED_SECRET, limit=4000)

    assert REGISTERED_SECRET not in rendered
    assert "[REDACTED:bearer]" in rendered


def test_g2_registered_secret_scrubbed_from_exception_message():
    register_request_secret(REGISTERED_SECRET)
    exc = HTTPException(status_code=500, detail=f"boom {REGISTERED_SECRET}")
    span = MagicMock(spec=["add_event", "set_status"])
    _record_span_error(span, exc)
    (_, attrs) = span.add_event.call_args.args
    assert REGISTERED_SECRET not in attrs["exception.message"]
    assert "[REDACTED:bearer]" in attrs["exception.message"]


def test_d4_shape_net_scrubs_unregistered_jwt_and_authz_shapes():
    """D4 — the secondary shape net catches a JWT/Bearer-shaped value
    EVEN WITHOUT registration (it must cover credentials that bypass D3:
    a future HTTPBearer(), a header read by hand, an alg:none token).

    Calls the production ``scrub_text`` directly — distinct from
    ``test_g6d_net_self_validation_fires_on_planted_jwt``, which exercises
    the NET's own INDEPENDENT scanning function
    (``tests.conftest.scan_for_secret_shapes``), not this one — the net
    must detect a leak even if ``scrub_text`` itself is broken, so the two
    must never share an implementation.
    """
    assert request_secrets() == ()  # nothing registered — D4 only
    rendered = scrub_text(f"Authorization: {UNREGISTERED_JWT}")
    assert UNREGISTERED_JWT not in rendered
    assert "[REDACTED:jwt]" in rendered

    bearer_text = "Authorization: Bearer " + "z" * 40
    rendered_bearer = scrub_text(bearer_text)
    assert "z" * 40 not in rendered_bearer
    assert "[REDACTED:authz]" in rendered_bearer


# ═══════════════════════════ G2e — exactly one exception event ════════════


def test_g2e_exactly_one_exception_event_per_failing_seam_call():
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g2e-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    register_request_secret(REGISTERED_SECRET)

    @log_call(logger=logging.getLogger("test.g2e"))
    def boom():
        raise ValueError(f"kaboom {REGISTERED_SECRET}")

    with pytest.raises(ValueError):
        boom()

    spans = [s for s in exporter.get_finished_spans() if s.name.endswith("boom")]
    assert spans, "expected a span for the failing call"
    span = spans[-1]
    events = [e for e in span.events if e.name == "exception"]
    assert len(events) == 1, f"expected exactly 1 exception event, got {len(events)}"
    assert REGISTERED_SECRET not in events[0].attributes["exception.message"]

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


# ═══════════════════════════ G2f — scrub before truncate ══════════════════


def test_g2f_scrub_before_truncate_boundary():
    """A token straddling the truncation cut must not leave an unscrubbed
    FRAGMENT (spec fact A7's exact escape) — checked fragment-by-fragment,
    not just full-string absence.

    Gate REJECT round 1 BLOCKER 4 — the previous version of this test
    only asserted ``REGISTERED_SECRET not in rendered``, which stays
    trivially true even under the truncate-THEN-scrub regression this
    test exists to catch: truncating a 40-char secret down to a ~20-char
    prefix means the FULL 40-char string was never going to be found
    either way, so the full-string check could never tell the two
    orderings apart. Checking every >=16-char substring of the secret
    closes that gap — only the correct (scrub-then-truncate) ordering
    passes it."""
    register_request_secret(REGISTERED_SECRET)
    padding = "x" * 100
    value = f"{padding}{REGISTERED_SECRET}{padding}"
    rendered = render_payload(value, limit=120)
    assert REGISTERED_SECRET not in rendered
    for n in (16, 24, 32):
        for i in range(0, len(REGISTERED_SECRET) - n + 1, 4):
            assert REGISTERED_SECRET[i : i + n] not in rendered


def test_g2f_truncate_first_would_leave_a_short_residue_scrub_first_does_not():
    """Should-fix (fix1 re-review's G2f ruling, ACCEPTABLE overall): WITHIN
    the <16-char fragment-scan floor, ordering is STILL security-relevant
    as defence-in-depth. A cut landing 1-15 chars into the secret would,
    if truncated FIRST, leave a residue too short for either D3 (not the
    full value) or the fragment scan (below ``_FRAGMENT_WINDOW_LEN``) to
    catch. The ACTUAL ``render_payload`` scrubs FIRST, so no residue of
    any length survives regardless of where the cut lands — this row
    makes that difference observable rather than asserting it only in
    prose."""
    register_request_secret(REGISTERED_SECRET)
    padding = "x" * 50
    value = f"{padding}{REGISTERED_SECRET}{padding}"
    residue_len = 7  # within the 1-15 advisory range
    cut_at = len(padding) + residue_len

    # Scrub-first (production, ``render_payload``): no residue survives.
    rendered = render_payload(value, limit=cut_at)
    assert REGISTERED_SECRET[:residue_len] not in rendered

    # Truncate-first (the pre-A7-fix ordering, reproduced BY HAND here —
    # never production code): a short residue DOES survive, and it is
    # genuinely below the fragment scan's floor.
    truncated_first = value[:cut_at]
    assert REGISTERED_SECRET[:residue_len] in truncated_first
    rescrubbed = scrub_text(truncated_first)
    assert REGISTERED_SECRET[:residue_len] in rescrubbed, (
        "sanity: a <16-char residue is BELOW the fragment-scan floor by "
        "design (D3's contract threshold) — ordering is what protects it"
    )


# ═══════ D3 fragment leg — survives third-party truncation (BLOCKER 1) ═════
#
# Gate REJECT round 1 BLOCKER 1: SQLAlchemy's ``StatementError`` repr
# truncates a long bound parameter out of the MIDDLE — "<head> ... (N
# characters truncated) ... <tail>" — before the value ever reaches
# ``scrub_text``. Both <head> and <tail> are genuine, unaltered substrings
# of the secret: the exact-match branch misses them (the full string is
# never present as one run) and the D4 shape net also misses them (a bare
# fragment carries no second "." so it is no longer JWT-shaped). The guard
# tests below exercise ``logging_config._scrub_secret_fragments`` /
# ``secret_leaks_into`` directly; the five ADDENDUM-C G8 real-route rows
# further down exercise the SAME mechanism end to end through the actual
# SQLAlchemy exception class, via a real HTTP route.

_REAL_LENGTH_SECRET = "s3cr3t." + "A" * 900 + "." + "B" * 400  # ~1300 chars


def _sqlalchemy_truncated_repr(secret: str, head: int = 120, tail: int = 120) -> str:
    """Reproduce SQLAlchemy's bound-parameter truncation shape: keep a
    genuine head and tail slice of the value, elide the middle."""
    return (
        f"{secret[:head]} ... ({len(secret) - head - tail} characters "
        f"truncated) ... {secret[-tail:]}"
    )


def _assert_no_fragment_of_length_survives(
    secret: str, rendered: str, lengths: tuple[int, ...] = (16, 32, 64, 100)
) -> None:
    for n in lengths:
        for i in range(0, len(secret) - n, 7):
            assert secret[i : i + n] not in rendered, (
                f"a {n}-char fragment of the secret survived redaction: "
                f"{secret[i : i + n]!r}"
            )


def test_d3_truncated_fragment_still_redacted():
    """BLOCKER 1's own counter-example: a head/tail fragment surviving a
    third party's mid-string truncation must still be fully redacted,
    even though the FULL registered value never appears verbatim."""
    register_request_secret(_REAL_LENGTH_SECRET)
    truncated = _sqlalchemy_truncated_repr(_REAL_LENGTH_SECRET)
    assert _REAL_LENGTH_SECRET not in truncated, (
        "sanity: the truncated repr must not already equal the full secret"
    )
    rendered = scrub_text(truncated)
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered)
    assert "[REDACTED:bearer]" in rendered


def test_d3_two_separate_fragments_in_one_string_both_redacted():
    """A head fragment AND a tail fragment, separated by unrelated text,
    must each be found and redacted independently — proves the scan does
    not stop after the first match."""
    register_request_secret(_REAL_LENGTH_SECRET)
    head = _REAL_LENGTH_SECRET[:200]
    tail = _REAL_LENGTH_SECRET[-200:]
    text = f"first chunk: {head}\nunrelated noise\nsecond chunk: {tail}"
    rendered = scrub_text(text)
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered)
    assert rendered.count("[REDACTED:bearer]") == 2


def test_l1_full_value_and_truncated_fragment_in_one_string_both_redacted():
    """BLOCKER L1 (gate REJECT round 2, orchestrator ruling 2026-10-11) —
    a single string carrying BOTH the full registered secret AND a
    separately-truncated fragment of the SAME secret used to leave the
    fragment unredacted: ``scrub_text``'s exact-match branch used to be
    an ``else``-guarded ALTERNATIVE to the fragment scan, so matching the
    full value short-circuited the fragment pass entirely for that
    secret. Plants the reviewer's two measured shapes in ONE string
    each: the full value plus a SQLAlchemy ``StatementError``-shaped
    repr of the same secret (an f-string message glued to a DB error
    repr — a realistic co-occurrence), and the full value plus its own
    100-char head (the old ``str(kwargs)[:100]`` DEBUG-log shape)."""
    register_request_secret(_REAL_LENGTH_SECRET)
    statement_error_shape = (
        f"full value present verbatim: {_REAL_LENGTH_SECRET}\n"
        "and separately truncated by a third party: "
        f"{_sqlalchemy_truncated_repr(_REAL_LENGTH_SECRET)}"
    )
    rendered = scrub_text(statement_error_shape)
    assert _REAL_LENGTH_SECRET not in rendered
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered)

    head_100_shape = (
        f"full value present verbatim: {_REAL_LENGTH_SECRET}\n"
        f"and a 100-char head logged separately: {_REAL_LENGTH_SECRET[:100]}"
    )
    rendered2 = scrub_text(head_100_shape)
    assert _REAL_LENGTH_SECRET not in rendered2
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered2)


def test_l1_neuter_exact_match_as_else_branch_leaks_the_co_occurring_fragment():
    """NEUTER for the guard above — restore the PRE-FIX ``scrub_text``
    body (the fragment scan living only as the ``else`` of the
    exact-match check, gate REJECT round 2's own description of the
    defect) and confirm the co-occurring-fragment case leaks. Proves the
    guard above actually exercises the round-2 fix, not a tautology that
    would pass even against the broken shape."""
    import audittrace.logging_config as lc

    def _pre_fix_scrub_text(value: str) -> str:
        if not value:
            return value
        secrets = request_secrets()
        for secret in sorted(secrets, key=len, reverse=True):
            if not secret:
                continue
            if secret in value:
                value = value.replace(secret, "[REDACTED:bearer]")
            else:
                value = lc._scrub_secret_fragments(value, secret)
        value = lc._JWT_SHAPE_RE.sub("[REDACTED:jwt]", value)
        value = lc._AUTHZ_SHAPE_RE.sub("[REDACTED:authz]", value)
        return value

    register_request_secret(_REAL_LENGTH_SECRET)
    text = (
        f"full value present verbatim: {_REAL_LENGTH_SECRET}\n"
        "and separately truncated: "
        f"{_sqlalchemy_truncated_repr(_REAL_LENGTH_SECRET)}"
    )
    rendered = _pre_fix_scrub_text(text)
    leaked = False
    try:
        _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered)
    except AssertionError:
        leaked = True
    assert leaked, (
        "NEUTER SANITY: the pre-fix else-branch body must leak a "
        "fragment when the full value co-occurs in the same string — if "
        "this assertion fails, the reproduction has drifted from "
        "scrub_text's real pre-fix shape"
    )


def test_l1_single_pass_sinks_redact_full_plus_fragment_co_occurrence():
    """L1, SINGLE-PASS sinks — proves the fix holds OUTSIDE the seam's
    double-scrub path. A ``@log_call`` seam span is scrubbed TWICE
    (``_record_span_error``/``_set_span_input`` call ``scrub_text``
    directly, then ``ScrubbingSpanProcessor.on_end`` scrubs the exported
    span again), so a one-pass regression in either mechanism could hide
    behind the other. A non-seam span attribute (set directly, never
    through ``@log_call``) and a formatted log message are each
    scrubbed EXACTLY ONCE — by ``ScrubbingSpanProcessor.on_end`` and by
    ``StructuredFormatter.format`` respectively — so this is the
    DIFFERENT-source proof the single exact-match-vs-fragment fix
    actually needs."""
    register_request_secret(_REAL_LENGTH_SECRET)
    combined = (
        f"full: {_REAL_LENGTH_SECRET} truncated: "
        f"{_sqlalchemy_truncated_repr(_REAL_LENGTH_SECRET)} head100: "
        f"{_REAL_LENGTH_SECRET[:100]}"
    )

    # Non-seam span attribute — single pass via ScrubbingSpanProcessor.on_end.
    class _FakeSpan:
        _attributes = {"input.value": combined}
        _events: tuple[Any, ...] = ()
        _links: tuple[Any, ...] = ()
        _status = None

    span = _FakeSpan()
    telemetry.ScrubbingSpanProcessor().on_end(span)  # type: ignore[arg-type]
    span_text = span._attributes["input.value"]
    assert _REAL_LENGTH_SECRET not in span_text
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, span_text)

    # Formatted log message — single pass via StructuredFormatter.format.
    logger = logging.getLogger("test.l1.single_pass_sink")
    record = logger.makeRecord(
        logger.name, logging.WARNING, __file__, 1, combined, (), None
    )
    formatted = StructuredFormatter().format(record)
    assert _REAL_LENGTH_SECRET not in formatted
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, formatted)


def test_d3_fragment_shorter_than_window_is_not_matched():
    """A genuinely coincidental <16-char overlap with a registered secret
    (e.g. common padding) must NOT be treated as a leak — the window
    length is a deliberate floor, not an accident."""
    register_request_secret(_REAL_LENGTH_SECRET)
    short_overlap = _REAL_LENGTH_SECRET[:10]  # below the 16-char window
    rendered = scrub_text(f"coincidental prefix: {short_overlap}")
    assert short_overlap in rendered  # too short to count as a leak signal


def test_d3_fragment_of_exactly_16_chars_at_text_end_is_redacted():
    """Should-fix (fix1 re-review): an EXACTLY-16-char trailing fragment,
    at the very END of the text (no trailing characters after it for the
    scan to extend into). Pins TWO mutations the reviewer's neuter table
    showed stay GREEN without this row:

      - the off-by-one ``while i <= n - W`` -> ``while i < n - W`` (the
        LAST possible window position is never scanned — a fragment
        starting exactly at ``n - W`` would be skipped);
      - the window constant ``_FRAGMENT_WINDOW_LEN`` 16 -> 24 (a 16-23
        char fragment would silently stop being redacted).
    """
    register_request_secret(_REAL_LENGTH_SECRET)
    exact_16 = _REAL_LENGTH_SECRET[100:116]
    assert len(exact_16) == 16
    text = f"leading noise then {exact_16}"
    rendered = scrub_text(text)
    assert exact_16 not in rendered
    assert "[REDACTED:bearer]" in rendered

    # And at the very START of the text (position 0 — the scan's first
    # possible window, pinning the loop's start boundary too).
    text_at_start = f"{exact_16} then trailing noise"
    rendered_at_start = scrub_text(text_at_start)
    assert exact_16 not in rendered_at_start
    assert "[REDACTED:bearer]" in rendered_at_start


def test_d3_secret_leaks_into_detects_fragment_and_exact():
    from audittrace.logging_config import secret_leaks_into

    assert secret_leaks_into(_REAL_LENGTH_SECRET, _REAL_LENGTH_SECRET)
    assert secret_leaks_into(
        f"noise {_REAL_LENGTH_SECRET[:50]} noise", _REAL_LENGTH_SECRET
    )
    assert not secret_leaks_into("nothing here", _REAL_LENGTH_SECRET)
    assert not secret_leaks_into(_REAL_LENGTH_SECRET[:10], _REAL_LENGTH_SECRET)


def test_d3_fragment_scrub_terminates_promptly_on_large_text():
    """Sanity bound on the O(n) claim: scrubbing a realistically-sized
    (StatementError-shaped) text carrying one fragment must not exhibit
    pathological (quadratic-in-secret-length) slowdown."""
    register_request_secret(_REAL_LENGTH_SECRET)
    noise = "x" * 50_000
    text = f"{noise}{_REAL_LENGTH_SECRET[:200]}{noise}"
    start = time.perf_counter()
    rendered = scrub_text(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"fragment scrub took {elapsed:.2f}s — investigate"
    _assert_no_fragment_of_length_survives(_REAL_LENGTH_SECRET, rendered)


# ═══════════════════════════ G3 — scheme pin (route-level) ════════════════

_test_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PRIVATE_PEM = _test_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
TEST_PUBLIC_PEM = (
    _test_private_key.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)
TEST_ISSUER = "http://keycloak:8080/realms/audittrace"
TEST_AUDIENCE = "audittrace-server"


def _mint_jwt(**extra_claims: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "sub": "g3-test-user",
        "scope": "audittrace:query",
        "iat": now,
        "exp": now + 3600,
        "jti": "jti-g3",
    }
    claims.update(extra_claims)
    return jwt.encode(claims, TEST_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def _g3_env(monkeypatch):
    import fakeredis

    from audittrace import config as config_mod
    from audittrace import identity as identity_mod

    fake = fakeredis.FakeRedis(decode_responses=True)
    cache = identity_mod.TokenCache(fake, default_ttl_seconds=300)
    monkeypatch.setattr(identity_mod, "_token_cache", cache)
    monkeypatch.setattr(identity_mod, "_redis_client", fake)

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", TEST_AUDIENCE)
    yield
    config_mod.get_settings.cache_clear()
    monkeypatch.setattr(identity_mod, "_token_cache", None)
    monkeypatch.setattr(identity_mod, "_redis_client", None)


def test_g3_require_user_span_redacts_the_bearer(monkeypatch, _g3_env):
    """The exported `require_user` span's input.value + Langfuse mirror
    contain [REDACTED:bearer] and no >=16-char substring of the minted
    token; the DEBUG INPUT record's call_kwargs likewise."""
    from unittest.mock import AsyncMock

    from audittrace import auth as auth_mod

    monkeypatch.setattr(
        auth_mod, "_fetch_jwks_keys", AsyncMock(return_value=[TEST_PUBLIC_PEM])
    )
    auth_mod._jwks_cache.clear()

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g3-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(auth_mod.require_user)):
        return {"user_id": user.user_id}

    token = _mint_jwt()
    caplog_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.setLevel(logging.DEBUG)
    handler.emit = caplog_records.append
    logging.getLogger("audittrace.auth").addHandler(handler)
    logging.getLogger("audittrace.auth").setLevel(logging.DEBUG)
    try:
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
    finally:
        logging.getLogger("audittrace.auth").removeHandler(handler)

    spans = [s for s in exporter.get_finished_spans() if "require_user" in s.name]
    assert spans, "expected a require_user span"
    span = spans[-1]
    input_val = span.attributes.get("input.value", "")
    mirror_val = span.attributes.get("langfuse.observation.input", "")
    assert "[REDACTED:bearer]" in input_val
    assert "[REDACTED:bearer]" in mirror_val
    # No >=16-char substring of the raw token anywhere on the span.
    for n in (16, 32, 64):
        for i in range(0, len(token) - n, 7):
            assert token[i : i + n] not in input_val

    debug_records = [r for r in caplog_records if r.getMessage().startswith("INPUT")]
    assert debug_records, "expected a DEBUG INPUT record"
    formatted = StructuredFormatter().format(debug_records[-1])
    assert token not in formatted

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


# NOTE on the G3 neuter: monkeypatching ``audittrace.auth.oauth2_scheme``
# from a test does NOT neuter ``require_user`` — ``token: str | None =
# Depends(oauth2_scheme)`` binds the default argument ONCE, at
# ``auth.py`` import time (ordinary Python default-argument semantics;
# FastAPI's dependant tree captures that same object). Verified
# empirically: swapping the module attribute to a plain
# ``OAuth2AuthorizationCodeBearer`` and disabling the shape net still
# produced ``[REDACTED:bearer]`` on the span, because the ALREADY-BOUND
# registering scheme instance is what FastAPI actually calls regardless
# of what the module attribute points to afterwards. The real G3
# neuter therefore has to edit the SOURCE (removing the
# ``register_request_secret(token)`` call inside
# ``_RegisteringOAuth2Scheme.__call__``) and is scripted as N11 in
# ``neuter-runner.py`` (evidence dir) rather than done in-test.


# ═══════════════════════════ G4 — equality pins ═══════════════════════════


def _iter_py_files():
    yield from SRC_ROOT.rglob("*.py")


_FASTAPI_SECURITY_BASE_NAMES = (
    "OAuth2AuthorizationCodeBearer",
    "OAuth2PasswordBearer",
    "OAuth2",
    "HTTPBearer",
    "HTTPBasic",
    "APIKeyHeader",
    "APIKeyCookie",
    "APIKeyQuery",
)


def test_g4a_single_fastapi_security_scheme_instantiation_site():
    """Exactly one module in src/ instantiates a fastapi.security.* scheme
    class (directly OR via a local subclass of one — ``auth.py`` defines
    ``_RegisteringOAuth2Scheme(OAuth2AuthorizationCodeBearer)`` precisely
    so the bearer has ONE entry point, D3), and it is auth.py."""
    # First pass: find every class anywhere in src/ that (transitively)
    # subclasses a fastapi.security.* base, by name.
    subclass_names = set(_FASTAPI_SECURITY_BASE_NAMES)
    paths = list(_iter_py_files())
    changed = True
    while changed:
        changed = False
        for path in paths:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    base_names = {
                        (b.id if isinstance(b, ast.Name) else getattr(b, "attr", None))
                        for b in node.bases
                    }
                    if base_names & subclass_names and node.name not in subclass_names:
                        subclass_names.add(node.name)
                        changed = True

    sites: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if name in subclass_names:
                    sites.add(str(path.relative_to(SRC_ROOT.parent.parent)))
    assert sites == {"src/audittrace/auth.py"}


def _is_log_call_decorator(dec: ast.expr) -> bool:
    dec_fn = dec.func if isinstance(dec, ast.Call) else dec
    dec_name = (
        dec_fn.attr
        if isinstance(dec_fn, ast.Attribute)
        else getattr(dec_fn, "id", None)
    )
    return dec_name == "log_call"


def test_g4b_log_call_ast_targets_match_runtime_marker():
    """Every @log_call AST target — MODULE-LEVEL *and* CLASS-SCOPED alike
    — once imported, carries the __audittrace_log_call__ marker the
    decorator sets.

    Gate REJECT round 1 BLOCKER 3 — the previous version of this test
    verified only the 94 module-level targets and merely COUNTED the
    ~244 class-scoped ones without ever checking the marker against
    them; a decorator silently dropped from a class method would have
    passed unnoticed. Resolving ``getattr(mod, cls_name)`` then
    ``getattr(cls, func_name)`` closes that gap for every class reachable
    as a module attribute (measured: 338/338 resolve and all carry the
    marker — zero class nested inside a function, the one documented
    AST-vs-import-resolution limit this shares with the module-level
    path)."""

    count_targets = 0
    unmarked: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel_module = (
            "audittrace." + str(path.relative_to(SRC_ROOT))[:-3].replace("/", ".")
        ).rstrip(".")
        if rel_module.endswith("__init__"):
            rel_module = rel_module[: -len(".__init__")]
        tree = ast.parse(path.read_text(), filename=str(path))

        # Module-level targets: direct children of the Module node.
        module_level_funcs = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_is_log_call_decorator(d) for d in node.decorator_list)
        }
        # Class-scoped targets: each ClassDef's OWN direct body (so a
        # method is attributed to its immediate enclosing class).
        class_scoped: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _is_log_call_decorator(d) for d in item.decorator_list
                ):
                    class_scoped.append((node.name, item.name))

        if not module_level_funcs and not class_scoped:
            continue
        try:
            mod = importlib.import_module(rel_module)
        except Exception:
            unmarked.append(f"{rel_module}: import failed")
            continue

        for name in module_level_funcs:
            count_targets += 1
            obj = getattr(mod, name, None)
            if obj is None or not getattr(obj, "__audittrace_log_call__", False):
                unmarked.append(f"{rel_module}.{name}")

        for cls_name, func_name in class_scoped:
            count_targets += 1
            cls = getattr(mod, cls_name, None)
            if cls is None:
                # Not reachable as a module attribute (e.g. a class
                # nested inside a function) — same documented limit the
                # module-level path has for a function nested inside a
                # function. Measured: 0 occurrences in this codebase.
                unmarked.append(f"{rel_module}.{cls_name}: class not import-resolvable")
                continue
            obj = getattr(cls, func_name, None)
            target = getattr(obj, "__func__", obj)  # unwrap classmethod/staticmethod
            if obj is None or not (
                getattr(target, "__audittrace_log_call__", False)
                or getattr(obj, "__audittrace_log_call__", False)
            ):
                unmarked.append(f"{rel_module}.{cls_name}.{func_name}")

    assert count_targets > 300, "sanity: expected the ~338-target scale"
    assert not unmarked, f"@log_call targets missing the marker: {unmarked}"


def _flatten_annotation(ann: ast.expr | None):
    if ann is None:
        return
    if isinstance(ann, ast.Name):
        yield ann.id
    elif isinstance(ann, ast.Attribute):
        yield ann.attr
    elif isinstance(ann, ast.Subscript):
        yield from _flatten_annotation(ann.slice)
    elif isinstance(ann, ast.BinOp):
        yield from _flatten_annotation(ann.left)
        yield from _flatten_annotation(ann.right)
    elif isinstance(ann, ast.Tuple):
        for el in ann.elts:
            yield from _flatten_annotation(el)


def _collect_log_call_signature_names() -> set[str]:
    """AST-walk every @log_call target's parameter AND return annotations,
    flattened to bare class names. Shared by G4c (models/dataclasses) and
    G4d (ORM rows) — both independently re-derive the SAME "what classes
    appear in a decorated signature" fact, then resolve it against a
    DIFFERENT allowlist leg each."""
    names: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not any(
                    (
                        d.func.attr
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        else (
                            d.func.id
                            if isinstance(d, ast.Call)
                            else getattr(d, "id", getattr(d, "attr", None))
                        )
                    )
                    == "log_call"
                    for d in node.decorator_list
                ):
                    continue
                args = node.args
                for a in args.posonlyargs + args.args + args.kwonlyargs:
                    if a.arg in ("self", "cls"):
                        continue
                    names.update(_flatten_annotation(a.annotation))
                if node.returns is not None:
                    names.update(_flatten_annotation(node.returns))
    return names


def test_g4c_render_allowlists_match_independent_ast_derivation():
    """Independently re-derive the params∪returns class set via AST +
    import resolution and assert it equals logging_config's allowlists —
    TWO-SIDED (gate REJECT round 1 BLOCKER 3): a class allowlisted but
    never used in any decorated signature is JUST as much a drift as one
    used but not allowlisted (the X-G4c "allowlist gains an UNUSED
    model" neuter showed the old missing-only check never caught the
    former).

    Deliberately does NOT reuse logging_config._get_render_allowlists —
    an equality pin whose two sides share a computation is vacuous
    (`feedback_a_pins_reference_point_must_be_inside_the_guarded_unit`).
    """
    import sys as _sys

    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    import audittrace.server  # noqa: F401 - ensure the full app surface is import-resolvable

    names = _collect_log_call_signature_names()

    resolved_models: set[type] = set()
    resolved_dataclasses: set[type] = set()
    for modname, mod in list(_sys.modules.items()):
        if not modname.startswith("audittrace"):
            continue
        for n in names:
            obj = getattr(mod, n, None)
            if isinstance(obj, type):
                if issubclass(obj, BaseSettings):
                    continue
                if issubclass(obj, BaseModel):
                    resolved_models.add(obj)
                elif __import__("dataclasses").is_dataclass(obj):
                    from audittrace.identity import UserContext

                    if obj is not UserContext:
                        resolved_dataclasses.add(obj)

    render_models, render_dataclasses, _render_orm = _get_render_allowlists()
    missing_models = resolved_models - render_models
    extra_models = render_models - resolved_models
    missing_dataclasses = resolved_dataclasses - render_dataclasses
    extra_dataclasses = render_dataclasses - resolved_dataclasses
    assert not missing_models, (
        f"models used in signatures but not allowlisted: {missing_models}"
    )
    assert not extra_models, (
        f"models allowlisted but unused in any @log_call signature: {extra_models}"
    )
    assert not missing_dataclasses, (
        f"dataclasses used in signatures but not allowlisted: {missing_dataclasses}"
    )
    assert not extra_dataclasses, (
        f"dataclasses allowlisted but unused in any @log_call signature: "
        f"{extra_dataclasses}"
    )


def test_g4e_render_orm_allowlist_matches_independent_ast_derivation():
    """G4e (RENAMED from a round-1 mislabel — fix1 re-review R3: "a
    different guard was given the G4d label"; G4d is the spec's
    ``input.value``-writers pin, built separately below) — the SAME
    independent-AST-derivation equality pin as G4c, for the ORM allowlist
    leg (``_RENDER_ORM_IMPORTS``), which had NO independent check at all
    before this. Two-sided, same reasoning as G4c."""
    import sys as _sys

    from sqlalchemy.exc import NoInspectionAvailable
    from sqlalchemy.inspection import inspect as sa_inspect

    import audittrace.server  # noqa: F401 - ensure the full app surface is import-resolvable

    names = _collect_log_call_signature_names()

    resolved_orm: set[type] = set()
    for modname, mod in list(_sys.modules.items()):
        if not modname.startswith("audittrace"):
            continue
        for n in names:
            obj = getattr(mod, n, None)
            if not isinstance(obj, type):
                continue
            try:
                sa_inspect(obj)
            except NoInspectionAvailable:
                continue
            except Exception:
                continue
            resolved_orm.add(obj)

    _render_models, _render_dataclasses, render_orm = _get_render_allowlists()
    missing_orm = resolved_orm - render_orm
    extra_orm = render_orm - resolved_orm
    assert not missing_orm, (
        f"ORM rows used in signatures but not allowlisted: {missing_orm}"
    )
    assert not extra_orm, (
        f"ORM rows allowlisted but unused in any @log_call signature: {extra_orm}"
    )


# ═══════════════════════ G4d — direct input.value writers pin ═════════════
#
# Spec G4d (ADDENDUM A §5 S1), gate REJECT round 1 fix1 re-review R3 — an
# AST-derived, FROZEN equality over every site OUTSIDE the seam
# (``telemetry.py``/``logging_config.py``) that writes the literal span
# attribute key ``"input.value"`` directly, bypassing ``_set_span_input``'s
# ``render_payload`` call. These sites rely on D7 (the export-time
# scrubber) as their ONLY redaction layer, since they never call
# ``scrub_text``/``render_payload`` themselves — a 5th such writer, with
# no accompanying review of whether D7 alone covers it, is exactly the
# drift this pin exists to catch.


def _find_direct_input_value_writers() -> list[tuple[str, str]]:
    """AST-derived set of every ``"input.value"``-writing site outside
    ``telemetry.py``/``logging_config.py`` (the seam's own machinery,
    which legitimately writes/reads that literal key as part of its own
    plumbing — the self-test probe plant, the Langfuse-key mirror).
    Returns a sorted ``(relative-path, enclosing-def-name)`` set — an
    EQUALITY pin (G6e's should-fix: pin by symbol, not line number, so a
    harmless edit above a site does not spuriously redden it)."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path.name in ("telemetry.py", "logging_config.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = str(path.relative_to(SRC_ROOT.parent.parent))
        for node in ast.walk(tree):
            lineno: int | None = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_attribute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "input.value"
            ):
                lineno = node.lineno
            elif isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and k.value == "input.value":
                        lineno = node.lineno
                        break
            if lineno is not None:
                enclosing = _enclosing_def_name(tree, lineno)
                if enclosing is not None:
                    sites.add((rel, enclosing))
    return sorted(sites)


# FROZEN — measured against this commit. A legitimate new direct writer
# (reviewed to confirm D7 alone covers it, or amended to call
# render_payload itself) updates this tuple BY HAND, never
# programmatically.
_FROZEN_DIRECT_INPUT_VALUE_WRITERS: tuple[tuple[str, str], ...] = (
    ("src/audittrace/routes/_memory_tool_loop.py", "_execute_memory_tool"),
    ("src/audittrace/routes/_memory_tool_loop.py", "run_memory_tool_loop"),
    ("src/audittrace/routes/chat.py", "_set_genai_request_attributes"),
    ("src/audittrace/routes/chat.py", "chat_completions"),
)


def test_g4d_direct_input_value_writers_are_frozen():
    """G4d (spec, ADDENDUM A §5 S1) — the direct ``input.value`` writers
    outside the seam are a frozen set of exactly 4
    (``_memory_tool_loop.py`` x2, ``chat.py`` x2). A planted 5th writer
    reddens this test (gate REJECT round 1 fix1 re-review R3)."""
    actual = _find_direct_input_value_writers()
    assert actual == list(_FROZEN_DIRECT_INPUT_VALUE_WRITERS)


# ═══════════════════════════ G5 — composition matrix ══════════════════════
#
# ADDENDUM C's composition matrix: D3 (registry) x D4 (shape net) x
# sync/async x WHERE the secret appears in the call, measured through the
# REAL @log_call decorator + span emission + the DEBUG log line (not just
# render_payload in isolation, which is G2's job).
#
# Gate REJECT round 1 fix1 re-review R1 — this hand-picked matrix is ONE
# leg of G5, not the whole of it; it stays, extended with the two cells
# the reviewer named as missing (the RETURN-VALUE position and an
# UNREGISTERED-but-JWT-shaped second plant per ADDENDUM A S2 — "each cell
# plants two values"). The GENERATED leg over all ~338 real AST-derived
# targets (``test_g5b_generated_signature_cloned_matrix_...`` below) is
# the part this hand-picked matrix does NOT cover — the round-1 rationale
# that exhausting all targets "would require real business methods with
# full DI wiring" was wrong: a signature-cloned STUB needs neither.


_G5_POSITIONS = (
    "positional",
    "kwarg",
    "nested_dict",
    "list_element",
    "return_value",
    "exception",
)


def _g5_matrix_cases() -> list[Any]:
    """Build the composition matrix's cells EXCLUDING the one combination
    with no mechanism able to catch the plant at all (D3 only matches
    registered values; D4, the shape net, is off) — filtered OUT of
    collection entirely, never ``pytest.skip``ped at runtime (this repo's
    zero-skip policy forbids skips: a skip that never runs is not
    evidence of anything, and CI treats any skip as a hard failure).
    documents the matrix's own known boundary by ABSENCE of that cell,
    not by a runtime skip."""
    cases = []
    for position in _G5_POSITIONS:
        for is_async in (True, False):
            for shape_net in ("on", "off"):
                for value_kind in ("registered", "unregistered_jwt"):
                    if value_kind == "unregistered_jwt" and shape_net == "off":
                        continue
                    cases.append(
                        pytest.param(
                            position,
                            is_async,
                            shape_net,
                            value_kind,
                            id=f"{position}-{is_async}-{shape_net}-{value_kind}",
                        )
                    )
    return cases


@pytest.mark.parametrize("position,is_async,shape_net,value_kind", _g5_matrix_cases())
def test_g5_composition_matrix_log_call_scrubs_every_combination(
    position, is_async, shape_net, value_kind, monkeypatch
):
    """G5 — parametrised composition matrix: 6 positions (incl. the
    return value) x 2 sync/async x 2 shape-net states x 2 planted-value
    kinds (registered / unregistered-but-JWT-shaped), MINUS the one
    uncatchable cell (see ``_g5_matrix_cases``) = 36 rows, each measured
    through the real @log_call wrapper's span attributes AND its DEBUG
    log record, not through render_payload directly.
    """
    import asyncio

    import audittrace.logging_config as lc

    if shape_net == "off":
        monkeypatch.setattr(lc, "_JWT_SHAPE_RE", __import__("re").compile(r"(?!x)x"))
        monkeypatch.setattr(lc, "_AUTHZ_SHAPE_RE", __import__("re").compile(r"(?!x)x"))

    register_request_secret(REGISTERED_SECRET)
    secret = REGISTERED_SECRET if value_kind == "registered" else UNREGISTERED_JWT

    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda *a, **kw: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    logger = logging.getLogger(
        f"test.g5.{position}.{is_async}.{shape_net}.{value_kind}"
    )
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.setLevel(logging.DEBUG)
    handler.emit = records.append
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    raises = position == "exception"

    if is_async:

        @log_call(logger=logger)
        async def op(payload):
            if raises:
                raise ValueError(f"boom {secret}")
            if position == "return_value":
                return secret
            return "ok"
    else:

        @log_call(logger=logger)
        def op(payload):
            if raises:
                raise ValueError(f"boom {secret}")
            if position == "return_value":
                return secret
            return "ok"

    payload: Any
    if position in ("positional", "return_value"):
        payload = secret if position == "positional" else "irrelevant"
    elif position == "kwarg":
        payload = {"token": secret}
    elif position == "nested_dict":
        payload = {"outer": {"inner": secret}}
    elif position == "list_element":
        payload = ["a", secret, "c"]
    else:  # exception
        payload = "trigger"

    try:
        if is_async:
            if raises:
                with pytest.raises(ValueError):
                    asyncio.run(op(payload))
            else:
                asyncio.run(op(payload))
        else:
            if raises:
                with pytest.raises(ValueError):
                    op(payload)
            else:
                op(payload)
    finally:
        logger.removeHandler(handler)

    span_text = str(
        {call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list}
    )
    assert secret not in span_text, (
        f"secret leaked onto the span for position={position} "
        f"is_async={is_async} shape_net={shape_net} value_kind={value_kind}"
    )
    # Measure the EMITTED artefact (StructuredFormatter's output), not the
    # raw in-memory LogRecord — redaction happens in the FORMATTER
    # (`feedback_guard_the_emitted_artefact_not_the_intended_one`); the raw
    # record's own ``msg``/``exc_info`` legitimately still carry the
    # original text until formatted, same as every other test here (G3,
    # G7b) that calls ``StructuredFormatter().format(...)``.
    log_text = "\n".join(StructuredFormatter().format(r) for r in records)
    assert secret not in log_text, (
        f"secret leaked into a formatted DEBUG log line for "
        f"position={position} is_async={is_async} shape_net={shape_net} "
        f"value_kind={value_kind}"
    )


# ═══════════════ G5(b) — generated, signature-cloned per-target leg ═══════
#
# Gate REJECT round 1 fix1 re-review R1 (NOT ACCEPTABLE as delivered): the
# hand-picked matrix above is only ONE leg of G5. The spec's binding
# minimum also requires a GENERATED leg over every real @log_call target,
# via a signature-cloned STUB (``inspect.signature(real_fn)`` — no DI, no
# DB/S3, no business logic; the round-1 rationale that this would require
# "real business methods with their full DI wiring" was factually wrong).


def _enclosing_def_name(tree: ast.Module, lineno: int) -> str | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best.name if best else None


def _iter_log_call_targets_with_objects():
    """Yield ``(qualname, real_fn)`` for every import-resolvable
    @log_call target (module-level AND class-scoped) — the SAME ~338
    targets G4b counts, but returning the REAL (already-decorated)
    function OBJECT so its signature can be cloned."""
    for path in SRC_ROOT.rglob("*.py"):
        rel_module = (
            "audittrace." + str(path.relative_to(SRC_ROOT))[:-3].replace("/", ".")
        ).rstrip(".")
        if rel_module.endswith("__init__"):
            rel_module = rel_module[: -len(".__init__")]
        tree = ast.parse(path.read_text(), filename=str(path))
        module_level_funcs = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_is_log_call_decorator(d) for d in node.decorator_list)
        }
        class_scoped: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _is_log_call_decorator(d) for d in item.decorator_list
                ):
                    class_scoped.append((node.name, item.name))
        if not module_level_funcs and not class_scoped:
            continue
        try:
            mod = importlib.import_module(rel_module)
        except Exception:
            continue
        for name in module_level_funcs:
            obj = getattr(mod, name, None)
            if obj is not None and getattr(obj, "__audittrace_log_call__", False):
                yield f"{rel_module}.{name}", obj
        for cls_name, func_name in class_scoped:
            cls = getattr(mod, cls_name, None)
            if cls is None:
                continue
            obj = getattr(cls, func_name, None)
            if obj is None:
                continue
            target = getattr(obj, "__func__", obj)
            if getattr(target, "__audittrace_log_call__", False) or getattr(
                obj, "__audittrace_log_call__", False
            ):
                yield f"{rel_module}.{cls_name}.{func_name}", obj


def _clone_signature_stub(real_fn: Any, *, is_async: bool) -> Any:
    """Build (via a fully AST-derived ``exec``, never external input) a
    function whose parameter NAMES and KINDS match ``inspect.signature(
    real_fn)`` exactly — so a value can be bound POSITIONALLY or BY
    KEYWORD exactly as a real caller of ``real_fn`` would — WITHOUT
    calling ``real_fn``'s actual body (no DI, no DB/S3, no business
    logic). Defaults are irrelevant here (every call below supplies every
    parameter explicitly) so every default is replaced with ``None``.
    """
    sig = inspect.signature(real_fn)
    parts = []
    for name, p in sig.parameters.items():
        if p.kind == inspect.Parameter.VAR_POSITIONAL:
            parts.append(f"*{name}")
        elif p.kind == inspect.Parameter.VAR_KEYWORD:
            parts.append(f"**{name}")
        elif (
            p.default is not inspect.Parameter.empty
            or p.kind == inspect.Parameter.KEYWORD_ONLY
        ):
            parts.append(f"{name}=None")
        else:
            parts.append(name)
    keyword = "async def" if is_async else "def"
    src = f"{keyword} _stub({', '.join(parts)}):\n    return _G5B_RETURN_SENTINEL\n"
    namespace: dict[str, Any] = {"_G5B_RETURN_SENTINEL": _G5B_RETURN_SENTINEL}
    exec(src, namespace)  # noqa: S102 - src is fully derived from inspect.signature above
    return namespace["_stub"], sig


_G5B_RETURN_SENTINEL = "G5B-PLANT-AS-RETURN-VALUE"
_G5B_PLACEHOLDER = "g5b-placeholder"


def _g5b_build_call(
    params: list[tuple[str, inspect.Parameter]], plant_index: int, plant_value: Any
) -> tuple[list[Any], dict[str, Any]] | None:
    """Positional call: every slot filled in order, the plant at
    ``plant_index``. Returns ``None`` if ``plant_index`` targets a
    VAR_POSITIONAL/VAR_KEYWORD slot (handled by the keyword-call builder
    instead, where applicable)."""
    if params[plant_index][1].kind in (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        return None
    args: list[Any] = []
    for i, (_name, p) in enumerate(params):
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        args.append(plant_value if i == plant_index else _G5B_PLACEHOLDER)
    return args, {}


def _g5b_build_keyword_call(
    params: list[tuple[str, inspect.Parameter]], plant_index: int, plant_value: Any
) -> tuple[list[Any], dict[str, Any]] | None:
    """ALL-KEYWORD call: every POSITIONAL_OR_KEYWORD/KEYWORD_ONLY param
    passed by name, the plant at ``plant_index``. Returns ``None`` if
    ``plant_index`` is POSITIONAL_ONLY (cannot be passed by keyword) or a
    *args/**kwargs slot."""
    plant_name, plant_param = params[plant_index]
    if plant_param.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        return None
    kwargs: dict[str, Any] = {}
    for i, (name, p) in enumerate(params):
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        kwargs[name] = plant_value if i == plant_index else _G5B_PLACEHOLDER
    return [], kwargs


def test_g5b_generated_signature_cloned_matrix_plants_every_parameter_and_return(
    monkeypatch,
):
    """G5(b) — the GENERATED leg (gate REJECT round 1 fix1 re-review R1,
    "NOT ACCEPTABLE as delivered"): for every real @log_call target, a
    signature-cloned stub (no DI, no business logic — see
    ``_clone_signature_stub``) decorated with the REAL ``log_call`` is
    called with the registered secret bound to every parameter both
    POSITIONALLY and BY KEYWORD, plus as the function's RETURN value.
    One looping test reporting every failure together (974+-target x
    N-param individual parametrisation would be prohibitively slow to
    report row by row, per the reviewer's own allowance) rather than
    974 separate test ids.
    """
    import asyncio

    register_request_secret(REGISTERED_SECRET)
    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda *a, **kw: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    targets = list(_iter_log_call_targets_with_objects())
    assert len(targets) > 300, "sanity: expected the ~338-target scale"

    failures: list[str] = []
    checked_params = 0
    checked_calls = 0

    for qualname, real_fn in targets:
        is_async = asyncio.iscoroutinefunction(real_fn)
        try:
            stub, sig = _clone_signature_stub(real_fn, is_async=is_async)
        except (SyntaxError, ValueError, TypeError) as e:
            failures.append(f"{qualname}: could not clone signature ({e})")
            continue
        params = list(sig.parameters.items())
        decorated = log_call(logger=logging.getLogger(f"test.g5b.{qualname}"))(stub)

        def _invoke(args: list[Any], kwargs: dict[str, Any], *, _d=decorated) -> Any:
            if is_async:
                return asyncio.run(_d(*args, **kwargs))
            return _d(*args, **kwargs)

        plantable_indices = [
            i for i, (name, _p) in enumerate(params) if name not in ("self", "cls")
        ]
        for plant_index in plantable_indices:
            name = params[plant_index][0]
            for call_builder, shape in (
                (_g5b_build_call, "positional"),
                (_g5b_build_keyword_call, "keyword"),
            ):
                built = call_builder(params, plant_index, REGISTERED_SECRET)
                if built is None:
                    continue
                args, kwargs = built
                fake_span.reset_mock()
                try:
                    _invoke(args, kwargs)
                except Exception as e:  # pragma: no cover - diagnostic only
                    failures.append(
                        f"{qualname}::{name}::{shape}: stub call raised "
                        f"{type(e).__name__}: {e}"
                    )
                    continue
                checked_calls += 1
                span_text = str(
                    [c.args for c in fake_span.set_attribute.call_args_list]
                )
                if REGISTERED_SECRET in span_text:
                    failures.append(
                        f"{qualname}::{name}::{shape}: secret leaked onto the span"
                    )
            checked_params += 1

        # Return-value position: a varargs stub (no signature needed — the
        # plant is in the RETURN, not any parameter) that simply returns
        # the secret.
        async def _return_stub(*a, **k):  # noqa: ANN002, ANN003
            return REGISTERED_SECRET

        def _return_stub_sync(*a, **k):  # noqa: ANN002, ANN003
            return REGISTERED_SECRET

        return_decorated = log_call(
            logger=logging.getLogger(f"test.g5b.{qualname}.ret")
        )(_return_stub if is_async else _return_stub_sync)
        fake_span.reset_mock()
        try:
            if is_async:
                asyncio.run(return_decorated())
            else:
                return_decorated()
        except Exception as e:  # pragma: no cover - diagnostic only
            failures.append(
                f"{qualname}::<return>: stub call raised {type(e).__name__}: {e}"
            )
        else:
            checked_calls += 1
            span_text = str([c.args for c in fake_span.set_attribute.call_args_list])
            if REGISTERED_SECRET in span_text:
                failures.append(f"{qualname}::<return>: secret leaked onto the span")

    assert checked_params > 0 and checked_calls > 0, (
        "sanity: the generated leg must actually have exercised something"
    )
    assert not failures, (
        f"{len(failures)} generated-leg plant(s) leaked or errored "
        f"(checked {checked_calls} calls across {len(targets)} targets): "
        f"{failures[:20]}{' +more' if len(failures) > 20 else ''}"
    )


# ═══════════════════════════ G6 — suite-wide net self-validation ══════════


def test_g6d_net_self_validation_fires_on_planted_jwt():
    """The net's scanning function must fire on a deliberately planted,
    UNREGISTERED JWT-shaped string and a Bearer-shaped one — called
    directly (the net cannot certify itself by relying on the autouse
    fixture, which would fail the whole test instead of letting us
    observe the hits)."""
    from tests.conftest import scan_for_secret_shapes

    hits = scan_for_secret_shapes(
        [
            f"plain text carrying {UNREGISTERED_JWT} inline",
            "Authorization: Bearer " + "z" * 40,
            "nothing interesting here",
        ]
    )
    assert len(hits) == 2
    assert all(UNREGISTERED_JWT not in h and "z" * 40 not in h for h in hits)


def test_g6c_net_registered_value_leg_fires_on_a_fragment_the_shape_net_misses():
    """The net's SECOND, independent leg — ``scan_for_registered_secret_values``
    — must fire on a registered secret's fragment even when it carries
    NO JWT/Bearer shape at all (exactly BLOCKER 1's leak: a truncated
    repr's head/tail fragment is not JWT-shaped). The shape-only leg
    (G6d above) would miss this; this leg must not."""
    from tests.conftest import scan_for_registered_secret_values, scan_for_secret_shapes

    register_request_secret(_REAL_LENGTH_SECRET)
    fragment = _REAL_LENGTH_SECRET[200:260]  # plain substring, no JWT shape
    assert not scan_for_secret_shapes([f"plain text carrying {fragment}"])
    hits = scan_for_registered_secret_values([f"plain text carrying {fragment}"])
    assert len(hits) == 1
    assert fragment not in hits[0]  # rule 10 — never print the matched text


def test_j1_call_handlers_patch_is_installed_by_identity():
    """J1 identity pin (gate REJECT round 5) — ``logging.Logger.
    callHandlers`` must BE the net's own patched function, proving the
    hook is actually installed (not merely defined) for the life of
    this process. The autouse ``_redaction_net`` fixture installs it on
    first use (idempotently); by the time ANY test runs, it must already
    be in place."""
    import logging as _logging_mod

    from tests.conftest import _patched_call_handlers_identity

    assert _logging_mod.Logger.callHandlers is _patched_call_handlers_identity()


def test_j2_span_end_patch_is_installed_by_identity():
    """J2 identity pin (gate REJECT round 5) — ``opentelemetry.sdk.
    trace.Span.end`` must BE the net's own patched function."""
    from opentelemetry.sdk.trace import Span as _SDKSpan

    from tests.conftest import _patched_span_end_identity

    assert _SDKSpan.end is _patched_span_end_identity()


def test_g6_canary_seam_span_is_scanned(_canary_seam_span):
    """Dedicated per-test canary (gate REJECT round 4, R1) for the
    seam-span category — replaces the deleted session-wide aggregate.
    Drives a real ``@log_call`` seam span; the net's teardown (keyed on
    the ``_canary_seam_span`` marker fixture) asserts — from ``texts``,
    not a raw capture buffer (R2) — that >=1 was scanned. No provider
    reset needed (round 5, J2): the ``Span.end`` hook captures every
    span regardless of which provider it is bound to."""

    @log_call
    def _canary_target(x: int) -> int:
        return x

    _canary_target(1)


def test_g6_canary_server_span_is_scanned(client, _canary_server_span):
    """Dedicated per-test canary for the SERVER-span category (gate
    REJECT round 4, R1) — a real route through ``TestClient(app)``
    produces exactly one FastAPI-auto-instrumented SERVER span per call.
    Uses the plain ``client`` fixture (round 5, J2 — no provider reset
    needed; the ``Span.end`` hook does not care which provider the
    FastAPI instrumentor bound to, or whether that provider has since
    been shut down by an EARLIER test's own ``client`` lifespan exit —
    exactly the order-dependence the fix3/fix4 per-provider-attachment
    design suffered from)."""
    client.get("/health")


def test_g6_canary_non_seam_module_span_is_scanned(_canary_non_seam_module_span):
    """Dedicated per-test canary for the non-seam-MODULE-span category
    (gate REJECT round 4, R1) — drives a span with one of the real
    marker names (``llm.chat.completions``) directly through the
    module's own ``get_tracer()`` mechanism, independent of the G6e
    per-module-tracer probe above (which uses a non-matching marker
    name and so would never satisfy this category on its own). No
    provider reset needed (round 5, J2)."""
    from opentelemetry import trace as otel_trace

    tracer = otel_trace.get_tracer("audittrace.routes.chat")
    with tracer.start_as_current_span("llm.chat.completions"):
        pass


def test_g6_canary_formatted_record_is_scanned(_canary_formatted_record):
    """Dedicated per-test canary for the formatted-log-record category
    (gate REJECT round 4, R1) — any record dispatched through ANY
    logger is captured by the ``Logger.callHandlers`` hook (round 5,
    J1) and formatted via ``StructuredFormatter``, regardless of what
    is or is not attached to root at the time."""
    logging.getLogger("audittrace.canary").warning("canary formatted record")


def test_v1d_root_attach_scrubs_third_party_loggers_too():
    """S1 (gate REJECT round 4; mechanism replaced round 5 per J1) —
    ADDENDUM A §4 names a ROOT handler explicitly, not an
    "audittrace"-only one (fix3's choice): a third-party logger
    (sqlalchemy/httpx/starlette/opentelemetry/...) is NOT a descendant of
    "audittrace" in the logging hierarchy, so attaching only there left
    every such logger's output unscanned — the reviewer's V1d neuter
    (formatter skips the scrub for non-"audittrace" logger prefixes)
    stayed GREEN under that scope because the net never even captured
    the record to check. Round 5's ``Logger.callHandlers`` hook (J1)
    fires for EVERY logger's dispatched records, not just root's own
    (or any one named logger's) descendants, so this now holds by
    construction — this plants a registered secret through a literal
    third-party-shaped logger name and checks the net's OWN exposed
    capture directly."""
    from tests import conftest as conftest_mod

    register_request_secret(REGISTERED_SECRET)
    logging.getLogger("sqlalchemy.engine").warning(
        "third-party logger carries %s", REGISTERED_SECRET
    )
    captured = list(conftest_mod._last_redaction_net_records)
    joined = "\n".join(captured)
    assert REGISTERED_SECRET not in joined
    assert any("third-party logger carries" in r for r in captured), (
        "a third-party-named logger's record never reached the net's "
        "capture (gate REJECT round 4 S1 / round 5 J1)"
    )


# ═══════════════════════════ G7 — both sinks, one serialiser ══════════════


def test_g7_log_and_span_use_the_same_serialiser(monkeypatch):
    """G7 — every emission site (span input.value AND output.value, the
    DEBUG log's call_args AND call_kwargs AND result) must route through
    the SAME ``render_payload`` call, checked per FIELD.

    Gate REJECT round 1 BLOCKER 4 — the previous version only checked
    ``call_kwargs``; the reviewer's X-G7 neuters (``call_args`` and the
    OUTPUT ``result`` reverted to bare ``str(...)[:500]``) stayed GREEN
    because nothing here ever looked at those two fields."""
    sentinel = "SENTINEL-RENDER-OUTPUT"
    import audittrace.logging_config as lc

    monkeypatch.setattr(lc, "render_payload", lambda value, limit: sentinel)

    fake_span = MagicMock()
    fake_span.is_recording.return_value = True
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda *a, **kw: fake_span)
    monkeypatch.setattr(telemetry, "_tracer", MagicMock())

    logger = logging.getLogger("test.g7")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.setLevel(logging.DEBUG)
    handler.emit = records.append
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @lc.log_call(logger=logger)
    def op(x, y):
        return "some-result"

    try:
        op("anything", "else")
    finally:
        logger.removeHandler(handler)

    span_attrs = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert span_attrs.get("input.value") == sentinel
    assert span_attrs.get("output.value") == sentinel
    input_records = [r for r in records if r.getMessage().startswith("INPUT")]
    output_records = [r for r in records if r.getMessage().startswith("OUTPUT")]
    assert input_records, "expected a DEBUG INPUT record"
    assert output_records, "expected a DEBUG OUTPUT record"
    assert input_records[-1].call_args == sentinel, (
        "the DEBUG log's call_args must come from the SAME render_payload "
        "call as the span's input.value"
    )
    assert input_records[-1].call_kwargs == sentinel, (
        "the DEBUG log's call_kwargs must come from the SAME render_payload "
        "call as the span's input.value — a specific field check, not a "
        "blanket OR across the whole record, so a neuter of ONE call site "
        "cannot hide behind another"
    )
    assert output_records[-1].result == sentinel, (
        "the DEBUG log's result must come from the SAME render_payload "
        "call as the span's output.value"
    )


def test_g7b_formatter_scrubs_message_exception_and_settings_extra():
    register_request_secret(REGISTERED_SECRET)
    from audittrace.config import Settings

    logger = logging.getLogger("test.g7b")
    try:
        raise ValueError(f"fail with {REGISTERED_SECRET}")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name=logger.name,
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=f"ERROR op: {REGISTERED_SECRET}",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.settings_extra = Settings(langfuse_secret_key="sk-g7b")
    formatted = StructuredFormatter().format(record)
    assert REGISTERED_SECRET not in formatted
    assert "sk-g7b" not in formatted
    assert "<Settings>" in formatted
    assert "[REDACTED:bearer]" in formatted


def test_p22_formatter_scrubs_stack_info():
    """P22 (should-fix, gate REJECT round 2) — ``StructuredFormatter``
    already routes ``record.stack_info`` through ``scrub_text``; this
    closes the missing-TEST gap (the reviewer's neuter table found the
    removal of that one call GREEN because nothing exercised it)."""
    register_request_secret(REGISTERED_SECRET)
    logger = logging.getLogger("test.p22.stack_info")
    record = logger.makeRecord(
        logger.name, logging.WARNING, __file__, 1, "boom", (), None
    )
    record.stack_info = f"Stack (most recent call last):\n  carries {REGISTERED_SECRET}"
    formatted = StructuredFormatter().format(record)
    assert REGISTERED_SECRET not in formatted
    assert "[REDACTED:bearer]" in formatted


def test_p22_neuter_removing_stack_info_scrub_leaks(monkeypatch):
    """NEUTER for the guard above — reproduce the pre-fix ``format()``
    body (``stack_info`` assigned via ``formatStack`` with no
    ``scrub_text`` wrapper) and confirm the secret leaks."""
    register_request_secret(REGISTERED_SECRET)
    logger = logging.getLogger("test.p22.neuter")
    record = logger.makeRecord(
        logger.name, logging.WARNING, __file__, 1, "boom", (), None
    )
    record.stack_info = f"Stack (most recent call last):\n  carries {REGISTERED_SECRET}"

    formatter = StructuredFormatter()
    unscrubbed_stack = formatter.formatStack(record.stack_info)
    assert REGISTERED_SECRET in unscrubbed_stack, (
        "NEUTER SANITY: formatStack's own output must still carry the "
        "secret verbatim — scrub_text is what the fix adds on top"
    )


# ═══ P31 — scrubber reaches span link attributes (defensive, should-fix) ═══


def test_p31_scrubbing_processor_redacts_link_attributes():
    """P31 (should-fix, gate REJECT round 2) — ``ScrubbingSpanProcessor
    .on_end`` already scrubs link attributes defensively (S-C4-adjacent:
    a span can carry OTel ``Link`` objects, each with its own attribute
    mapping); this closes the missing-TEST gap the reviewer's neuter
    table found GREEN — nothing exercised the link-scrubbing loop."""
    from opentelemetry.trace import Link
    from opentelemetry.trace.span import SpanContext, TraceFlags

    register_request_secret(REGISTERED_SECRET)
    ctx = SpanContext(trace_id=1, span_id=1, is_remote=False, trace_flags=TraceFlags(0))
    link = Link(ctx, attributes={"linked.detail": f"carries {REGISTERED_SECRET}"})

    class _FakeSpan:
        _attributes: dict[str, Any] = {}
        _events: tuple[Any, ...] = ()
        _links: tuple[Any, ...] = (link,)
        _status = None

    telemetry.ScrubbingSpanProcessor().on_end(_FakeSpan())  # type: ignore[arg-type]
    assert REGISTERED_SECRET not in str(link.attributes.get("linked.detail"))
    assert "[REDACTED:bearer]" in str(link.attributes.get("linked.detail"))


def test_p31_neuter_link_scrub_loop_removed_leaks(monkeypatch):
    """NEUTER for the guard above — remove the link-scrubbing loop from
    ``on_end`` and confirm the secret survives."""
    from opentelemetry.trace import Link
    from opentelemetry.trace.span import SpanContext, TraceFlags

    def _on_end_without_links(self, span):
        from audittrace.logging_config import scrub_text as _scrub

        telemetry._scrub_bounded_attributes(getattr(span, "_attributes", None))
        for event in getattr(span, "_events", None) or ():
            telemetry._scrub_bounded_attributes(getattr(event, "_attributes", None))
        # LINK scrubbing deliberately omitted — this is the neuter.
        status = getattr(span, "_status", None)
        description = getattr(status, "_description", None)
        if status is not None and description:
            try:
                status._description = _scrub(description)
            except Exception:  # pragma: no cover - defensive
                pass

    monkeypatch.setattr(
        telemetry.ScrubbingSpanProcessor, "on_end", _on_end_without_links
    )
    register_request_secret(REGISTERED_SECRET)
    ctx = SpanContext(trace_id=1, span_id=1, is_remote=False, trace_flags=TraceFlags(0))
    link = Link(ctx, attributes={"linked.detail": f"carries {REGISTERED_SECRET}"})

    class _FakeSpan:
        _attributes: dict[str, Any] = {}
        _events: tuple[Any, ...] = ()
        _links: tuple[Any, ...] = (link,)
        _status = None

    telemetry.ScrubbingSpanProcessor().on_end(_FakeSpan())  # type: ignore[arg-type]
    assert REGISTERED_SECRET in str(link.attributes.get("linked.detail")), (
        "NEUTER SANITY: with link scrubbing removed, the secret must "
        "survive — if this fails, the reproduction has drifted"
    )


# ═══════════════════════════ G8 / S-C4 / S-C6 — scrubber mechanism ════════


def test_g8_scrubbing_processor_redacts_str_and_sequence_attributes_and_events():
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g8-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    register_request_secret(REGISTERED_SECRET)
    tracer = otel_trace.get_tracer("g8-test")
    with tracer.start_as_current_span("g8-span") as span:
        span.set_attribute("input.value", f"carries {REGISTERED_SECRET}")
        # S-C4 — a Sequence[str] attribute value.
        span.set_attribute("seq.attr", ("a", REGISTERED_SECRET, "c"))
        span.add_event("probe", {"detail": f"has {REGISTERED_SECRET}"})

    spans = [s for s in exporter.get_finished_spans() if s.name == "g8-span"]
    assert spans
    sp = spans[-1]
    assert REGISTERED_SECRET not in sp.attributes["input.value"]
    assert REGISTERED_SECRET not in str(sp.attributes["seq.attr"])
    assert "[REDACTED:bearer]" in sp.attributes["input.value"]
    event = next(e for e in sp.events if e.name == "probe")
    assert REGISTERED_SECRET not in event.attributes["detail"]

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


def test_g8_streaming_response_raising_mid_body(monkeypatch):
    """S-C6 — a StreamingResponse that raises mid-body must still have its
    SERVER span's exception text scrubbed (chat is streaming-first)."""
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="g8-stream-test",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=False,
    )
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from starlette.responses import StreamingResponse

    exporter = InMemorySpanExporter()
    otel_trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    register_request_secret(REGISTERED_SECRET)
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def gen():
            yield b"chunk-1"
            raise RuntimeError(f"mid-body failure {REGISTERED_SECRET}")

        return StreamingResponse(gen())

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pytest.skip("FastAPI OTel instrumentation unavailable")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/stream")
    assert resp.status_code in (200, 500)

    all_snapshots = []
    for sp in exporter.get_finished_spans():
        for e in sp.events:
            all_snapshots.append(str(dict(e.attributes)))
        all_snapshots.append(str(dict(sp.attributes)))
        if sp.status.description:
            all_snapshots.append(sp.status.description)
    joined = "\n".join(all_snapshots)
    assert REGISTERED_SECRET not in joined

    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


# ═══════ ADDENDUM C G8 — five real-route rows (BLOCKER 1 evidence) ════════
#
# Adapted from the reviewer's own evidence (real_route_probe.py) into a
# permanent guard: a real-length minted token through the REAL
# create_app(), covering every ADDENDUM-C-named shape — including the
# ``StatementError`` truncation case that was the whole of BLOCKER 1 —
# parametrised over shape_net on/off so D3 alone is proven sufficient.


class _G8RouteSnapshotExporter:
    def __init__(self) -> None:
        self.snaps: list[str] = []

    def export(self, spans):
        import json as _json

        from opentelemetry.sdk.trace.export import SpanExportResult

        for s in spans:
            self.snaps.append(
                _json.dumps(
                    {
                        "name": s.name,
                        "kind": str(s.kind),
                        "attributes": {
                            k: (list(v) if isinstance(v, tuple) else v)
                            for k, v in (s.attributes or {}).items()
                        },
                        "events": [
                            {"name": e.name, "attributes": dict(e.attributes or {})}
                            for e in s.events
                        ],
                        "status": s.status.description,
                        # O5 (gate REJECT round 2) — the "nothing lost"
                        # rows need the status CODE, not just whether a
                        # description string happens to be present: P50
                        # is a neuter that resets status to UNSET while
                        # leaving some description text behind.
                        "status_code": str(s.status.status_code),
                    },
                    default=str,
                )
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - SpanExporter protocol
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True


@pytest.fixture
def _g8_route_env(monkeypatch, test_container):
    import fakeredis

    from audittrace import auth as auth_mod
    from audittrace import config as config_mod
    from audittrace import dependencies
    from audittrace import identity as identity_mod

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        identity_mod,
        "_token_cache",
        identity_mod.TokenCache(fake, default_ttl_seconds=300),
    )
    monkeypatch.setattr(identity_mod, "_redis_client", fake)
    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("AUDITTRACE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", TEST_AUDIENCE)
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        auth_mod, "_fetch_jwks_keys", AsyncMock(return_value=[TEST_PUBLIC_PEM])
    )
    auth_mod._jwks_cache.clear()
    dependencies.container = test_container
    from audittrace.server import create_app

    app = create_app()
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = otel_trace.get_tracer_provider()
    processors = provider._active_span_processor._span_processors
    assert isinstance(processors[0], telemetry.ScrubbingSpanProcessor)
    snap = _G8RouteSnapshotExporter()
    provider.add_span_processor(SimpleSpanProcessor(snap))

    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, r: logging.LogRecord) -> None:
            try:
                records.append(StructuredFormatter().format(r))
            except Exception as e:  # pragma: no cover - defensive
                records.append(f"FORMAT-FAILED {type(e).__name__}")

    handler = _H()
    handler.setLevel(logging.DEBUG)
    # Gate REJECT round 2, BLOCKER V1 — attaching to the ROOT logger here
    # used to go blind: this fixture adds the handler BEFORE yielding
    # ``app`` to the test body, but the test body then enters
    # ``TestClient(app, ...)`` as a context manager, which runs
    # ``lifespan`` -> ``setup_logging()`` -> ``root.handlers.clear()`` —
    # removing this handler before a single request is sent, so
    # ``records`` stayed permanently empty (measured: fixture records =
    # 0). Attach to the "audittrace" logger instead (see the identical
    # fix + rationale on the suite-wide net fixture in conftest.py) —
    # every emission this test cares about is logged under an
    # "audittrace.*" child logger, and ``setup_logging`` never clears a
    # named logger's own handlers, only the root's.
    audittrace_logger = logging.getLogger("audittrace")
    audittrace_logger.addHandler(handler)
    old_level = audittrace_logger.level
    audittrace_logger.setLevel(logging.DEBUG)
    try:
        yield app, snap, records
    finally:
        audittrace_logger.removeHandler(handler)
        audittrace_logger.setLevel(old_level)
        config_mod.get_settings.cache_clear()


def _g8_fragment_hits(token: str, texts: list[str], n: int = 16, step: int = 5) -> int:
    """Count how many of ``token``'s ``n``-char windows (sampled every
    ``step`` chars) survive anywhere in ``texts`` — the same style of
    measurement the gate reviewer's evidence used."""
    joined = "\n".join(texts)
    windows = {token[i : i + n] for i in range(0, len(token) - n + 1, step)}
    return sum(1 for w in windows if w in joined)


@pytest.mark.parametrize("shape_net", ["on", "off"])
@pytest.mark.parametrize(
    "case",
    [
        "chat_nonstream",
        "chat_stream",
        "chat_stream_upstream_exc",
        "context_statement_error",
        "context_cause_chain",
    ],
)
def test_g8_addendum_c_real_route_rows(_g8_route_env, monkeypatch, case, shape_net):
    """ADDENDUM C G8 — a real-length minted bearer through the REAL
    create_app(), covering every named case including
    ``context_statement_error`` — BLOCKER 1's own repro: a SQLAlchemy
    ``StatementError`` whose repr TRUNCATES the token out of the middle.
    Parametrised shape_net on/off: D3 (the registry + fragment scrub)
    alone must be sufficient; the shape net is secondary."""
    app, snap, records = _g8_route_env
    import audittrace.logging_config as lc

    if shape_net == "off":
        monkeypatch.setattr(lc, "_JWT_SHAPE_RE", __import__("re").compile(r"(?!x)x"))
        monkeypatch.setattr(lc, "_AUTHZ_SHAPE_RE", __import__("re").compile(r"(?!x)x"))

    token = _mint_jwt(sub="g8-probe-user", jti="jti-g8-probe")
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": "g8-route-session"}
    body = {"model": "qwen3.5-35b", "messages": [{"role": "user", "content": "hi"}]}

    with TestClient(app, raise_server_exceptions=False) as client:
        snap.snaps.clear()
        records.clear()
        if case == "chat_nonstream":
            from tests.test_chat_proxy import (
                _FakeAsyncClient,
                _ok_chat_response,
                _patch_async_client,
            )

            with _patch_async_client(_FakeAsyncClient(post_json=_ok_chat_response())):
                resp = client.post("/v1/chat/completions", json=body, headers=headers)
        elif case == "chat_stream":
            from tests.test_chat_proxy import _FakeAsyncClient, _patch_async_client

            lines = [
                'data: {"id":"x","object":"chat.completion.chunk","choices":'
                '[{"index":0,"delta":{"content":"hey"},"finish_reason":null}]}',
                "data: [DONE]",
            ]
            with _patch_async_client(_FakeAsyncClient(stream_lines=lines)):
                resp = client.post(
                    "/v1/chat/completions",
                    json={**body, "stream": True},
                    headers=headers,
                )
        elif case == "chat_stream_upstream_exc":
            from tests.test_chat_failure_audit import _FakeStreamGenericExcClient
            from tests.test_chat_proxy import _patch_async_client

            class _ExcClient(_FakeStreamGenericExcClient):
                def stream(self, method, url, json=None, **kw):
                    class _C:
                        async def __aenter__(self):
                            raise RuntimeError(f"upstream died while holding {token}")

                        async def __aexit__(self, *a):
                            return False

                    return _C()

            with _patch_async_client(_ExcClient()):
                resp = client.post(
                    "/v1/chat/completions",
                    json={**body, "stream": True},
                    headers=headers,
                )
        else:
            from sqlalchemy.exc import StatementError

            from audittrace.services import context_builder as cb

            if case == "context_statement_error":

                async def _boom(self, *a, **k):
                    # BLOCKER 1's exact repro: SQLAlchemy truncates a long
                    # bound-parameter repr out of the MIDDLE.
                    raise StatementError(
                        "db failed", "SELECT :p", {"p": token}, ValueError("orig")
                    )
            else:

                async def _boom(self, *a, **k):
                    try:
                        raise RuntimeError(f"inner carries {token}")
                    except RuntimeError as e:
                        raise ValueError("outer") from e

            for cls in (cb.DefaultContextBuilder, cb.MockContextBuilder):
                monkeypatch.setattr(cls, "build_system_context_with_stats", _boom)
            resp = client.post("/context", json={"query": "q"}, headers=headers)

    texts = snap.snaps + records
    joined = "\n".join(texts)
    assert resp.status_code in (200, 500), (
        f"unexpected status for {case}: {resp.status_code}"
    )
    # Gate REJECT round 2, BLOCKER V1 — the log leg of this fixture used
    # to be permanently blind (``records`` always empty: entering
    # ``TestClient(app)`` above clears the ROOT logger's handlers via
    # ``setup_logging()``, wiping the fixture's root-attached handler
    # before a single request ran). Assert it actually captured
    # something — every one of these 5 cases goes through at least one
    # ``@log_call`` DEBUG INPUT/OUTPUT line or an ERROR line.
    assert len(records) > 0, (
        f"log capture captured ZERO records for case={case} "
        f"shape_net={shape_net} — the log leg of this fixture is blind "
        "(gate REJECT round 2 BLOCKER V1)"
    )
    assert token not in joined, (
        f"full token leaked for case={case} shape_net={shape_net}"
    )
    fragment_hits = _g8_fragment_hits(token, texts)
    assert fragment_hits == 0, (
        f"{fragment_hits} 16-char window(s) of the token survived for "
        f"case={case} shape_net={shape_net}"
    )


@pytest.mark.parametrize("case", ["context_statement_error", "context_cause_chain"])
def test_g8_addendum_c_nothing_lost_vs_unscrubbed_baseline(_g8_route_env, case):
    """BLOCKER V2 (gate REJECT round 2) — ADDENDUM C §1's measured table
    claims redaction loses NOTHING: each row keeps exactly 1 exception
    event, status ERROR, and the SAME 500 body the unscrubbed provider
    would have produced. No prior test asserted the status CODE (only a
    description string, which P50's neuter — resetting status to UNSET
    while still leaving text behind — can satisfy while actually
    breaking the invariant) or the event COUNT, and none compared the
    response body against a baseline at all.

    The baseline comparison is structural, not a second scrub-disabled
    request: ``unhandled_exception_handler`` (``server.py``) ALWAYS
    returns the fixed ADR-033 envelope — a generic ``"An internal error
    occurred."`` message that never echoes the exception text, scrubbed
    or not — so "the body is unaffected by the exception's content" is
    exactly what "nothing lost" means here, and is what this test
    measures directly, rather than re-deriving it from a second,
    secret-carrying request that would ALSO have to evade the
    suite-wide redaction net's own, separate shape leg."""
    app, snap, records = _g8_route_env
    token = _mint_jwt(sub="g8-nothing-lost-probe", jti="jti-g8-nothing-lost")
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": "g8-nothing-lost"}

    from audittrace.services import context_builder as cb

    if case == "context_statement_error":
        from sqlalchemy.exc import StatementError

        async def _boom(self, *a, **k):
            raise StatementError(
                "db failed", "SELECT :p", {"p": token}, ValueError("orig")
            )
    else:

        async def _boom(self, *a, **k):
            try:
                raise RuntimeError(f"inner carries {token}")
            except RuntimeError as e:
                raise ValueError("outer") from e

    import unittest.mock as _mock

    with (
        _mock.patch.object(
            cb.DefaultContextBuilder, "build_system_context_with_stats", _boom
        ),
        _mock.patch.object(
            cb.MockContextBuilder, "build_system_context_with_stats", _boom
        ),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        snap.snaps.clear()
        records.clear()
        resp = client.post("/context", json={"query": "q"}, headers=headers)

    assert resp.status_code == 500
    body = resp.json()
    error = body["error"]
    assert error["message"] == "An internal error occurred."
    assert error["type"] == "api_error"
    assert error["param"] is None
    assert error["code"] == "internal_error"
    assert error["status"] == 500
    assert error["user_facing_message"] == "Something went wrong. Please try again."
    # trace_id is the only field that legitimately varies per request —
    # "nothing lost" means everything ELSE is the fixed envelope.
    assert set(error.keys()) == {
        "message",
        "type",
        "param",
        "code",
        "status",
        "operator_hint",
        "trace_id",
        "user_facing_message",
    }

    import json as _json

    server_spans = []
    seam_spans = []
    for raw in snap.snaps:
        d = _json.loads(raw)
        if "SpanKind.SERVER" == d["kind"]:
            server_spans.append(d)
        elif d["name"] == "context-build":
            seam_spans.append(d)

    assert server_spans, (
        f"no SERVER span captured for case={case}: "
        f"{[_json.loads(r)['name'] for r in snap.snaps]}"
    )
    assert seam_spans, (
        f"no context-build seam span captured for case={case}: "
        f"{[_json.loads(r)['name'] for r in snap.snaps]}"
    )

    for label, spans in (("SERVER", server_spans), ("seam", seam_spans)):
        for d in spans:
            assert d["status_code"] == "StatusCode.ERROR", (
                f"{label} span {d['name']!r} status_code={d['status_code']!r}, "
                f"expected ERROR (case={case})"
            )
            assert len(d["events"]) == 1, (
                f"{label} span {d['name']!r} carries {len(d['events'])} "
                f"exception event(s), expected exactly 1 (case={case})"
            )


def test_g8_route_log_leg_reddens_on_an_unscrubbed_planted_log_line(
    _g8_route_env, monkeypatch
):
    """BLOCKER V1 companion — a real route handler that logs a planted
    marker must be CAUGHT by this fixture's log capture, proving
    ``records`` is wired to something live, not just non-empty by
    coincidence (e.g. an unrelated startup line). The marker is
    deliberately NOT JWT/``Bearer``-shaped and never registered via
    ``register_request_secret`` — this test's own concern is "does the
    capture mechanism see a real route's log output at all", which is
    orthogonal to the suite-wide redaction net's shape/registered-value
    legs (``tests/conftest.py``); a JWT-shaped or registered plant here
    would additionally (and separately) trip that net, conflating two
    different guards in one test."""
    app, snap, records = _g8_route_env
    import uuid

    from audittrace.services import context_builder as cb

    marker = "UNSCRUBBED-LOG-PLANT-" + uuid.uuid4().hex
    headers = {
        "Authorization": f"Bearer {_mint_jwt(sub='g8-log-leak-probe')}",
        "X-Session-Id": "g8-log-leak",
    }

    probe_logger = logging.getLogger("audittrace.test_g8_log_leak_probe")

    async def _boom(self, *a, **k):
        # Deliberately bypass scrub_text — simulates a future log call
        # that forgot to route through it.
        probe_logger.error("UNSCRUBBED PLANT carries %s", marker)
        raise ValueError("boom")

    for cls in (cb.DefaultContextBuilder, cb.MockContextBuilder):
        monkeypatch.setattr(cls, "build_system_context_with_stats", _boom)

    with TestClient(app, raise_server_exceptions=False) as client:
        snap.snaps.clear()
        records.clear()
        client.post("/context", json={"query": "q"}, headers=headers)

    joined = "\n".join(records)
    assert marker in joined, (
        "a deliberately-unscrubbed planted log line through a real "
        "route must be visible in `records` — if this assertion fails, "
        "the log capture is blind (gate REJECT round 2 BLOCKER V1)"
    )


def test_g8_addendum_c_truncation_shape_row_exact_match_only_would_leak(
    _g8_route_env, monkeypatch
):
    """Companion to the parametrised row above, isolating the EXACT
    regression BLOCKER 1 fixed: with the fragment scan DISABLED (exact
    match only — the behaviour before this fix round), the
    ``context_statement_error`` case's head/tail fragments survive.
    Proves the fragment scan added for BLOCKER 1 is load-bearing for
    THIS specific route+exception combination, not just for the unit
    tests in the D3-fragment-leg section above."""
    app, snap, records = _g8_route_env
    import audittrace.logging_config as lc

    # Exact-match-only: reproduce scrub_text's pre-fix body (no fragment
    # scan branch) via monkeypatch, rather than editing the source file —
    # this test's OWN existence as a permanent regression guard is
    # distinct from the one-time source-edit neuter proof run separately.
    def _exact_match_only_scrub_text(value: str) -> str:
        if not value:
            return value
        from audittrace.identity import request_secrets

        secrets = request_secrets()
        for secret in sorted(secrets, key=len, reverse=True):
            if secret and secret in value:
                value = value.replace(secret, "[REDACTED:bearer]")
        value = lc._JWT_SHAPE_RE.sub("[REDACTED:jwt]", value)
        value = lc._AUTHZ_SHAPE_RE.sub("[REDACTED:authz]", value)
        return value

    monkeypatch.setattr(lc, "scrub_text", _exact_match_only_scrub_text)
    # telemetry.py imports scrub_text locally inside on_end/_scrub_bounded_attributes
    # (lazy import), so patching the logging_config attribute is sufficient —
    # no separate telemetry-side patch needed.

    token = _mint_jwt(sub="g8-truncation-probe", jti="jti-g8-trunc")
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": "g8-trunc-session"}

    with TestClient(app, raise_server_exceptions=False) as client:
        snap.snaps.clear()
        records.clear()
        from sqlalchemy.exc import StatementError

        from audittrace.services import context_builder as cb

        async def _boom(self, *a, **k):
            raise StatementError(
                "db failed", "SELECT :p", {"p": token}, ValueError("orig")
            )

        for cls in (cb.DefaultContextBuilder, cb.MockContextBuilder):
            monkeypatch.setattr(cls, "build_system_context_with_stats", _boom)
        client.post("/context", json={"query": "q"}, headers=headers)

    texts = snap.snaps + records
    fragment_hits = _g8_fragment_hits(token, texts)
    assert fragment_hits > 0, (
        "NEUTER SANITY: with the fragment scan disabled, SQLAlchemy's "
        "StatementError truncation must still leak >=1 16-char window — "
        "if this assertion fails, the exact-match-only reproduction "
        "above has drifted from scrub_text's real pre-fix shape"
    )


def test_g8c_processor_and_exporter_construction_sites_frozen():
    """AST-derived equality: add_span_processor/*SpanExporter/*SpanProcessor/
    Langfuse( construction sites in telemetry.py match a frozen count."""
    path = SRC_ROOT / "telemetry.py"
    tree = ast.parse(path.read_text())
    add_span_processor_calls = 0
    span_exporter_constructions = 0
    span_processor_constructions = 0
    langfuse_constructions = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "add_span_processor":
                add_span_processor_calls += 1
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name and name.endswith("SpanExporter") and name != "SpanExporter":
                span_exporter_constructions += 1
            if name in ("BatchSpanProcessor", "SimpleSpanProcessor"):
                span_processor_constructions += 1
            if name == "Langfuse":
                langfuse_constructions += 1
    assert add_span_processor_calls >= 1
    assert span_exporter_constructions == 1  # OTLPSpanExporter
    assert span_processor_constructions >= 2  # OTLP batch + self-test probe
    assert langfuse_constructions == 1


_TRACING_CONSTRUCTION_CALL_NAMES = frozenset(
    {"TracerProvider", "add_span_processor", "get_tracer"}
)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _find_tracing_construction_sites() -> list[tuple[str, str, str]]:
    """AST-walk EVERY file under src/audittrace/ (not just telemetry.py)
    for a get_tracer(/TracerProvider(/*SpanExporter(/add_span_processor(
    call site. Returns a SORTED list of (relative-path, enclosing-def-or-
    "<module>", name) — an EQUALITY pin, not a count: a rogue module
    instantiating its own private TracerProvider/exporter (gate REJECT
    round 1's X-G6e/X-G8c neuter) adds a NEW tuple, which a bare
    ``len(...) >= N`` count-style check would never notice.

    Pinned by SYMBOL (gate REJECT round 1 fix1 re-review should-fix), not
    line number: an unrelated edit above a site no longer spuriously
    reddens this test. Duplicate (file, def, name) triples are NOT
    deduplicated — ``init_telemetry`` legitimately calls
    ``add_span_processor`` twice (the scrubber, then the OTLP batch
    processor) and ``get_tracer`` twice; collapsing to a set would make
    removing ONE of a pair invisible to this pin.
    """
    sites: list[tuple[str, str, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = str(path.relative_to(SRC_ROOT.parent.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name is None:
                continue
            if name in _TRACING_CONSTRUCTION_CALL_NAMES or (
                name.endswith("SpanExporter") and name != "SpanExporter"
            ):
                enclosing = _enclosing_def_name(tree, node.lineno) or "<module>"
                sites.append((rel, enclosing, name))
    return sorted(sites)


# FROZEN — the complete, enumerated set of tracing-construction call sites
# across the ENTIRE src/ tree, as measured against this commit. Regenerate
# with ``_find_tracing_construction_sites()`` and update BY HAND (never
# programmatically regenerate-and-assert-equal-to-itself, or the pin loses
# its teeth) whenever a legitimate new site is added.
_FROZEN_TRACING_CONSTRUCTION_SITES: tuple[tuple[str, str, str], ...] = (
    ("src/audittrace/routes/_memory_tool_loop.py", "<module>", "get_tracer"),
    ("src/audittrace/routes/chat.py", "<module>", "get_tracer"),
    ("src/audittrace/services/decision/client.py", "__init__", "get_tracer"),
    ("src/audittrace/services/session_summarizer.py", "<module>", "get_tracer"),
    (
        "src/audittrace/telemetry.py",
        "_run_startup_scrub_self_test",
        "add_span_processor",
    ),
    ("src/audittrace/telemetry.py", "_run_startup_scrub_self_test", "get_tracer"),
    ("src/audittrace/telemetry.py", "init_telemetry", "OTLPSpanExporter"),
    ("src/audittrace/telemetry.py", "init_telemetry", "TracerProvider"),
    ("src/audittrace/telemetry.py", "init_telemetry", "add_span_processor"),
    ("src/audittrace/telemetry.py", "init_telemetry", "add_span_processor"),
    ("src/audittrace/telemetry.py", "init_telemetry", "get_tracer"),
    ("src/audittrace/telemetry.py", "init_telemetry", "get_tracer"),
)


def test_g6e_tracing_construction_sites_are_frozen_src_wide():
    """G6e/G6b (gate REJECT round 1 BLOCKER 3) — equality, not a count,
    over EVERY get_tracer(/TracerProvider(/*SpanExporter(/
    add_span_processor( site in the WHOLE src/ tree. A rogue module
    building its own private provider+exporter (the reviewer's
    X-G6e/X-G8c neuter: a new ``_review_rogue_tracing.py`` with its own
    ``TracerProvider()``+``SimpleSpanProcessor(ConsoleSpanExporter())``,
    including the ALIASED-import variant) adds a new tuple here — the
    only thing that can make this pass again is deleting the planted
    file, not widening a count.

    If this test reddens because a LEGITIMATE new site was added,
    regenerate the frozen tuple with ``_find_tracing_construction_sites()``
    printed to stdout and update it BY HAND, with a one-line reason in
    the commit — never by copy-pasting the function's own output back
    in via a helper that bypasses review."""
    actual = _find_tracing_construction_sites()
    assert actual == list(_FROZEN_TRACING_CONSTRUCTION_SITES)


def test_g6c_net_log_capture_survives_lifespan(app):
    """V1 companion (gate REJECT round 2; mechanism replaced round 5 per
    J1) — the suite-wide net's log capture must survive
    ``TestClient(app)``'s lifespan, which calls ``setup_logging()``
    (``server.py``), which clears the ROOT logger's handler list. Round
    5's ``Logger.callHandlers`` hook (J1) does not depend on any handler
    surviving at all — it fires for every dispatched record regardless
    of root's handler list — so this test is now a straightforward
    regression guard rather than a timing-sensitive reproduction: open
    ``TestClient(app)`` for real, drive a request through lifespan
    startup, and confirm a startup line logged strictly AFTER
    ``setup_logging()`` runs was still captured."""
    from tests import conftest as conftest_mod

    conftest_mod._last_redaction_net_records.clear()
    with TestClient(app) as c:
        c.get("/health")
    # "Starting audittrace-server" is logged by ``audittrace.server``
    # from INSIDE ``lifespan`` -- strictly AFTER ``setup_logging()``
    # itself runs (``server.py``: setup_logging() is called, THEN this
    # line is logged). Scoped to the "audittrace." logger namespace
    # deliberately: a line from an unrelated logger (e.g. the test HTTP
    # client's own "HTTP Request: ..." line) would also be captured by
    # the round-5 hook (it captures every logger now, see
    # test_v1d_root_attach_scrubs_third_party_loggers_too below), but
    # scoping here keeps this test's own assertion unambiguous about
    # WHICH line it is checking for.
    startup_lines = [
        r
        for r in conftest_mod._last_redaction_net_records
        if '"logger": "audittrace.' in r and "Starting audittrace-server" in r
    ]
    assert startup_lines, (
        "the suite-wide net captured no post-setup_logging() "
        "'Starting audittrace-server' line for a lifespan-driven test "
        "(gate REJECT round 2 BLOCKER V1 / round 5 J1)"
    )


def test_g6e_per_module_tracer_lands_on_the_net_capture():
    """G6e per-module probe half (should-fix, fix1 re-review; simplified
    round 5 per J2): the AST frozen set above only proves WHERE a module
    CALLS ``get_tracer`` — not that a span from that call actually gets
    scanned.

    Pre-round-5 this needed a provider reset + a "net-attached exporter"
    check, because the OLD mechanism depended on being attached to the
    CURRENT global provider, and OTel's ``ProxyTracer`` permanently
    caches its FIRST-resolved real tracer (``opentelemetry/trace/
    __init__.py:420-433``) — a module's already-imported ``_tracer``
    singleton could be bound to a provider this suite had since
    replaced. Round 5's ``Span.end`` hook (J2) needs none of that: it
    captures every span regardless of which provider or ProxyTracer
    delivered it, so this probe simply uses each module's OWN
    ``get_tracer(modname)`` call directly and checks the hook's own
    global snapshot list."""
    from opentelemetry import trace as otel_trace

    from tests.conftest import _SPAN_END_SNAPSHOTS

    module_names = (
        "audittrace.routes._memory_tool_loop",
        "audittrace.routes.chat",
        "audittrace.services.session_summarizer",
    )
    checked = 0
    for modname in module_names:
        tracer = otel_trace.get_tracer(modname)
        before = len(_SPAN_END_SNAPSHOTS)
        marker_name = f"g6e-probe-{modname}"
        with tracer.start_as_current_span(marker_name):
            pass
        after_names = [
            __import__("json").loads(s).get("name")
            for s in _SPAN_END_SNAPSHOTS[before:]
        ]
        assert marker_name in after_names, (
            f"get_tracer({modname!r}) did not deliver a span to the "
            "Span.end capture hook"
        )
        checked += 1
    assert checked == len(module_names)


def test_g8c_scrubber_is_index_zero_with_and_without_otlp(monkeypatch):
    # Mocked exporter class — a real "http://otel.invalid" endpoint would
    # spend several seconds retrying on a background BatchSpanProcessor
    # thread (DNS failure + exponential backoff), which is slow and noisy
    # without proving anything more than "the construction call happened".
    import sys

    fake_trace_mod = MagicMock()
    fake_trace_mod.OTLPSpanExporter = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_trace_mod,
    )

    for otlp_endpoint in ("", "http://otel.test"):
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="g8c-test",
            otlp_endpoint=otlp_endpoint,
            tracing_enabled=True,
            metrics_enabled=False,
        )
        from opentelemetry import trace as otel_trace

        provider = otel_trace.get_tracer_provider()
        processors = provider._active_span_processor._span_processors
        assert isinstance(processors[0], telemetry.ScrubbingSpanProcessor), (
            f"scrubber not first for otlp_endpoint={otlp_endpoint!r}"
        )
    telemetry._reset_for_tests()
    telemetry.init_telemetry(
        service_name="audittrace-server-tests",
        otlp_endpoint="",
        tracing_enabled=True,
        metrics_enabled=True,
    )


def test_p38_init_telemetry_wiring_calls_assert_scrubber_is_first(monkeypatch):
    """P38 (should-fix, gate REJECT round 2) —
    ``test_g8c_scrubber_is_index_zero_with_and_without_otlp`` above (and
    ``_assert_scrubber_is_first``'s own unit tests under S-C1) only
    prove the FUNCTION's logic; neither proves ``init_telemetry`` itself
    CALLS it. Monkeypatch the function to unconditionally raise and
    drive ``init_telemetry`` through its real startup path — if the
    call site were ever removed, nothing else would propagate the
    raise, and this test would stay GREEN against a removed wiring."""
    telemetry._reset_for_tests()

    def _boom(_provider: Any) -> None:
        raise RuntimeError("P38-PROBE: _assert_scrubber_is_first called")

    monkeypatch.setattr(telemetry, "_assert_scrubber_is_first", _boom)
    try:
        with pytest.raises(RuntimeError, match="P38-PROBE"):
            telemetry.init_telemetry(
                service_name="p38-wiring-test",
                otlp_endpoint="",
                tracing_enabled=True,
                metrics_enabled=False,
            )
    finally:
        # Undo the patch BEFORE re-initialising for the rest of the
        # suite — monkeypatch's own teardown only runs AFTER this test
        # function returns, so the re-init below would otherwise call
        # the still-patched ``_boom`` too and raise outside the
        # ``pytest.raises`` block.
        monkeypatch.undo()
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


# ═════════ G6b — tests/-wide _tracer escape-site equality (BUILT) ═════════
#
# Gate REJECT round 2 BLOCKER O3 — G6e above pins every tracing-
# construction call site across src/, but ADDENDUM A §4's G6b ("the
# AST-derived set of setattr(telemetry, "_tracer", ...) and
# set_tracer_provider( sites in tests/ == a frozen list") was never
# BUILT: no test anywhere walked tests/ for these two escape-hatch
# shapes. The reviewer's own T6 neuter (a planted tests/ file using
# BOTH shapes) measured this directly: nothing reddened. Those two
# calls are a LEGITIMATE, intentional way for a test to point a module
# at a provider/tracer that is NOT the net-attached global one (G6b
# does not forbid them — it freezes WHERE they are, so a new one is a
# conscious addition, never a silent one).


def _enclosing_def_name_in_tree(tree: ast.AST, lineno: int) -> str | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                if best is None or node.lineno > best.lineno:
                    best = node
    return best.name if best else None


def _find_tracer_escape_sites_in_tests() -> list[tuple[str, str, str]]:
    """AST-walk every file under ``tests/`` (never ``src/`` — that is
    G6e's job above) for a ``setattr(telemetry, "_tracer", ...)`` or
    ``set_tracer_provider(`` call site. Returns a SORTED list of
    (relative-path, enclosing-def-or-"<module>", shape) — an EQUALITY
    pin, like G6e, not a count. ``neuter_fixture``/``neuter_fixture_src``
    are excluded for the same reason ``conftest.py``'s own
    ``collect_ignore`` excludes them from normal collection: they are
    static templates copied into a THROWAWAY git repo by a different
    test, never collected or executed as part of this tree."""
    tests_root = SRC_ROOT.parent.parent / "tests"
    sites: list[tuple[str, str, str]] = []
    for path in sorted(tests_root.rglob("*.py")):
        rel = str(path.relative_to(SRC_ROOT.parent.parent))
        if "neuter_fixture" in rel:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name == "setattr":
                args = node.args
                if len(args) < 2:
                    continue
                target = args[0]
                attr_arg = args[1]
                target_name = None
                if isinstance(target, ast.Name):
                    target_name = target.id
                elif isinstance(target, ast.Attribute):
                    target_name = target.attr
                attr_value = (
                    attr_arg.value if isinstance(attr_arg, ast.Constant) else None
                )
                if target_name == "telemetry" and attr_value == "_tracer":
                    enclosing = _enclosing_def_name_in_tree(tree, node.lineno) or (
                        "<module>"
                    )
                    sites.append((rel, enclosing, "setattr(telemetry,_tracer)"))
            elif name == "set_tracer_provider":
                enclosing = _enclosing_def_name_in_tree(tree, node.lineno) or (
                    "<module>"
                )
                sites.append((rel, enclosing, "set_tracer_provider"))
    return sorted(sites)


# FROZEN — the complete, enumerated set of ``telemetry._tracer``/
# ``set_tracer_provider`` escape sites across the ENTIRE tests/ tree, as
# measured against this commit (20: 17 ``setattr`` + 3
# ``set_tracer_provider`` — ADDENDUM A fact A6 measured 14+3=17 at the
# pre-fix base; this build's OWN G5/G5b/G7 tests legitimately added 3
# more ``setattr`` sites). Regenerate with
# ``_find_tracer_escape_sites_in_tests()`` and update BY HAND whenever a
# legitimate new site is added — never programmatically.
_FROZEN_TRACER_ESCAPE_SITES_IN_TESTS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/console_store/test_base_stamping.py",
        "test_trace_id_is_none_when_no_span_is_active_during_the_write",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_chat_failure_audit.py",
        "_real_tracer_provider",
        "set_tracer_provider",
    ),
    (
        "tests/test_console_acl_routes_rls_postgres.py",
        "recording",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_console_acl_routes_rls_postgres.py",
        "test_r7_filter_is_rls_scoped_to_the_caller",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_console_acl_routes_rls_postgres.py",
        "test_trace_linkage_response_audit_and_entry_agree",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_console_acl_write_routes.py",
        "global_recording_provider",
        "set_tracer_provider",
    ),
    (
        "tests/test_console_acl_write_routes.py",
        "recording_tracer",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_log_call_redaction.py",
        "test_g5_composition_matrix_log_call_scrubs_every_combination",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_log_call_redaction.py",
        "test_g5b_generated_signature_cloned_matrix_plants_every_parameter_and_return",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_log_call_redaction.py",
        "test_g7_log_and_span_use_the_same_serialiser",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_log_call_user_id.py",
        "span_exporter",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_memory_upload_handler.py",
        "test_propagates_active_traceparent",
        "set_tracer_provider",
    ),
    (
        "tests/test_observability.py",
        "test_log_call_emits_input_for_self_only_methods",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_log_call_emits_output_for_none_returning_function",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_log_call_payload_opt_out_keeps_span_but_drops_input_and_output",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_set_current_span_attributes_mirrors_langfuse_keys",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_set_current_span_attributes_no_op_when_uninitialised",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_set_current_span_attributes_skips_none_values",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_start_span_drops_none_metadata_values",
        "setattr(telemetry,_tracer)",
    ),
    (
        "tests/test_observability.py",
        "test_start_span_uses_otel_tracer",
        "setattr(telemetry,_tracer)",
    ),
)


def test_g6b_tracer_escape_sites_in_tests_are_frozen():
    """G6b (gate REJECT round 2, BUILT per orchestrator ruling) —
    equality, not a count, over every ``setattr(telemetry, "_tracer",
    ...)``/``set_tracer_provider(`` site across the WHOLE ``tests/``
    tree. A new such site (the reviewer's T6 neuter: a planted
    ``tests/`` file using both shapes) adds a new tuple here — the only
    thing that can make this pass again is deleting the planted file or
    deliberately widening the frozen tuple BY HAND, never a count."""
    actual = _find_tracer_escape_sites_in_tests()
    assert actual == list(_FROZEN_TRACER_ESCAPE_SITES_IN_TESTS)


# ═══════════════════════════ G9 / S-C5 — header-capture refusal ═══════════


def test_g9_benign_capture_header_starts_and_widens_sanitize_fields(monkeypatch):
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", "x-session-id"
    )
    monkeypatch.delenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS", raising=False
    )
    telemetry._refuse_if_header_capture_configured()
    import os

    sanitize = os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"]
    for name in ("authorization", "cookie", "set-cookie", "proxy-authorization"):
        assert name in sanitize


@pytest.mark.parametrize(
    "entry",
    ["authorization", "auth.*", "AUTHORIZATION", "cookie", ".*", "("],
)
def test_g9_dangerous_or_uncompilable_entry_refuses_to_start(monkeypatch, entry):
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", entry
    )
    with pytest.raises(RuntimeError):
        telemetry._refuse_if_header_capture_configured()


_STATIC_CONFIG_GLOBS = ("charts/**/*.yaml", "charts/**/*.yml", "docker-compose*.yml")
_SRC_SCAN_GLOBS = ("src/**/*.py",)


def _iter_static_config_files() -> Any:
    repo_root = SRC_ROOT.parent.parent
    for pattern in _STATIC_CONFIG_GLOBS:
        yield from repo_root.glob(pattern)


def _iter_src_files() -> Any:
    repo_root = SRC_ROOT.parent.parent
    for pattern in _SRC_SCAN_GLOBS:
        yield from repo_root.glob(pattern)


# Gate REJECT round 2, O2/V4 — the previous YAML-structural walk only
# matched the capture-headers var name used as a MAPPING KEY
# (``CAPTURE_HEADERS_SERVER_REQUEST: authorization``). The chart's own
# ``deployment.yaml`` templates its env entries in the Kubernetes-NATIVE
# two-key shape (``- name: <VAR>`` / ``value: <VALUE>``, see
# ``charts/audittrace/templates/memory-server/deployment.yaml``) — the
# var name there is a VALUE under the "name" key, not a mapping key, so
# the old walk never matched it even when YAML parsing succeeded. Worse,
# every ``templates/*.yaml`` file FAILS plain ``yaml.safe_load`` outright
# (Helm's ``{{ ... }}`` Go-template syntax is not valid YAML once it
# appears mid-scalar), so the structural walk never even reached that
# file's content — it silently fell through the "CAPTURE_HEADERS not in
# text" skip. A single regex-based TEXT scan (not a YAML parse) handles
# Helm templates, native k8s env entries, AND arbitrary Python source
# uniformly, and does not depend on the file being valid YAML at all.
_CAPTURE_HEADER_FAMILY_RE = re.compile(
    r"OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_(?:SERVER|CLIENT)_(?:REQUEST|RESPONSE)"
)
# Single-line ``KEY: value`` / ``KEY = "value"`` / ``KEY"] = "value"``
# shape — matches within a short run of non-newline characters after the
# var name, so it survives surrounding punctuation (a closing quote and
# bracket before a Python ``os.environ[...] = ...`` assignment) without
# crossing onto an unrelated following line.
_INLINE_VALUE_RE = re.compile(r'[^\n]{0,15}?[:=]\s*[\'"]?([^\'"\n,}]+)')
# Kubernetes-native two-key shape: the var name appears as the VALUE of
# a "name" key, and the actual env value is the SIBLING "value" key on
# the next line.
_NATIVE_NAME_VALUE_RE = re.compile(r'[\'"]?\s*\n\s*value:\s*[\'"]?([^\'"\n]+)')


def _dangerous_capture_header_entries_in_text(text: str) -> list[str]:
    """Scan raw TEXT (works even when the file is not valid YAML, e.g. a
    Helm template, or is not YAML at all, e.g. Python source) for a
    CAPTURE_HEADERS family var name assigned a value that could reach a
    sensitive header, in either the inline ``KEY: value`` shape or the
    k8s-native ``- name: KEY`` / ``value: VALUE`` two-line shape."""
    hits: list[str] = []
    for match in _CAPTURE_HEADER_FAMILY_RE.finditer(text):
        name = match.group(0)
        tail = text[match.end() : match.end() + 80]
        inline = _INLINE_VALUE_RE.match(tail)
        if inline:
            for entry in inline.group(1).split(","):
                if telemetry._capture_entry_matches_sensitive(entry):
                    hits.append(f"{name}={entry!r} (inline KEY:value form)")
        native = _NATIVE_NAME_VALUE_RE.match(tail)
        if native:
            for entry in native.group(1).split(","):
                if telemetry._capture_entry_matches_sensitive(entry):
                    hits.append(f"{name}={entry!r} (k8s-native name/value form)")
    return hits


def test_g9_static_config_never_sets_a_dangerous_capture_header_entry():
    """G9 static-file check (gate REJECT round 1 BLOCKER 3, an
    "undeclared-missing guard"; extended round 2 O2/V4) — scan every
    deployable config file (``charts/``, ``docker-compose*.yml``) AND
    every ``src/`` Python source file for the capture-headers env var
    family, in BOTH the inline-mapping and the k8s-native ``name:``/
    ``value:`` shapes, and verify no statically-configured value could
    match a sensitive header. The RUNTIME refusal
    (``_refuse_if_header_capture_configured``) is the backstop that
    fires at pod startup regardless of this check, but a static config
    file (or a hardcoded default in source) carrying a dangerous entry
    today would crash-loop the pod in production — catching it here is
    cheaper, at commit time."""
    hits: list[str] = []
    for path in list(_iter_static_config_files()) + list(_iter_src_files()):
        text = path.read_text()
        if "CAPTURE_HEADERS" not in text:
            continue
        for hit in _dangerous_capture_header_entries_in_text(text):
            hits.append(f"{path}: {hit}")
    assert not hits, f"static config sets a dangerous capture-headers entry: {hits}"


def test_g9s_neuter_native_name_value_env_form_reddens(tmp_path):
    """V4 companion (gate REJECT round 2; round 4 R4 — never write into
    the real ``charts/`` tree, which could leave a dangerous file
    behind if the test process were killed mid-run) — the reviewer's
    own measured neuter shape: a chart file setting
    ``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST`` to
    ``authorization`` via the k8s-native ``- name:``/``value:`` two-key
    env form the real ``deployment.yaml`` template uses. Written ONLY
    under pytest's own ``tmp_path`` (never ``charts/``), and the
    detection function is called on its TEXT content directly — proves
    the NEW detection leg the test above added, not just the untouched
    inline-mapping leg, with zero filesystem risk to the real tree."""
    plant_path = tmp_path / "_g9s_native_form_probe.yaml"
    plant_path.write_text(
        "env:\n"
        "  - name: OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST\n"
        "    value: authorization\n"
    )
    text = plant_path.read_text()
    hits = _dangerous_capture_header_entries_in_text(text)
    assert hits, (
        "NEUTER SANITY: the k8s-native name/value form must be "
        "detected as dangerous — if this fails, the detection leg "
        "itself regressed, not just the test above's file list"
    )


def test_g9_wiring_init_telemetry_itself_refuses(monkeypatch):
    """G9 WIRING (gate REJECT round 1 BLOCKER 3) — call ``init_telemetry``
    itself, not just the ``_refuse_if_header_capture_configured`` helper
    directly: proves the ENTRYPOINT actually calls the check. A neuter
    that removes the call site from ``init_telemetry`` (leaving the
    helper function itself intact and separately unit-tested) would stay
    GREEN against ``test_g9_dangerous_or_uncompilable_entry_refuses_to_start``
    — that test never goes through ``init_telemetry`` at all."""
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", "authorization"
    )
    telemetry._reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="G9"):
            telemetry.init_telemetry(
                service_name="g9-wiring-test",
                otlp_endpoint="",
                tracing_enabled=True,
                metrics_enabled=False,
            )
    finally:
        monkeypatch.delenv(
            "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", raising=False
        )
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


# ═══════════════════════════ S-C1 — startup self-test ═════════════════════


def test_s_c1_self_test_fails_closed_when_scrubber_is_neutered():
    """NEUTER: a scrubber whose on_end does nothing must make
    init_telemetry refuse to start."""

    def _noop_on_end(self, span):
        pass

    original_on_end = telemetry.ScrubbingSpanProcessor.on_end
    telemetry.ScrubbingSpanProcessor.on_end = _noop_on_end
    telemetry._reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="startup self-test FAILED"):
            telemetry.init_telemetry(
                service_name="s-c1-neuter-test",
                otlp_endpoint="",
                tracing_enabled=True,
                metrics_enabled=False,
            )
    finally:
        telemetry.ScrubbingSpanProcessor.on_end = original_on_end
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )


# Gate REJECT round 1 BLOCKER 2 — the two checks inline inside
# ``_run_startup_scrub_self_test`` were extracted to
# ``telemetry._check_self_test_result`` / ``telemetry._assert_scrubber_is_first``
# specifically so each is independently unit-testable, below. The
# reviewer's neuter table showed BOTH checks vacuous when the ONLY way to
# exercise them was the full self-test flow: with the scrubber still
# functioning normally, the marker was never actually present in the
# snapshot either way, so neutering the check's OWN ``if`` condition
# (``if marker in snapshot:`` -> ``if False:``, or the index-0 pin's
# condition -> ``if False:``) was unobservable — nothing in the full flow
# ever reached the branch that mattered. Calling the extracted functions
# directly, with a hand-built snapshot/provider, isolates each check.


def test_s_c1_check_self_test_result_catches_marker_leak():
    with pytest.raises(RuntimeError, match="a planted secret reached"):
        telemetry._check_self_test_result(
            "SECRET-MARKER-XYZ",
            ['{"a": "[REDACTED:bearer] but SECRET-MARKER-XYZ also leaked"}'],
        )


def test_s_c1_check_self_test_result_catches_missing_redaction_marker():
    with pytest.raises(RuntimeError, match="redaction marker never appeared"):
        telemetry._check_self_test_result(
            "SECRET-MARKER-XYZ", ['{"a": "nothing redacted here"}']
        )


def test_s_c1_check_self_test_result_passes_when_clean():
    telemetry._check_self_test_result(
        "SECRET-MARKER-XYZ", ['{"a": "[REDACTED:bearer] only"}']
    )  # must not raise


def test_s_c1_assert_scrubber_is_first_catches_wrong_order():
    from opentelemetry.sdk.trace import TracerProvider

    tp = TracerProvider()
    tp.add_span_processor(telemetry.ScrubbingSpanProcessor())
    sp = tp._active_span_processor
    real_processors = sp._span_processors
    try:
        # Simulate a later addition landing ahead via a direct tuple
        # rebuild — production code never writes this private list
        # (S-C2 forbids it); this test constructs the exact violation
        # the pin exists to catch.
        sp._span_processors = (object(), real_processors[0])
        with pytest.raises(RuntimeError, match="S-C1a"):
            telemetry._assert_scrubber_is_first(tp)
    finally:
        # Restore a real, shutdown()-able tuple before this throwaway
        # provider's atexit-registered shutdown() runs at process exit —
        # otherwise it calls .shutdown() on the planted bare object().
        sp._span_processors = real_processors
        tp.shutdown()


def test_s_c1_assert_scrubber_is_first_passes_when_correct():
    from opentelemetry.sdk.trace import TracerProvider

    tp = TracerProvider()
    tp.add_span_processor(telemetry.ScrubbingSpanProcessor())
    try:
        telemetry._assert_scrubber_is_first(tp)  # must not raise
    finally:
        tp.shutdown()


def test_s_c1_self_test_plants_and_checks_a_status_description(monkeypatch):
    """BLOCKER 2's own counter-example (reviewer's probe_sc1.py mechanism):
    before the status plant, a later SDK renaming ``_status``/
    ``_description`` failed OPEN because nothing in the self-test ever
    put a marker into a span's STATUS description to begin with. Hiding
    ``_status`` from the scrubber's ``on_end`` (simulating the rename)
    must now make ``init_telemetry`` refuse."""

    original_on_end = telemetry.ScrubbingSpanProcessor.on_end

    def _hiding_on_end(self: Any, span: Any) -> None:
        class _HidingView:
            def __getattr__(self, name: str) -> Any:
                if name == "_status":
                    raise AttributeError(name)
                return getattr(span, name)

        return original_on_end(self, _HidingView())

    monkeypatch.setattr(telemetry.ScrubbingSpanProcessor, "on_end", _hiding_on_end)
    telemetry._reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="startup self-test FAILED"):
            telemetry.init_telemetry(
                service_name="s-c1-status-rename-test",
                otlp_endpoint="",
                tracing_enabled=True,
                metrics_enabled=False,
            )
    finally:
        # Restore BEFORE reinitialising — monkeypatch's own fixture-scoped
        # teardown runs AFTER this function returns, which would be too
        # late: init_telemetry below would otherwise re-run the self-test
        # with the hiding view still active and fail again.
        monkeypatch.setattr(telemetry.ScrubbingSpanProcessor, "on_end", original_on_end)
        telemetry._reset_for_tests()
        telemetry.init_telemetry(
            service_name="audittrace-server-tests",
            otlp_endpoint="",
            tracing_enabled=True,
            metrics_enabled=True,
        )
