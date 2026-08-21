"""Unit tests for the ADR-059 memory-wiring helper (Component / WS4).

The network-egress seam ``memory._http_request`` is monkeypatched in every test:
no sockets, no front door, no token files (a ``tmp_path`` token file is used).

Two asymmetric contracts are exercised in BOTH directions:

* ``recall_deploy_lessons`` is BEST-EFFORT — every failure mode (no token, HTTP
  error, transport raise, empty body, bad JSON, bad URL) returns ``[]`` and
  never raises; a healthy answer returns the lessons.
* ``log_deploy_record`` is RELIABLE — a healthy path returns the keys; a missing
  token, a non-200 upload, and a non-200 index each raise ``DeployLogError``.

URL routing in these tests uses ``urlparse(...).path`` + ``parse_qs`` exact
matching, never a substring ``in url`` check (the CodeQL
``py/incomplete-url-substring`` class the WS2 tests tripped).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Request

from audittrace.services.recall_telemetry import classify_recall_source_from_request
from scripts.deploy import memory
from scripts.deploy.memory import (
    BUILD_OUTCOMES,
    RECALL_QUARANTINED_OUTCOMES,
    DeployLogError,
    _ensure_md,
    _fleet_headers,
    _is_existing_file,
    _is_recall_quarantined,
    _multipart_body,
    _normalize_front_door,
    _record_bytes_and_name,
    _resolve_token,
    _tag_outcome,
    log_deploy_record,
    recall_deploy_lessons,
    ssl_context,
)

FRONT = "https://audittrace.test"
TOKEN = "test.jwt.token"


# ── fake egress seam ──────────────────────────────────────────────────────────


class _FakeHttp:
    """Records requests and returns canned ``(status, headers, body)`` per path.

    Routing keys on ``urlparse(url).path`` — EXACT match, never substring.
    """

    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, method, url, headers=None, body=None, timeout=30, context=None):
        parsed = urlparse(url)
        self.calls.append(
            {
                "method": method,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": headers or {},
                "body": body,
                "context": context,
            }
        )
        status, payload = self.responses.get(parsed.path, (404, b'{"detail":"nf"}'))
        return status, {}, payload


def _install(monkeypatch, responses):
    fake = _FakeHttp(responses)
    monkeypatch.setattr(memory, "_http_request", fake)
    return fake


def _clear_env_token(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)


# ── recall: happy path ─────────────────────────────────────────────────────────


def test_recall_returns_lessons(monkeypatch):
    _clear_env_token(monkeypatch)
    items = [{"key": "decisions/surge-safe.md", "content": "maxSurge=0"}]
    fake = _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": items}).encode())}
    )
    out = recall_deploy_lessons("surge", front_door=FRONT, token=TOKEN)
    assert out == items
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/memory/semantic"
    assert call["query"]["collection"] == ["decisions"]
    # token used only as a Bearer header, never printed
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_recall_works_without_a_query_argument(monkeypatch):
    """SPEC-wu1b (2026-08-07) — live symptom: a fleet caller invoked
    ``memory.recall_deploy_lessons(front_door=..., insecure=...)`` (no
    positional/keyword ``query``) and got ``TypeError: missing 1 required
    positional argument: 'query'`` because the parameter had no default.
    ``query`` is logging-only (never used to filter/rank — see the
    docstring), so omitting it must degrade gracefully, not crash the
    ADR-059 recall-before step. Neutering the default (``query: str`` with
    no default again) turns this RED with the exact live ``TypeError``."""
    _clear_env_token(monkeypatch)
    items = [{"key": "decisions/surge-safe.md", "content": "maxSurge=0"}]
    _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": items}).encode())}
    )
    out = recall_deploy_lessons(front_door=FRONT, token=TOKEN)
    assert out == items


def test_recall_filters_non_dict_items(monkeypatch):
    _clear_env_token(monkeypatch)
    body = json.dumps({"items": [{"key": "a"}, "junk", 3]}).encode()
    _install(monkeypatch, {"/memory/semantic": (200, body)})
    out = recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    assert out == [{"key": "a"}]


# ── recall: every failure returns [] and never raises ───────────────────────────


def test_recall_empty_items_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_http_error_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/semantic": (503, b"upstream")})
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_transport_failure_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)

    def _boom(*a, **k):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(memory, "_http_request", _boom)
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_bad_json_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/semantic": (200, b"not-json{")})
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_no_items_key_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"total": 0}).encode())}
    )
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_no_token_returns_empty(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    called = {"n": 0}

    def _never(*a, **k):
        called["n"] += 1
        return 200, {}, b"{}"

    monkeypatch.setattr(memory, "_http_request", _never)
    monkeypatch.setattr(memory, "DEFAULT_TOKEN_FILE", tmp_path / "missing.json")
    assert recall_deploy_lessons("q", front_door=FRONT) == []
    assert called["n"] == 0  # never even hits the network without a token


def test_recall_bad_front_door_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(
        memory,
        "_http_request",
        lambda *a, **k: called.__setitem__("n", 1) or (200, {}, b"{}"),
    )
    assert recall_deploy_lessons("q", front_door="ftp://nope", token=TOKEN) == []
    assert called["n"] == 0


def test_recall_passes_insecure_context(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/semantic": (200, json.dumps({"items": [{"k": 1}]}).encode())},
    )
    recall_deploy_lessons("q", front_door=FRONT, token=TOKEN, insecure=True)
    assert fake.calls[0]["context"] is not None  # unverified SSL context selected


# ── recall: fleet attribution headers (RECALL-METRIC-COVERAGE crux #4a) ──────
#
# The independent review of ``ef49935`` found the server-side classifier
# (``classify_recall_source_from_request``) was fully wired but INERT on real
# fleet traffic: ``recall_deploy_lessons`` never sent the ``X-Source`` /
# ``X-Agent-Role`` headers it keys on, so a genuine fleet recall was always
# classified ``"backoffice"``. ``recall_deploy_lessons`` is the SHARED recall
# path for BOTH the local OpenCode/Qwen fleet AND the Sonnet cloud fleet, so
# ``X-Agent-Role`` (always sent, runtime-agnostic) is the LOAD-BEARING
# classification signal for both, while ``X-Source`` stays runtime-honest via
# ``AUDITTRACE_AGENT_RUNTIME`` — never a hardcoded ``"opencode-"`` prefix,
# which would mislabel Sonnet-fleet recalls as OpenCode. These tests are the
# falsifiable proof the two halves now connect: (1) the outbound request
# carries the headers, and (2) a request built with those exact headers is
# classified ``"fleet"`` by the REAL server-side classifier (not a
# re-implementation of its logic).


def _request_with_headers(headers: dict[str, str]) -> Request:
    """Bare Starlette ``Request`` from a raw ASGI scope carrying ``headers``
    — same idiom as ``tests/test_recall_telemetry.py::_make_request`` — used
    to feed the REAL server-side classifier the exact headers
    ``recall_deploy_lessons`` sends, proving the two halves connect."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/memory/semantic",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _clear_agent_env(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_AGENT_ROLE", raising=False)
    monkeypatch.delenv("AUDITTRACE_AGENT_RUNTIME", raising=False)


def test_recall_always_sends_x_agent_role_header(monkeypatch):
    """``X-Agent-Role`` is the UNIVERSAL, runtime-agnostic fleet marker and
    must be present on every outbound recall GET regardless of runtime.
    Neuter: stop sending the header (or send it empty) -> RED."""
    _clear_env_token(monkeypatch)
    monkeypatch.setenv("AUDITTRACE_AGENT_ROLE", "reviewer")
    fake = _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    headers = fake.calls[0]["headers"]
    assert headers["X-Agent-Role"] == "reviewer"
    # the bearer token must still be present alongside the new headers
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_recall_sends_fleet_default_x_agent_role_when_unset(monkeypatch):
    """``recall_deploy_lessons`` is BY DEFINITION the fleet/agent recall path
    (never a human front-door read) — an unset ``AUDITTRACE_AGENT_ROLE`` must
    still default to the fleet marker, NEVER ``backoffice``/empty. Neuter:
    change the default to omit ``X-Agent-Role`` -> RED."""
    _clear_agent_env(monkeypatch)
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    headers = fake.calls[0]["headers"]
    assert headers["X-Agent-Role"] == "fleet"


@pytest.mark.parametrize(
    ("runtime", "role", "expected_x_source"),
    [
        ("opencode", "builder", "opencode-builder"),
        ("sonnet", "reviewer", "sonnet-reviewer"),
        ("", "builder", "builder"),  # no runtime known -> bare role, no prefix
        ("", "", "fleet"),  # nothing configured -> plain fleet marker
    ],
)
def test_recall_x_source_is_runtime_honest_not_hardcoded_opencode(
    monkeypatch, runtime, role, expected_x_source
):
    """``X-Source`` must reflect the ACTUAL calling runtime (OpenCode vs
    Sonnet), never a hardcoded ``"opencode-"`` prefix that would mislabel
    Sonnet-fleet recalls as OpenCode. Neuter: hardcode the ``opencode-``
    prefix back in -> the ``sonnet``/``""`` cases go RED."""
    _clear_env_token(monkeypatch)
    _clear_agent_env(monkeypatch)
    if runtime:
        monkeypatch.setenv("AUDITTRACE_AGENT_RUNTIME", runtime)
    if role:
        monkeypatch.setenv("AUDITTRACE_AGENT_ROLE", role)
    fake = _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    headers = fake.calls[0]["headers"]
    assert headers["X-Source"] == expected_x_source


def test_fleet_headers_role_and_runtime_from_env_directly(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_AGENT_ROLE", "builder")
    monkeypatch.setenv("AUDITTRACE_AGENT_RUNTIME", "sonnet")
    assert _fleet_headers() == {
        "X-Source": "sonnet-builder",
        "X-Agent-Role": "builder",
    }


def test_fleet_headers_defaults_when_env_blank(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_AGENT_ROLE", "   ")
    monkeypatch.delenv("AUDITTRACE_AGENT_RUNTIME", raising=False)
    assert _fleet_headers() == {
        "X-Source": "fleet",
        "X-Agent-Role": "fleet",
    }


@pytest.mark.parametrize(
    ("runtime", "role"),
    [
        ("opencode", "builder"),
        ("sonnet", "reviewer"),
        ("sonnet", "deployer"),
        ("", "verifier"),
        ("", ""),  # nothing configured -> still classifies fleet
    ],
)
def test_recall_headers_classify_as_fleet_by_the_real_server_classifier(
    monkeypatch, runtime, role
):
    """End-to-end connection proof: feed the REAL
    ``classify_recall_source_from_request`` a request carrying the EXACT
    headers ``recall_deploy_lessons`` sends — for BOTH the OpenCode runtime
    and the Sonnet runtime, and the fully-unconfigured default. This must
    classify ``"fleet"`` in every case — never ``"backoffice"``. Neuter
    either the header-sending side (drop ``_fleet_headers()`` from the
    outbound call, or drop ``X-Agent-Role`` from it) or the classifier's own
    logic -> RED."""
    _clear_env_token(monkeypatch)
    _clear_agent_env(monkeypatch)
    if runtime:
        monkeypatch.setenv("AUDITTRACE_AGENT_RUNTIME", runtime)
    if role:
        monkeypatch.setenv("AUDITTRACE_AGENT_ROLE", role)
    fake = _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    sent_headers = fake.calls[0]["headers"]
    request = _request_with_headers(sent_headers)
    assert classify_recall_source_from_request(request) == "fleet"


# ── log: happy path ─────────────────────────────────────────────────────────────


def test_log_upload_and_index_success_returns_keys(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (200, json.dumps({"key": "episodic/rec.md"}).encode()),
            "/memory/index": (200, json.dumps({"indexed": 1}).encode()),
        },
    )
    out = log_deploy_record(
        "some record text", front_door=FRONT, token=TOKEN, filename="rec.md"
    )
    assert out["status"] == "logged"
    assert out["key"] == "episodic/rec.md"
    assert out["index_status"] == {"indexed": 1}
    # upload is multipart to the file field; index references the uploaded key
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    index = next(c for c in fake.calls if c["path"] == "/memory/index")
    assert upload["method"] == "POST"
    assert "multipart/form-data" in upload["headers"]["Content-Type"]
    assert b'name="file"' in upload["body"]
    assert index["query"]["file"] == ["episodic/rec.md"]
    assert index["query"]["collections"] == ["decisions"]


def test_log_reads_from_file_path(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    rec = tmp_path / "decision-2026.md"
    rec.write_text("# a real record\nbody")
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (
                200,
                json.dumps({"key": "episodic/decision-2026.md"}).encode(),
            ),
            "/memory/index": (200, b"{}"),
        },
    )
    out = log_deploy_record(rec, front_door=FRONT, token=TOKEN)
    assert out["key"] == "episodic/decision-2026.md"
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    assert b"# a real record" in upload["body"]
    assert upload["query"]["filename"] == ["decision-2026.md"]


def test_log_derives_key_when_upload_omits_it(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    out = log_deploy_record("text", front_door=FRONT, token=TOKEN, filename="x.md")
    assert out["key"] == "episodic/x.md"


def test_log_derives_key_when_upload_body_not_json(monkeypatch):
    """A 200 upload with a non-JSON body → default ``<layer>/<leaf>`` key used."""
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"OK plain text"), "/memory/index": (200, b"{}")},
    )
    out = log_deploy_record("text", front_door=FRONT, token=TOKEN, filename="q.md")
    assert out["key"] == "episodic/q.md"


