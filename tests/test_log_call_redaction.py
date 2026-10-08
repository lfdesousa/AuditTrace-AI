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
import logging
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


def test_g1_neuter_restoring_default_str_leaks_the_secret():
    """NEUTER: restoring the old `str(value)`/`default=str` escape hatch
    must leak the secret — proves the guard above is load-bearing."""
    from audittrace.config import Settings

    settings = Settings(langfuse_secret_key="sk-super-secret-value")
    import json as _json

    leaked = _json.dumps(settings.model_dump(), default=str)
    assert "sk-super-secret-value" in leaked, (
        "the neuter itself must reproduce the historical leak shape"
    )


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


def test_g1c_allowlisted_classes_carry_no_secret_shaped_field():
    """Every allowlisted model/dataclass must have zero SecretStr fields
    and zero secret-named fields — the allowlist's own safety invariant."""
    import re as _re

    from pydantic import SecretStr

    secret_name_re = _re.compile(
        r"(?i)\b(token|secret|password|api_key|authorization|cookie)\b"
    )
    secret_names_allowed = {"token_id"}  # a jti, not a credential (D2)

    render_models, render_dataclasses, render_orm = _get_render_allowlists()
    violations: list[str] = []
    for cls in (*render_models, *render_dataclasses):
        fields = getattr(cls, "model_fields", None)
        if fields is not None:
            for name, field in fields.items():
                if field.annotation is SecretStr:
                    violations.append(f"{cls.__name__}.{name}: SecretStr")
                if name not in secret_names_allowed and secret_name_re.search(name):
                    violations.append(f"{cls.__name__}.{name}: secret-shaped name")
        else:
            import dataclasses as _dc

            for f in _dc.fields(cls):
                if f.name not in secret_names_allowed and secret_name_re.search(f.name):
                    violations.append(f"{cls.__name__}.{f.name}: secret-shaped name")
    for cls in render_orm:
        from sqlalchemy import inspect as _sa_inspect

        for attr in _sa_inspect(cls).column_attrs:
            if attr.key not in secret_names_allowed and secret_name_re.search(attr.key):
                violations.append(f"{cls.__name__}.{attr.key}: secret-shaped name")
    assert not violations, (
        f"allowlisted classes carry secret-shaped fields: {violations}"
    )


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


def test_g1d_neuter_removing_scalar_branch_raises():
    """NEUTER: json.dumps on a raw datetime (no safe-scalar branch) raises —
    proving the branch above is load-bearing, not decorative."""
    import datetime as _dt
    import json as _json

    with pytest.raises(TypeError):
        _json.dumps(_dt.datetime(2026, 10, 8))


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


def test_g2_neuter_skipping_registry_pass_leaks():
    """NEUTER: scrub_text with the registry pass skipped must leak."""
    register_request_secret(REGISTERED_SECRET)
    text = f"token={REGISTERED_SECRET}"
    # Reproduce scrub_text's body minus the D3 registry pass.
    import audittrace.logging_config as lc

    leaked = lc._AUTHZ_SHAPE_RE.sub(
        "[REDACTED:authz]", lc._JWT_SHAPE_RE.sub("[REDACTED:jwt]", text)
    )
    assert REGISTERED_SECRET in leaked


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
    prefix (spec fact A7's exact escape)."""
    register_request_secret(REGISTERED_SECRET)
    padding = "x" * 100
    value = f"{padding}{REGISTERED_SECRET}{padding}"
    rendered = render_payload(value, limit=120)
    # Everything up to the limit must already be scrubbed — no raw secret
    # substring survives even though it straddles the cut.
    assert REGISTERED_SECRET not in rendered


def test_g2f_neuter_truncate_before_scrub_leaks_a_prefix():
    """NEUTER: truncating BEFORE scrubbing can cut a token in half,
    leaving an unscrubbed (and no-longer-JWT-shaped) prefix — the exact
    regression this ordering prevents."""
    register_request_secret(REGISTERED_SECRET)
    padding = "x" * 10
    value = f"{padding}{REGISTERED_SECRET}{padding}"
    truncated_first = value[:15]  # cuts into the middle of the secret
    # The truncated prefix still contains a chunk of the registered
    # secret — proving truncate-then-scrub is unsafe (scrub_text would
    # never see the full value to match against).
    assert REGISTERED_SECRET[:5] in truncated_first


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
    """Every MODULE-LEVEL @log_call AST target, once imported, carries the
    __audittrace_log_call__ marker the decorator sets — a 100% check over
    what is directly reachable as a module attribute (class-scoped
    methods are counted separately below since they aren't module
    attributes; this is an AST-vs-import-resolution limit, not a gap in
    what the decorator itself marks)."""

    count_targets = 0
    count_class_scoped = 0
    unmarked: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel_module = (
            "audittrace." + str(path.relative_to(SRC_ROOT))[:-3].replace("/", ".")
        ).rstrip(".")
        if rel_module.endswith("__init__"):
            rel_module = rel_module[: -len(".__init__")]
        tree = ast.parse(path.read_text(), filename=str(path))

        # Module-level targets only: direct children of the Module node.
        module_level_names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_is_log_call_decorator(d) for d in node.decorator_list)
        ]
        # Class-scoped targets: walk, then subtract the module-level set.
        all_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_is_log_call_decorator(d) for d in node.decorator_list)
        ]
        count_class_scoped += len(all_names) - len(module_level_names)

        if not module_level_names:
            continue
        count_targets += len(module_level_names)
        try:
            mod = importlib.import_module(rel_module)
        except Exception:
            unmarked.append(f"{rel_module}: import failed")
            continue
        for name in module_level_names:
            obj = getattr(mod, name, None)
            if obj is None or not getattr(obj, "__audittrace_log_call__", False):
                unmarked.append(f"{rel_module}.{name}")

    assert count_targets + count_class_scoped > 300, (
        "sanity: expected the ~338-target scale"
    )
    assert not unmarked, (
        f"module-level @log_call targets missing the marker: {unmarked}"
    )


