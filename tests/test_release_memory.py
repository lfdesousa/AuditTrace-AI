"""Unit tests for the ADR-059 memory-wiring helper for the Release Agent (#424).

The network-egress seam ``memory._http_request`` is monkeypatched in every test:
no sockets, no front door, no token files (a ``tmp_path`` token file is used).

Two asymmetric contracts are exercised in BOTH directions:

* ``recall_release_lessons`` is BEST-EFFORT — every failure mode (no token, HTTP
  error, transport raise, empty body, bad JSON, bad URL) returns ``[]`` and
  never raises; a healthy answer returns the lessons.
* ``log_release_record`` is RELIABLE — a healthy path returns the keys; a missing
  token, a non-200 upload, and a non-200 index each raise ``ReleaseLogError``.

URL routing in these tests uses ``urlparse(...).path`` + ``parse_qs`` exact
matching, never a substring ``in url`` check (the CodeQL
``py/incomplete-url-substring`` class).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.release import memory
from scripts.release.memory import (
    ReleaseLogError,
    _ensure_md,
    _multipart_body,
    _normalize_front_door,
    _record_bytes_and_name,
    _resolve_token,
    log_release_record,
    recall_release_lessons,
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
    out = recall_release_lessons("release", front_door=FRONT, token=TOKEN)
    assert out == items
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/memory/semantic"
    assert call["query"]["collection"] == ["decisions"]
    # token used only as a Bearer header, never printed
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_recall_filters_non_dict_items(monkeypatch):
    _clear_env_token(monkeypatch)
    body = json.dumps({"items": [{"key": "a"}, "junk", 3]}).encode()
    _install(monkeypatch, {"/memory/semantic": (200, body)})
    out = recall_release_lessons("q", front_door=FRONT, token=TOKEN)
    assert out == [{"key": "a"}]


def test_recall_default_limit_is_five():
    assert memory.DEFAULT_RECALL_LIMIT == 5


# ── recall: every failure returns [] and never raises ───────────────────────────


def test_recall_empty_items_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"items": []}).encode())}
    )
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_http_error_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/semantic": (503, b"upstream")})
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_transport_failure_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)

    def _boom(*a, **k):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(memory, "_http_request", _boom)
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_bad_json_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/semantic": (200, b"not-json{")})
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_no_items_key_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch, {"/memory/semantic": (200, json.dumps({"total": 0}).encode())}
    )
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_recall_no_token_returns_empty(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    called = {"n": 0}

    def _never(*a, **k):
        called["n"] += 1
        return 200, {}, b"{}"

    monkeypatch.setattr(memory, "_http_request", _never)
    out = recall_release_lessons(
        "q", front_door=FRONT, token_file=tmp_path / "missing.json"
    )
    assert out == []
    assert called["n"] == 0  # never even hits the network without a token


def test_recall_bad_front_door_returns_empty(monkeypatch):
    _clear_env_token(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(
        memory,
        "_http_request",
        lambda *a, **k: called.__setitem__("n", 1) or (200, {}, b"{}"),
    )
    assert recall_release_lessons("q", front_door="ftp://nope", token=TOKEN) == []
    assert called["n"] == 0


def test_recall_passes_insecure_context(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/semantic": (200, json.dumps({"items": [{"k": 1}]}).encode())},
    )
    recall_release_lessons("q", front_door=FRONT, token=TOKEN, insecure=True)
    assert fake.calls[0]["context"] is not None  # unverified SSL context selected


def test_recall_secure_context_is_none(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/semantic": (200, json.dumps({"items": [{"k": 1}]}).encode())},
    )
    recall_release_lessons("q", front_door=FRONT, token=TOKEN, insecure=False)
    assert fake.calls[0]["context"] is None


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
    out = log_release_record(
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
    rec = tmp_path / "release-2026.md"
    rec.write_text("# a real record\nbody")
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (
                200,
                json.dumps({"key": "episodic/release-2026.md"}).encode(),
            ),
            "/memory/index": (200, b"{}"),
        },
    )
    out = log_release_record(rec, front_door=FRONT, token=TOKEN)
    assert out["key"] == "episodic/release-2026.md"
    upload = next(c for c in fake.calls if c["path"] == "/memory/upload")
    assert b"# a real record" in upload["body"]
    assert upload["query"]["filename"] == ["release-2026.md"]


def test_log_derives_key_when_upload_omits_it(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    out = log_release_record("text", front_door=FRONT, token=TOKEN, filename="x.md")
    assert out["key"] == "episodic/x.md"


def test_log_derives_key_when_upload_body_not_json(monkeypatch):
    """A 200 upload with a non-JSON body → default ``<layer>/<leaf>`` key used."""
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"OK plain text"), "/memory/index": (200, b"{}")},
    )
    out = log_release_record("text", front_door=FRONT, token=TOKEN, filename="q.md")
    assert out["key"] == "episodic/q.md"


def test_log_custom_layer_and_collections(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    log_release_record(
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
    """Since WU-B5, ``POST /memory/upload`` may return a PRIVATE-tier key
    shaped ``{jwt.sub}/{layer}/{filename}``, not the legacy
    ``{layer}/{filename}``. ``log_release_record`` already round-trips
    whatever ``key`` the upload response carries straight into ``?file=`` —
    this locks that contract so the fleet self-log -> decisions fold path is
    provably intact. Neuter: if the helper ever derives ``file=`` from
    ``layer``/``filename`` instead of the upload response's ``key``, this
    test goes RED (the private sub-prefixed key would be lost).
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
    out = log_release_record(
        "some record text", front_door=FRONT, token=TOKEN, filename="rec.md"
    )
    assert out["status"] == "logged"
    assert out["key"] == private_key
    index = next(c for c in fake.calls if c["path"] == "/memory/index")
    # The exact private key (WITH the sub prefix) must be what gets
    # indexed — never a truncated/derived {layer}/{filename} shape.
    assert index["query"]["file"] == [private_key]