def test_log_custom_layer_and_collections(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    log_deploy_record(
        "t",
        front_door=FRONT,
        token=TOKEN,
        layer="procedural",
        collections=("decisions", "skills"),
        filename="y.md",
    )
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    index = next(c for c in fake.calls if c["path"] == "/memory/index")
    assert upload["query"]["layer"] == ["procedural"]
    assert index["query"]["collections"] == ["decisions,skills"]


def test_log_private_tier_key_round_trips(monkeypatch):
    """Gate 6 (#426) — since WU-B5, ``POST /memory/upload`` returns a
    PRIVATE-tier key shaped ``{jwt.sub}/{layer}/{filename}``, not the
    legacy ``{layer}/{filename}``. ``log_deploy_record`` already round-
    trips whatever ``key`` the upload response carries straight into
    ``?file=`` — this locks that contract against the ADR-062 Phase B key
    shape so the fleet self-log -> decisions fold path is provably intact.
    Neuter: if the helper ever derives ``file=`` from ``layer``/``filename``
    instead of the upload response's ``key``, this test goes RED (the
    private sub-prefixed key would be lost).
    """
    _clear_env_token(monkeypatch)
    private_key = "b1946ac9-2f1e-4c3a-9e1b-000000000001/episodic/rec.md"
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (200, json.dumps({"key": private_key}).encode()),
            "/memory/index": (200, json.dumps({"status": "indexed"}).encode()),
        },
    )
    out = log_deploy_record(
        "some record text", front_door=FRONT, token=TOKEN, filename="rec.md"
    )
    assert out["status"] == "logged"
    assert out["key"] == private_key
    index = next(c for c in fake.calls if c["path"] == "/memory/index")
    # The exact private key (WITH the sub prefix) must be what gets
    # indexed — never a truncated/derived {layer}/{filename} shape.
    assert index["query"]["file"] == [private_key]


