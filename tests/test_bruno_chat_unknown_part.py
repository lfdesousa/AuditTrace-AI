"""#474 F-d -- structural guards for Bruno ``chat/22`` (a part of an unknown type, the canary).

No live Bruno run here (the live run is ADR-049 evidence, pending for the next release
candidate). The request is PARSED, in the manner of ``tests/test_bruno_chat_image_part.py``;
every name is compared EXACTLY (a token, not a substring), so a misspelt variable or part type
fails:

* ``seq: 22``, unique in ``chat/``; posts to the chat route with the ``accessToken`` bearer;
* it reads exactly ``contentPartPrompt`` and ``unknownPartCanary`` and sets exactly the two
  variables the next request reads;
* the canary default is the same short base64 string in EVERY environment file (under the
  128-character run rule, so only redaction by part type could remove it from a trace);
* the part it sends has the type ``custom_blob``, and that type is one the service does NOT
  know (read from the redaction module), so the request keeps exercising the unknown path;
* the assertion lines are pinned exactly, and the README lists it.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from audittrace.media.redaction import _PAYLOAD_SLOTS, _TEXT_SLOTS
from tests.test_bruno_chat_image_part import (
    ENV_FILES,
    _block,
    _code_lines,
    _env_vars,
    _seq,
)

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
CHAT = COLLECTION / "chat"
NAME = "22-chat-unknown-part-non-stream.bru"
REQUEST = CHAT / NAME
README = COLLECTION / "README.md"
CANARY = "Q0FOQVJZLTQ3NC1VTktOT1dOLVBBUlQtREFUQQ=="
PART_TYPE = "custom_blob"

EXPECTED_TEST_LINES = [
    'test("the turn is answered with an HTTP status (recorded, not asserted)", function() {',
    'expect(res.getStatus()).to.be.a("number");',
    'bru.setVar("unknownPartReplyStatus", res.getStatus());',
    "});",
    'test("no echo of the canary", function() {',
    "const raw = JSON.stringify(res.getBody());",
    'expect(raw).to.not.contain(bru.getEnvVar("unknownPartCanary"));',
    "});",
    'test("the reply carries X-Trace-Id (stashed as unknownPartTraceId)", function() {',
    'expect(res.getHeader("x-trace-id")).to.match(/^[0-9a-f]{32}$/);',
    'bru.setVar("unknownPartTraceId", res.getHeader("x-trace-id"));',
    "});",
]


def _code() -> str:
    text = REQUEST.read_text()
    return "\n".join(
        _code_lines(text, "script:pre-request") + _code_lines(text, "tests")
    )


def test_the_environment_files_were_read() -> None:
    assert len(ENV_FILES) >= 3, (
        "control: an empty glob would make the next test vacuous"
    )


def test_seq_is_22_and_unique_in_chat() -> None:
    assert _seq(REQUEST) == 22
    assert [p.name for p in sorted(CHAT.glob("*.bru")) if _seq(p) == 22] == [NAME]


def test_posts_to_the_chat_route_with_the_access_token() -> None:
    text = REQUEST.read_text()
    assert _code_lines(text, "post") == [
        "url: {{baseUrl}}/v1/chat/completions",
        "body: json",
        "auth: bearer",
    ]
    assert _code_lines(text, "auth:bearer") == ["token: {{accessToken}}"]


def test_it_reads_and_sets_exactly_the_documented_variables() -> None:
    code = _code()
    assert set(re.findall(r'bru\.getEnvVar\("(\w+)"\)', code)) == {
        "contentPartPrompt",
        "unknownPartCanary",
    }
    assert set(re.findall(r'bru\.setVar\("(\w+)"', code)) == {
        "unknownPartReplyStatus",
        "unknownPartTraceId",
    }


def test_the_canary_default_is_the_same_short_string_in_every_environment() -> None:
    assert base64.b64decode(CANARY) == b"CANARY-474-UNKNOWN-PART-DATA"
    assert len(CANARY) <= 128
    for env in ENV_FILES:
        assert _env_vars(env).get("unknownPartCanary") == CANARY, env.name
        assert "contentPartPrompt" in _env_vars(env), env.name


def test_the_part_has_the_type_custom_blob_which_the_service_does_not_know() -> None:
    text = REQUEST.read_text()
    types = set(
        re.findall(
            r'\btype: "(\w+)"', "\n".join(_code_lines(text, "script:pre-request"))
        )
    )
    assert types == {"text", PART_TYPE}
    assert PART_TYPE not in set(_PAYLOAD_SLOTS) | set(_TEXT_SLOTS)
    assert set(re.findall(r'"type": "(\w+)"', _block(text, "body:json"))) == types


def test_the_assertion_lines_are_pinned_exactly() -> None:
    assert _code_lines(REQUEST.read_text(), "tests") == EXPECTED_TEST_LINES


def test_the_readme_lists_it_once_as_a_whole_token() -> None:
    assert README.read_text().count(f"`chat/{NAME}`") == 1