# ── log: failures are LOUD (raise ReleaseLogError) ───────────────────────────────


def test_log_no_token_raises(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    with pytest.raises(ReleaseLogError) as exc:
        log_release_record(
            "t",
            front_door=FRONT,
            filename="z.md",
            token_file=tmp_path / "missing.json",
        )
    assert exc.value.step == "auth"


def test_log_upload_failure_raises(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (403, json.dumps({"detail": "forbidden"}).encode())},
    )
    with pytest.raises(ReleaseLogError) as exc:
        log_release_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "upload"
    assert exc.value.status == 403
    assert "forbidden" in str(exc.value)


def test_log_index_failure_raises(monkeypatch):
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (500, b"boom")},
    )
    with pytest.raises(ReleaseLogError) as exc:
        log_release_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "index"
    assert exc.value.status == 500


def test_log_transport_failure_on_upload_raises(monkeypatch):
    """A transport failure (seam returns status 0) is a non-200 upload → raises."""
    _clear_env_token(monkeypatch)
    _install(monkeypatch, {"/memory/upload": (0, b"")})
    with pytest.raises(ReleaseLogError) as exc:
        log_release_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.status == 0


def test_log_bad_front_door_raises_value_error(monkeypatch):
    """log is RELIABLE and does NOT swallow a bad front-door URL either — it
    is validated before the auth/upload/index steps even run."""
    _clear_env_token(monkeypatch)
    with pytest.raises(ValueError):
        log_release_record("t", front_door="ftp://nope", token=TOKEN, filename="z.md")


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


def test_resolve_token_default_file_used(monkeypatch, tmp_path):
    """When ``token_file`` is omitted, ``DEFAULT_TOKEN_FILE`` is consulted at
    CALL time (not bound as a default arg), so a test can monkeypatch it."""
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)
    tf = tmp_path / "tokens.json"
    tf.write_text(json.dumps({"access_token": "defaulttok"}))
    monkeypatch.setattr(memory, "DEFAULT_TOKEN_FILE", tf)
    assert _resolve_token(None) == "defaulttok"


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
    assert name.startswith("release-record-") and name.endswith(".md")


def test_record_bytes_and_name_forces_md_on_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("x")
    content, name = _record_bytes_and_name(p, None)
    assert content == b"x"
    assert name == "note.txt.md"


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


def test_token_never_printed_in_source():
    """Grep-assertable invariant #4: no code path logs/echoes the bearer
    token itself (only the header NAME 'Authorization' or the literal
    'Bearer' prefix may appear; the resolved token value is never
    interpolated into a log/print statement)."""
    src = Path(memory.__file__).read_text()
    for bad in ("print(resolved", "logger.info(resolved", "logger.debug(resolved"):
        assert bad not in src


def test_default_front_door_is_laptop_local():
    assert memory.DEFAULT_FRONT_DOOR == "https://audittrace.local"


# ── WS5-style: transport-timeout hardening at the REAL egress seam ─────────────
#
# A READ timeout raises a bare ``TimeoutError`` (== ``socket.timeout``, an
# ``OSError`` that is NOT a ``URLError``). The seam must map the WHOLE transport
# class to the status-0 sentinel so each caller honours its OWN contract:
# best-effort recall → ``[]``; reliable log → a CLEAN ``ReleaseLogError``. These
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
    assert recall_release_lessons("q", front_door=FRONT, token=TOKEN) == []


def test_log_read_timeout_raises_clean_release_log_error(monkeypatch):
    """log is RELIABLE: a real read timeout on the upload becomes a CLEAN
    ``ReleaseLogError`` (status 0), NOT a bare ``TimeoutError`` escaping the API."""
    _clear_env_token(monkeypatch)

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    with pytest.raises(ReleaseLogError) as exc:
        log_release_record("t", front_door=FRONT, token=TOKEN, filename="z.md")
    assert exc.value.step == "upload"
    assert exc.value.status == 0