# ── log: failures are LOUD (raise DeployLogError) ───────────────────────────────


def test_log_no_token_raises(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    monkeypatch.setattr(memory, "DEFAULT_TOKEN_FILE", tmp_path / "missing.json")
    with pytest.raises(DeployLogError) as exc:
        log_deploy_record("t", front_door=FRONT, filename="z.md")
    assert exc.value.step == "auth"


def test_log_upload_failure_raises(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (403, json.dumps({"detail": "forbidden"}).encode())},
    )
    with pytest.raises(DeployLogError) as exc:
        log_deploy_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "upload"
    assert exc.value.status == 403
    assert "forbidden" in str(exc.value)


def test_log_index_failure_raises(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (500, b"boom")},
    )
    with pytest.raises(DeployLogError) as exc:
        log_deploy_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "index"
    assert exc.value.status == 500


def test_log_transport_failure_on_upload_raises(monkeypatch):
    """A transport failure (seam returns status 0) is a non-200 upload → raises."""
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/upload": (0, b"")})
    with pytest.raises(DeployLogError) as exc:
        log_deploy_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.status == 0


# ── helpers ─────────────────────────────────────────────────────────────────────


def test_normalize_front_door_valid():
    assert _normalize_front_door("https://x.test/") == "https://x.test"
    assert _normalize_front_door("http://y.test") == "http://y.test"


