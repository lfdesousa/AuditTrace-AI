"""Unit tests for ``memory.log_build_record`` — the SDLC-ADR-005 WU-1 wiring.

Confirms the scoped, additive design: ``log_build_record`` validates the
Layer-0 schema BEFORE any network call and otherwise behaves exactly like
``log_deploy_record``; the existing, shared ``log_deploy_record`` path used
by the deploy/release/curator agents is completely unaffected (regression
guard at the bottom of this file).
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from scripts.deploy import memory
from scripts.deploy.build_record import BuildRecordValidationError
from scripts.deploy.memory import log_build_record, log_deploy_record

FRONT = "https://audittrace.test"
TOKEN = "test.jwt.token"

COMPLETE_FIELDS = {
    "spec_ref": "sdlc/specs/2026-08-28-SPEC-adr059-mechanical-enforcement.md",
    "spec_hash": "sha256:abc123",
    "recall_evidence": "query='adr059 layer0' -> 25 items",
    "log_key": "0b0cdd4d-04c3-428f-ab9d-37b47429c381/episodic/build-record.md",
    "index_status": "indexed: 3 chunks",
    "branch": "feat/sdlc-adr059-build-record-schema",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "gates": "make test: 640 passed, 0 skipped, 91.2% coverage",
}


def _complete_record_text() -> str:
    lines = [f"{k}: {v!r}" for k, v in COMPLETE_FIELDS.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n# Build record\n\nbody.\n"


class _FakeHttp:
    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, method, url, headers=None, body=None, timeout=30, context=None):
        parsed = urlparse(url)
        self.calls.append({"method": method, "path": parsed.path})
        status, payload = self.responses.get(parsed.path, (404, b'{"detail":"nf"}'))
        return status, {}, payload


def _install(monkeypatch, responses):
    fake = _FakeHttp(responses)
    monkeypatch.setattr(memory, "_http_request", fake)
    return fake


def _clear_env_token(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)


# ── log_build_record: valid record delegates to log_deploy_record ───────────


def test_log_build_record_valid_delegates_and_logs(monkeypatch):
    """D1 two-phase upload: phase 1 persists the record as given (learning the
    real server key + index result), phase 2 re-persists it with those real
    values patched in — so TWO upload+index round trips happen, not one."""
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (
                200,
                json.dumps({"key": "episodic/build-record.md"}).encode(),
            ),
            "/memory/index": (200, json.dumps({"indexed": 3}).encode()),
        },
    )
    out = log_build_record(
        _complete_record_text(),
        front_door=FRONT,
        token=TOKEN,
        filename="build-record.md",
    )
    assert out["status"] == "logged"
    assert out["key"] == "episodic/build-record.md"
    assert out["index_status"] == {"indexed": 3}
    paths = [c["path"] for c in fake.calls]
    assert paths == [
        "/memory/upload",
        "/memory/index",
        "/memory/upload",
        "/memory/index",
    ]


def test_log_build_record_reads_from_file(monkeypatch, tmp_path):
    _clear_env_token(monkeypatch)
    record = tmp_path / "build-record.md"
    record.write_text(_complete_record_text())
    _install(
        monkeypatch,
        {
            "/memory/upload": (
                200,
                json.dumps({"key": "episodic/build-record.md"}).encode(),
            ),
            "/memory/index": (200, b"{}"),
        },
    )
    out = log_build_record(record, front_door=FRONT, token=TOKEN)
    assert out["key"] == "episodic/build-record.md"


# ── D1 acceptance: the SERVER-PERSISTED copy carries REAL values, never a
#    placeholder — auto-populate, no caller action required ─────────────────


def test_log_build_record_autopopulates_log_key_and_index_status(monkeypatch):
    """Neuter proof: comment out the phase-2 patch-and-repersist step in
    ``log_build_record`` and this goes RED — the FINAL uploaded body (what the
    server actually stores) still carries the literal ``PENDING`` placeholder
    text instead of the real key/index-status. With the fix in place, the
    last-uploaded body carries the real values and no placeholder text at
    all — the closest a monkeypatched-HTTP unit test can get to "an
    independently re-fetched server copy shows real values" (the live dogfood
    proof against the real front door is the full end-to-end confirmation)."""
    _clear_env_token(monkeypatch)
    fields = {
        **COMPLETE_FIELDS,
        "log_key": "PENDING — filled in by log_build_record...",
        "index_status": "PENDING — filled in by log_build_record...",
    }
    lines = [f"{k}: {v!r}" for k, v in fields.items()]
    text = "---\n" + "\n".join(lines) + "\n---\n\n# Build record\n\nbody.\n"

    uploads: list[bytes] = []

    def fake_http(method, url, headers=None, body=None, timeout=30, context=None):
        parsed = urlparse(url)
        if parsed.path == "/memory/upload":
            uploads.append(body or b"")
            return (
                200,
                {},
                json.dumps({"key": "0b0cdd4d-abc/episodic/build-record.md"}).encode(),
            )
        if parsed.path == "/memory/index":
            return (
                200,
                {},
                json.dumps({"status": "indexed", "total_chunks": 3}).encode(),
            )
        return 404, {}, b'{"detail":"nf"}'

    monkeypatch.setattr(memory, "_http_request", fake_http)

    out = log_build_record(
        text, front_door=FRONT, token=TOKEN, filename="build-record.md"
    )

    assert out["status"] == "logged"
    assert out["key"] == "0b0cdd4d-abc/episodic/build-record.md"
    assert len(uploads) == 2  # phase 1 (still placeholder) + phase 2 (real)
    final_body = uploads[-1].decode()
    assert "pending" not in final_body.lower()
    assert "0b0cdd4d-abc/episodic/build-record.md" in final_body
    assert "total_chunks" in final_body


# ── log_build_record: an invalid record is refused BEFORE any network call ──


def test_log_build_record_missing_field_raises_before_any_http_call(monkeypatch):
    """Neuter proof: with the Layer-0 pre-check removed, an incomplete
    record would still reach ``_http_request`` and (in this test) succeed —
    this test would go RED (no ``BuildRecordValidationError`` raised, and
    ``fake.calls`` non-empty). With the pre-check in place it is GREEN and
    zero network calls happen."""
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    incomplete = {k: v for k, v in COMPLETE_FIELDS.items() if k != "spec_ref"}
    lines = [f"{k}: {v!r}" for k, v in incomplete.items()]
    text = "---\n" + "\n".join(lines) + "\n---\n\nbody.\n"
    with pytest.raises(BuildRecordValidationError) as exc:
        log_build_record(text, front_door=FRONT, token=TOKEN, filename="bad.md")
    assert exc.value.missing_fields == ["spec_ref"]
    assert fake.calls == []


def test_log_build_record_no_front_matter_raises_before_any_http_call(monkeypatch):
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {"/memory/upload": (200, b"{}"), "/memory/index": (200, b"{}")},
    )
    with pytest.raises(BuildRecordValidationError):
        log_build_record(
            "just prose, no front-matter",
            front_door=FRONT,
            token=TOKEN,
            filename="bad.md",
        )
    assert fake.calls == []


def test_log_build_record_propagates_upload_failure_as_deploy_log_error(monkeypatch):
    """Once a record passes Layer-0 validation, the rest of the contract is
    identical to ``log_deploy_record`` — including its failure shape."""
    _clear_env_token(monkeypatch)
    _install(
        monkeypatch,
        {"/memory/upload": (500, b'{"detail":"boom"}')},
    )
    with pytest.raises(memory.DeployLogError) as exc:
        log_build_record(
            _complete_record_text(),
            front_door=FRONT,
            token=TOKEN,
            filename="build-record.md",
        )
    assert exc.value.step == "upload"


# ── regression: log_deploy_record (the shared, existing path) is unaffected ─


def test_log_deploy_record_still_accepts_schema_free_records(monkeypatch):
    """A deploy/release/curator record — no Layer-0 front-matter at all —
    must keep working through the ORIGINAL, unvalidated ``log_deploy_record``
    entry point exactly as before WU-1. This is the regression guard named in
    the WU-1 spec: existing callers of ``log_deploy_record`` are UNCHANGED."""
    _clear_env_token(monkeypatch)
    fake = _install(
        monkeypatch,
        {
            "/memory/upload": (
                200,
                json.dumps({"key": "episodic/deploy-record.md"}).encode(),
            ),
            "/memory/index": (200, json.dumps({"indexed": 1}).encode()),
        },
    )
    out = log_deploy_record(
        "phase P0-P5 deploy report, no front-matter at all",
        front_door=FRONT,
        token=TOKEN,
        filename="deploy-record.md",
    )
    assert out["status"] == "logged"
    assert out["key"] == "episodic/deploy-record.md"
    paths = [c["path"] for c in fake.calls]
    assert paths == ["/memory/upload", "/memory/index"]
