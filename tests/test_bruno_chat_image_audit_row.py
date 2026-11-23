"""LIBRECHAT-IMAGE-SLICE AC-6 -- structural guards for Bruno ``chat/20`` (image audit row) and ``chat/21`` (refused image).

No live run here (the live run is ADR-049 evidence). The request reads the audit row of
the ``chat/16`` image turn and checks ``input_media`` against two environment defaults.
Those defaults are not typed twice: this test DECODES the default ``imagePartData`` of
every environment file and requires ``imagePartSha256`` / ``imagePartBytes`` to equal
what it finds.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
CHAT = COLLECTION / "chat"
REQUEST = CHAT / "20-audit-image-part-row.bru"
REFUSED = CHAT / "21-chat-image-part-refused.bru"
ENV_FILES = sorted((COLLECTION / "environments").glob("*.bru"))
DATA_URL_PREFIX = "data:image/png;base64,"
EXPECTED_TEST_LINES = [
    'test("returns 200 with exactly one row for the session", function() {',
    "expect(res.getStatus()).to.equal(200);",
    'expect(res.getBody().interactions).to.be.an("array").with.lengthOf(1);',
    "});",
    'test("the row records one image: type, size, sha256, position", function() {',
    "const m = res.getBody().interactions[0].input_media;",
    "expect(m.schema).to.equal(1);",
    "expect(m.count).to.equal(1);",
    'expect(m.items).to.be.an("array").with.lengthOf(1);',
    "const i = m.items[0];",
    'expect(i.kind).to.equal("image");',
    'expect(i.status).to.equal("recorded");',
    'expect(i.source).to.equal("data_uri");',
    'expect(i.media_type).to.equal("image/png");',
    'expect(i.bytes).to.equal(Number(bru.getEnvVar("imagePartBytes")));',
    'expect(i.sha256).to.equal(bru.getEnvVar("imagePartSha256"));',
    "expect(i.message_index).to.equal(0);",
    "expect(i.part_index).to.equal(1);",
    "});",
    'test("the row carries no image data", function() {',
    "const raw = JSON.stringify(res.getBody());",
    'expect(raw).to.not.contain("base64,");',
    'expect(raw).to.not.contain("iVBORw0KGgo");',
    "});",
    'test("trace_id is 32 lowercase hex (stashed for audit/02)", function() {',
    "const r = res.getBody().interactions[0];",
    "expect(r.trace_id).to.match(/^[0-9a-f]{32}$/);",
    'bru.setVar("imagePartTraceId", r.trace_id);',
    "});",
]
EXPECTED_REFUSED_TEST_LINES = [
    'test("returns 400 exactly", function() {',
    "expect(res.getStatus()).to.equal(400);",
    "});",
    'test("explicit OpenAI-shaped error naming the reason", function() {',
    "const e = res.getBody().error;",
    'expect(e.type).to.equal("invalid_request_error");',
    'expect(e.param).to.equal("messages");',
    'expect(e.code).to.equal(bru.getEnvVar("imageRefusedCode"));',
    "expect(e.status).to.equal(400);",
    'expect(e.message).to.be.a("string").and.not.empty;',
    "});",
    'test("the reply does not echo the URL", function() {',
    'expect(JSON.stringify(res.getBody())).to.not.contain(bru.getEnvVar("imageRefusedUrl"));',
    "});",
]


def _env(path: Path) -> dict[str, str]:
    pairs = re.findall(r"^\s{2}([A-Za-z0-9_]+):[ \t]*(.*)$", path.read_text(), re.M)
    return {k: v.strip() for k, v in pairs}


def _block(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)} \{{\n(.*?)^\}}", text, re.M | re.S)
    assert match is not None, f"no {name} block"
    return match.group(1)


def test_the_request_exists_with_a_seq_of_its_own() -> None:
    assert REQUEST.is_file()
    seqs = [
        (p.name, int(m.group(1)))
        for p in sorted(CHAT.glob("*.bru"))
        if (m := re.search(r"^\s*seq:\s*(\d+)", p.read_text(), re.M))
    ]
    assert [n for n, s in seqs if s == 20] == [REQUEST.name]
    assert [n for n, s in seqs if s == 21] == [REFUSED.name]


def test_it_reads_the_image_turns_row_with_the_bearer() -> None:
    text = REQUEST.read_text()
    assert (
        "url: {{baseUrl}}/interactions?session_id={{imagePartSessionId}}&limit=5"
        in _block(text, "get")
    )
    assert "token: {{accessToken}}" in _block(text, "auth:bearer")


def test_the_assertions_are_pinned() -> None:
    lines = [ln.strip() for ln in _block(REQUEST.read_text(), "tests").splitlines()]
    assert [ln for ln in lines if ln] == EXPECTED_TEST_LINES


@pytest.mark.parametrize("env_file", ENV_FILES, ids=lambda p: p.name)
def test_the_defaults_describe_the_default_image(env_file: Path) -> None:
    env = _env(env_file)
    data = env["imagePartData"]
    assert data.startswith(DATA_URL_PREFIX)
    decoded = base64.b64decode(data[len(DATA_URL_PREFIX) :], validate=True)
    assert env["imagePartSha256"] == hashlib.sha256(decoded).hexdigest()
    assert env["imagePartBytes"] == str(len(decoded))


def test_the_refusal_request_posts_a_chat_turn_and_pins_its_assertions() -> None:
    text = REFUSED.read_text()
    assert "url: {{baseUrl}}/v1/chat/completions" in _block(text, "post")
    assert "token: {{accessToken}}" in _block(text, "auth:bearer")
    lines = [ln.strip() for ln in _block(text, "tests").splitlines()]
    assert [ln for ln in lines if ln] == EXPECTED_REFUSED_TEST_LINES
    assert "expect(res.getStatus()).to.equal(400);" in EXPECTED_REFUSED_TEST_LINES


@pytest.mark.parametrize("env_file", ENV_FILES, ids=lambda p: p.name)
def test_the_refusal_defaults_are_a_link_and_its_code(env_file: Path) -> None:
    env = _env(env_file)
    assert env["imageRefusedUrl"].startswith("https://")
    assert env["imageRefusedCode"] == "image_url_not_supported"