@pytest.mark.parametrize("bad", ["ftp://x", "not-a-url", "https:///nohost", ""])
def test_normalize_front_door_rejects(bad):
    with pytest.raises(ValueError):
        _normalize_front_door(bad)


def test_resolve_token_priority(monkeypatch, tmp_path):
    # explicit arg wins
    assert _resolve_token("arg", token_file=tmp_path / "n.json") == "arg"
    # env second
    monkeypatch.setenv("AUDITTRACE_TOKEN", "envtok")
    assert _resolve_token(None, token_file=tmp_path / "n.json") == "envtok"
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)
    # file third
    tf = tmp_path / "tokens.json"
    tf.write_text(json.dumps({"access_token": "filetok"}))
    assert _resolve_token(None, token_file=tf) == "filetok"
    # nothing available
    assert _resolve_token(None, token_file=tmp_path / "missing.json") is None
    # file present but no/blank token
    tf.write_text(json.dumps({"access_token": ""}))
    assert _resolve_token(None, token_file=tf) is None


def test_resolve_token_bad_json(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)
    tf = tmp_path / "tokens.json"
    tf.write_text("{ not json")
    assert _resolve_token(None, token_file=tf) is None


def test_multipart_body_shape():
    body, ct = _multipart_body("rec.md", b"hello")
    assert ct.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="rec.md"' in body
    assert b"hello" in body
    assert body.rstrip().endswith(b"--")


