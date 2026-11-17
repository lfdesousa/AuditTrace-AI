"""AT-NONSTREAM-UPSTREAM-STATUS -- the shipped Bruno requests carry the new contract.

``chat/14`` sends a non-stream turn that the model server rejects with an unmatched
HTTP 400 (an invalid ``response_format.type``); ``chat/15`` reads that turn's audit row.
``chat/18`` sends a turn the model server rejects with a server error (an audio part);
``chat/19`` shows that the one request produced exactly one audit row. They are the
live-evidence requests of this change, so their assertions are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BRUNO = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
CHAT = BRUNO / "chat"
CHAT14 = CHAT / "14-chat-unmatched-upstream-400-non-stream.bru"
CHAT15 = CHAT / "15-audit-unmatched-upstream-400-row.bru"
CHAT18 = CHAT / "18-chat-audio-part-non-stream.bru"
CHAT19 = CHAT / "19-audit-rows-of-the-audio-turn.bru"
ENVIRONMENTS = ["cluster.bru", "cluster-session-e2e.bru", "local.bru"]


def test_chat14_expects_the_upstream_status() -> None:
    text = CHAT14.read_text()
    assert "expect(res.getStatus()).to.equal(400);" in text
    assert "expect(res.getStatus()).to.equal(200);" not in text


def test_chat14_names_no_http_200_anywhere() -> None:
    """Test title and docs block included: a stale title must not survive the edit."""
    text = CHAT14.read_text()
    assert "HTTP 200" not in text
    assert 'test("HTTP 400 on the non-streaming call' in text


def test_chat14_still_checks_the_relayed_error_object() -> None:
    text = CHAT14.read_text()
    assert 'expect(b.error.type).to.equal("invalid_request_error");' in text
    assert 'expect(b.error.message).to.contain("response_format");' in text
    assert 'expect(b).to.not.have.property("choices");' in text


def test_chat15_reads_one_failed_row_with_the_status_shaped_detail() -> None:
    text = CHAT15.read_text()
    assert 'expect(r.status).to.equal("failed");' in text
    assert 'expect(r.failure_class).to.equal("upstream_error");' in text
    assert 'expect(r.error_detail.startsWith("status=400: ")).to.equal(true);' in text
    assert "lengthOf(1)" in text


def test_chat18_sends_one_non_stream_audio_part_with_a_stashed_session() -> None:
    text = CHAT18.read_text()
    assert "seq: 18" in text
    assert 'type: "input_audio"' in text
    assert 'data: bru.getEnvVar("audioPartData")' in text
    assert 'format: bru.getEnvVar("audioPartFormat")' in text
    assert "stream: false" in text
    assert 'bru.setVar("audioTurnSessionId", sid);' in text
    assert 'bru.setVar("audioTurnTraceId"' in text


def test_chat18_asserts_an_error_reply_without_predicting_its_status() -> None:
    text = CHAT18.read_text()
    assert "expect(res.getStatus()).to.not.equal(200);" in text
    assert "to.equal(500)" not in text and "to.equal(502)" not in text


def test_chat19_asserts_exactly_one_failed_row_and_records_the_status() -> None:
    text = CHAT19.read_text()
    assert "seq: 19" in text
    assert "session_id={{audioTurnSessionId}}" in text
    assert "lengthOf(1)" in text
    assert 'expect(r.status).to.equal("failed");' in text
    assert 'expect(r.failure_class).to.equal("upstream_error");' in text
    assert 'bru.setVar("audioTurnUpstreamStatus", m[1]);' in text
    assert "status=500" not in text and "status=502" not in text
    assert 'to.equal(bru.getVar("audioTurnTraceId"))' in text


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_every_environment_has_the_audio_turn_defaults(name: str) -> None:
    lines = (BRUNO / "environments" / name).read_text().splitlines()
    assert "  audioPartFormat: wav" in lines
    assert "  audioTurnSessionId:" in lines
    data = [ln for ln in lines if ln.startswith("  audioPartData: ")]
    assert len(data) == 1
    assert data[0].removeprefix("  audioPartData: ").startswith("UklGR")  # "RIFF"