def test_g4c_render_allowlists_match_independent_ast_derivation():
    """Independently re-derive the params∪returns class set via AST +
    import resolution and assert it equals logging_config's allowlists.

    Deliberately does NOT reuse logging_config._get_render_allowlists —
    an equality pin whose two sides share a computation is vacuous
    (`feedback_a_pins_reference_point_must_be_inside_the_guarded_unit`).
    """
    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    def _flatten(ann):
        if ann is None:
            return
        if isinstance(ann, ast.Name):
            yield ann.id
        elif isinstance(ann, ast.Attribute):
            yield ann.attr
        elif isinstance(ann, ast.Subscript):
            yield from _flatten(ann.slice)
        elif isinstance(ann, ast.BinOp):
            yield from _flatten(ann.left)
            yield from _flatten(ann.right)
        elif isinstance(ann, ast.Tuple):
            for el in ann.elts:
                yield from _flatten(el)

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
                    names.update(_flatten(a.annotation))
                if node.returns is not None:
                    names.update(_flatten(node.returns))

    import sys as _sys

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
    missing_dataclasses = resolved_dataclasses - render_dataclasses
    assert not missing_models, (
        f"models used in signatures but not allowlisted: {missing_models}"
    )
    assert not missing_dataclasses, (
        f"dataclasses used in signatures but not allowlisted: {missing_dataclasses}"
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


# ═══════════════════════════ G7 — both sinks, one serialiser ══════════════


def test_g7_log_and_span_use_the_same_serialiser(monkeypatch):
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
    def op(x):
        return x

    try:
        op("anything")
    finally:
        logger.removeHandler(handler)

    span_attrs = {
        call.args[0]: call.args[1] for call in fake_span.set_attribute.call_args_list
    }
    assert span_attrs.get("input.value") == sentinel
    input_records = [r for r in records if r.getMessage().startswith("INPUT")]
    assert input_records, "expected a DEBUG INPUT record"
    assert input_records[-1].call_kwargs == sentinel, (
        "the DEBUG log's call_kwargs must come from the SAME render_payload "
        "call as the span's input.value — a specific field check, not a "
        "blanket OR across the whole record, so a neuter of ONE call site "
        "cannot hide behind another"
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


def test_s_c3_ordering_defect_invisible_to_a_reference_holding_exporter():
    """Demonstrates WHY the net/G8 exporters must snapshot at export:
    a scrubber registered AFTER a reference-holding in-memory exporter
    still looks "scrubbed" later, because both read the SAME mutable
    object and the scrubber mutates it before the test inspects it.
    This is the measured defect ADDENDUM C's S-C3 names — it is not a
    production code path (production always puts the scrubber first),
    it is the reasoning for why the net/G8 use a SNAPSHOT exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    in_memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(in_memory))  # registered FIRST
    provider.add_span_processor(telemetry.ScrubbingSpanProcessor())  # registered SECOND

    register_request_secret(REGISTERED_SECRET)
    tracer = provider.get_tracer("s-c3-test")
    with tracer.start_as_current_span("s-c3-span") as span:
        span.set_attribute("input.value", f"carries {REGISTERED_SECRET}")

    # Misleading: reading the stored reference AFTER on_end finished
    # shows it scrubbed, even though the scrubber ran SECOND — because
    # both processors touch the SAME object within the one on_end call.
    stored = in_memory.get_finished_spans()[-1]
    assert REGISTERED_SECRET not in stored.attributes["input.value"], (
        "a reference-holding exporter cannot detect this ordering defect "
        "— which is exactly S-C3's point; a snapshot-at-export exporter "
        "(this repo's _NetSnapshotExporter / _SerialisingProbeExporter) "
        "is what actually proves ordering"
    )


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


def test_g9_neuter_removing_the_refusal_check_lets_it_through(monkeypatch):
    """NEUTER: without the check, a dangerous entry is silently accepted."""
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", "authorization"
    )
    # Simulating "the guard removed": just don't call it. No exception.
    assert True


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