def test_ensure_md():
    assert _ensure_md("a") == "a.md"
    assert _ensure_md("a.md") == "a.md"


def test_record_bytes_and_name_synth_filename():
    content, name = _record_bytes_and_name("literal text", None)
    assert content == b"literal text"
    assert name.startswith("deploy-record-") and name.endswith(".md")


def test_record_bytes_and_name_forces_md_on_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("x")
    content, name = _record_bytes_and_name(p, None)
    assert content == b"x"
    assert name == "note.txt.md"


# ── regression: literal text long enough to raise ENAMETOOLONG on stat ────────
#
# A literal record whose first line (no "/") exceeds the 255-byte path-segment
# limit makes ``Path(text).is_file()`` raise ``OSError`` (errno 36,
# ENAMETOOLONG) instead of returning False. Falsifiable: inline the raw
# ``Path(path_or_text).is_file()`` call (removing the try/except guard) and
# this test goes RED with an unhandled ``OSError``.

_LONG_FIRST_LINE_TEXT = ("word " * 60).strip() + "\nsecond line of the record"


def test_is_existing_file_true_for_real_file(tmp_path):
    p = tmp_path / "real.md"
    p.write_text("x")
    assert _is_existing_file(str(p)) is True


def test_is_existing_file_false_for_too_long_candidate():
    too_long = "definitely not a path " * 40
    assert len(too_long.split("/", 1)[0]) > 255
    assert _is_existing_file(too_long) is False


def test_record_bytes_and_name_survives_long_literal_text_no_slash():
    assert len(_LONG_FIRST_LINE_TEXT.split("/", 1)[0]) > 255
    content, name = _record_bytes_and_name(_LONG_FIRST_LINE_TEXT, "rec.md")
    assert content == _LONG_FIRST_LINE_TEXT.encode()
    assert name == "rec.md"


def test_log_deploy_record_survives_long_literal_text_no_slash(monkeypatch):
    """End-to-end via the public entry point: the crash is gone, not just at
    the helper level — upload + index both complete cleanly."""
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    out = log_deploy_record(
        _LONG_FIRST_LINE_TEXT, front_door=FRONT, token=TOKEN, filename="x.md"
    )
    assert out["status"] == "logged"
    assert out["key"] == "episodic/x.md"


