"""MODEL-MMPROJ-PERMANENT ADDENDUM-1 -- structural guards for Bruno ``chat/17``.

``chat/17`` sends an image part with a model routed to a model server that has no
vision projector and asserts the explicit error. Parsed, not grepped, in the manner of
``tests/test_bruno_chat_image_part.py``; the message the request asserts is compared
with the one the code builds, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_bruno_chat_image_part import _block, _code_lines, _env_vars, _seq

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
CHAT = COLLECTION / "chat"
REQUEST = CHAT / "17-chat-image-part-no-projector.bru"
ENV_FILES = sorted((COLLECTION / "environments").glob("*.bru"))


def _error_object() -> dict[str, object]:
    from audittrace.routes import _upstream_errors

    error = _upstream_errors.image_input_unsupported_error_body()["error"]
    assert isinstance(error, dict)
    return error


def test_exists_with_seq_17_unique_in_chat() -> None:
    assert _seq(REQUEST) == 17
    assert [p.name for p in sorted(CHAT.glob("*.bru")) if _seq(p) == 17] == [
        REQUEST.name
    ]


def test_posts_to_the_chat_route_with_the_access_token() -> None:
    text = REQUEST.read_text()
    assert _code_lines(text, "post") == [
        "url: {{baseUrl}}/v1/chat/completions",
        "body: json",
        "auth: bearer",
    ]
    assert _code_lines(text, "auth:bearer") == ["token: {{accessToken}}"]


def test_reads_the_model_from_the_environment_and_reuses_the_image() -> None:
    code = "\n".join(_code_lines(REQUEST.read_text(), "script:pre-request"))
    assert set(re.findall(r'bru\.getEnvVar\("(\w+)"\)', code)) == {
        "imageNoProjectorModel",
        "imagePartData",
        "imagePartPrompt",
    }
    assert 'model: bru.getEnvVar("imageNoProjectorModel"),' in code
    assert "audittrace-default" not in code


def test_asserts_exactly_400_and_the_whole_error_object_the_code_builds() -> None:
    lines = _code_lines(REQUEST.read_text(), "tests")
    assert "expect(res.getStatus()).to.equal(400);" in lines
    assert sum("getStatus()" in ln for ln in lines) == 1
    inner = _block(REQUEST.read_text(), "tests")
    m = re.search(
        r"expect\(res\.getBody\(\)\.error\)\.to\.eql\(\{(.*?)\}\);", inner, re.S
    )
    assert m, "the error object must be compared whole (to.eql)"
    fields = dict(re.findall(r'^\s*(\w+): (".*?"|\d+),?$', m.group(1), re.M))
    assert {k: json.loads(v) for k, v in fields.items()} == _error_object()


def test_stashes_the_trace_id() -> None:
    lines = _code_lines(REQUEST.read_text(), "tests")
    assert "expect(traceId).to.match(/^[0-9a-f]{32}$/);" in lines
    assert 'bru.setVar("imageNoProjectorTraceId", traceId);' in lines


def test_readme_lists_the_request() -> None:
    assert (
        "chat/17-chat-image-part-no-projector.bru"
        in (COLLECTION / "README.md").read_text()
    )


@pytest.mark.parametrize("env_file", ENV_FILES, ids=lambda p: p.name)
def test_model_default_in_every_environment(env_file: Path) -> None:
    env = _env_vars(env_file)
    assert env.get("imageNoProjectorModel") == "mistral"
    assert "imageNoProjectorTraceId" not in env
    assert "imageNoProjectorSessionId" not in env