def test_ssl_context():
    assert ssl_context(False) is None
    ctx = ssl_context(True)
    assert ctx is not None
    assert ctx.verify_mode.name == "CERT_NONE"


def test_no_url_substring_in_source():
    """CodeQL-safety guard: the module must not gate on a URL substring test."""
    src = Path(memory.__file__).read_text()
    assert " in url" not in src
    assert "in front_door" not in src


# ── WS5: transport-timeout hardening at the REAL egress seam ─────────────────
#
# A READ timeout raises a bare ``TimeoutError`` (== ``socket.timeout``, an
# ``OSError`` that is NOT a ``URLError``). The seam must map the WHOLE transport
# class to the status-0 sentinel so each caller honours its OWN contract:
# best-effort recall → ``[]``; reliable log → a CLEAN ``DeployLogError``. These
# drive the REAL ``_http_request`` (patching ``urllib.request.urlopen``) and are
# falsifiable: revert to ``except URLError`` and they raise a bare TimeoutError.


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),  # == socket.timeout on a read timeout
        ConnectionResetError("peer reset"),
        OSError("network unreachable"),
    ],
)
def test_http_request_maps_transport_errors_to_status_zero(monkeypatch, exc):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    status, headers, body = memory._http_request("GET", "https://audittrace.test/x")
    assert status == 0 and headers == {} and body == b""


def test_recall_read_timeout_returns_empty_via_real_seam(monkeypatch):
    """recall is BEST-EFFORT: a real read timeout must yield ``[]``, never raise."""
    _clear_env_token(monkeypatch)

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    assert recall_deploy_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_log_read_timeout_raises_clean_deploy_log_error(monkeypatch):
    """log is RELIABLE: a real read timeout on the upload becomes a CLEAN
    ``DeployLogError`` (status 0), NOT a bare ``TimeoutError`` escaping the API."""
    _clear_env_token(monkeypatch)

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    with pytest.raises(DeployLogError) as exc:
        log_deploy_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "upload"
    assert exc.value.status == 0


# ── build-outcome tagging + recall quarantine (2026-08-21 corpus-hygiene guard) ──
#
# A stalled/failed build trajectory is "gold to STUDY, poison to RECALL". The
# guard tags the record filename with `.outcome-<value>` on write and drops the
# quarantined outcomes (stalled/failed) from recall by default — while keeping
# `pass`, `reject`, and every legacy (untagged) record. Each test below neuters
# to RED if the guard is removed.


def test_tag_outcome_none_is_noop():
    """No outcome → the historical filename is untouched (backward compatible)."""
    assert _tag_outcome("rec.md", None) == "rec.md"


def test_tag_outcome_inserts_infix_before_md():
    assert _tag_outcome("2026-08-21-BUILD-456.md", "stalled") == (
        "2026-08-21-BUILD-456.outcome-stalled.md"
    )
    # tolerates a leaf without .md too (still lands a .md record)
    assert _tag_outcome("plain", "pass") == "plain.outcome-pass.md"


def test_tag_outcome_is_idempotent():
    once = _tag_outcome("rec.md", "failed")
    assert _tag_outcome(once, "failed") == once  # never double-tags
    assert once.count(".outcome-") == 1


def test_tag_outcome_rejects_unknown_value():
    with pytest.raises(ValueError, match="outcome must be one of"):
        _tag_outcome("rec.md", "bogus")


def test_log_tags_outcome_into_upload_and_index_filenames(monkeypatch):
    """The write half of the guard: outcome rides in the recall-visible filename
    (upload ``filename`` + index ``file``), so recall can see it WITHOUT a content
    fetch. Neuter (drop the ``leaf = _tag_outcome(...)`` line) → RED."""
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    out = log_deploy_record(
        "build stalled, zero code",
        front_door=FRONT,
        token=TOKEN,
        filename="2026-08-21-BUILD-456.md",
        outcome="stalled",
    )
    assert out["key"] == "episodic/2026-08-21-BUILD-456.outcome-stalled.md"
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    index = next(c for c in fake.calls if c["path"] == "/memory/index")
    assert upload["query"]["filename"] == ["2026-08-21-BUILD-456.outcome-stalled.md"]
    assert index["query"]["file"] == [
        "episodic/2026-08-21-BUILD-456.outcome-stalled.md"
    ]


def test_log_without_outcome_is_untagged(monkeypatch):
    """Every existing caller (no ``outcome=``) keeps its exact filename."""
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    log_deploy_record("text", front_door=FRONT, token=TOKEN, filename="rec.md")
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    assert upload["query"]["filename"] == ["rec.md"]
    assert ".outcome-" not in upload["query"]["filename"][0]


def test_log_rejects_unknown_outcome_raises(monkeypatch):
    """A mistagged outcome is LOUD (the log path is reliable) — never silently
    written untagged."""
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    with pytest.raises(ValueError, match="outcome must be one of"):
        log_deploy_record(
            "text", front_door=FRONT, token=TOKEN, filename="x.md", outcome="green"
        )


def _semantic(items):
    return {"/memory/semantic": (200, json.dumps({"items": items}).encode())}


def test_recall_quarantines_stalled_and_failed_by_default(monkeypatch):
    """The read half: recall drops stalled/failed build-records so an agent never
    recalls a drift trajectory as precedent. Neuter (remove the quarantine block)
    → this returns all four → RED."""
    _clear_env_token(monkeypatch)
    items = [
        {"key": "decisions/a", "title": "2026-08-21-BUILD-456.outcome-pass.md"},
        {"key": "decisions/b", "title": "2026-08-21-BUILD-456.outcome-stalled.md"},
        {"key": "decisions/c", "title": "2026-08-20-BUILD-402.outcome-failed.md"},
        {"key": "decisions/d", "title": "2026-08-19-BUILD-441.outcome-reject.md"},
    ]
    _install(monkeypatch, _semantic(items))
    out = recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    titles = {i["title"] for i in out}
    assert "2026-08-21-BUILD-456.outcome-stalled.md" not in titles
    assert "2026-08-20-BUILD-402.outcome-failed.md" not in titles
    # pass + reject survive
    assert "2026-08-21-BUILD-456.outcome-pass.md" in titles
    assert "2026-08-19-BUILD-441.outcome-reject.md" in titles


def test_recall_keeps_legacy_untagged_records(monkeypatch):
    """Non-regressive: records with no outcome infix (every historical spec /
    park / deploy / verify record) are NEVER quarantined."""
    _clear_env_token(monkeypatch)
    items = [
        {"key": "decisions/1", "title": "2026-08-19-SPEC-456-RATIFIED-A1.txt.md"},
        {"key": "decisions/2", "title": "deploy-v1.24.4-20260818.json.md"},
    ]
    _install(monkeypatch, _semantic(items))
    out = recall_deploy_lessons("q", front_door=FRONT, token=TOKEN)
    assert len(out) == 2


def test_recall_include_quarantined_returns_all(monkeypatch):
    """The escape hatch for a curator/study query returns the drift too."""
    _clear_env_token(monkeypatch)
    items = [
        {"key": "decisions/a", "title": "b.outcome-pass.md"},
        {"key": "decisions/b", "title": "b.outcome-stalled.md"},
    ]
    _install(monkeypatch, _semantic(items))
    out = recall_deploy_lessons(
        "q", front_door=FRONT, token=TOKEN, include_quarantined=True
    )
    assert len(out) == 2


def test_is_recall_quarantined_keys_on_identity_fields_only():
    assert _is_recall_quarantined({"title": "x.outcome-stalled.md"}) is True
    assert _is_recall_quarantined({"key": "decisions/x.outcome-failed.md"}) is True
    assert _is_recall_quarantined({"source": "x.outcome-failed.md"}) is True
    assert _is_recall_quarantined({"title": "x.outcome-pass.md"}) is False
    assert _is_recall_quarantined({"title": "legacy-spec.md"}) is False


def test_quarantine_set_is_subset_of_known_outcomes():
    """Guard against a typo drifting the two constants apart."""
    assert RECALL_QUARANTINED_OUTCOMES <= set(BUILD_OUTCOMES)
    assert RECALL_QUARANTINED_OUTCOMES == {"stalled", "failed"}
