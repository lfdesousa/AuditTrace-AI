"""MODEL-MMPROJ-PERMANENT -- structural guards for Bruno ``chat/16`` (image part).

No live Bruno run here (the live run is ADR-049 evidence). These tests PARSE the
request and the environment files, in the manner of
``tests/test_bruno_wp4_collection.py``:

* the request exists, is ``seq: 16``, and no other chat request shares its seq;
* it posts to ``/v1/chat/completions`` with the ``accessToken`` bearer;
* every environment variable it reads has a default in EVERY environment file,
  except the two it sets itself at run time;
* the default image is the same small PNG in every environment (pinned by
  sha256), so the request runs with no ``--env-var``;
* the pre-request script refuses a value that is not a ``data:image/`` URL
  (measured: without the refusal Bruno sends ``"image_url": {}``);
* the assertion lines are pinned exactly.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
CHAT = COLLECTION / "chat"
REQUEST = CHAT / "16-chat-image-part-non-stream.bru"
ENV_FILES = sorted((COLLECTION / "environments").glob("*.bru"))

PNG_SHA256 = "3aede1459192d34f16574afefedb8a0e4cd03f4a60bda50ad6e32a0e62f60631"
PNG_LEN = 9189
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DATA_URL_PREFIX = "data:image/png;base64,"

#: set by the request itself (pre-request script / tests block), never an env default
RUNTIME_VARS = {"imagePartTraceId"}
EXPECTED_ENV_READS = {
    "imagePartData",
    "imagePartPrompt",
    "imagePartExpect",
    "imagePartSessionId",
}

EXPECTED_TEST_LINES = [
    'test("returns 200", function() {',
    "expect(res.getStatus()).to.equal(200);",
    "});",
    'test("OpenAI shape preserved (feedback_openai_schema_inviolate)", function() {',
    "const b = res.getBody();",
    'expect(b.object).to.equal("chat.completion");',
    'expect(b.choices).to.be.an("array").with.lengthOf.at.least(1);',
    'expect(b.choices[0].message.role).to.equal("assistant");',
    'expect(b.choices[0].message.content).to.be.a("string");',
    "});",
    'test("no echo of the image data", function() {',
    "const raw = JSON.stringify(res.getBody());",
    'expect(raw).to.not.contain("base64,");',
    "});",
    'test("the answer names what the image shows (imagePartExpect)", function() {',
    'expect(res.getBody().choices[0].message.content).to.contain(bru.getEnvVar("imagePartExpect"));',
    "});",
    'test("x-trace-id is 32 lowercase hex (stashed for audit/02)", function() {',
    'const traceId = res.getHeader("x-trace-id");',
    "expect(traceId).to.match(/^[0-9a-f]{32}$/);",
    'bru.setVar("imagePartTraceId", traceId);',
    "});",
]
EXPECTED_GUARD_LINES = [
    'const image = bru.getEnvVar("imagePartData");',
    'if (typeof image !== "string" || !image.startsWith("data:image/")) {',
    'throw new Error("imagePartData must be a data:image/... URL (default in environments/*.bru)");',
    "}",
]


def _block(text: str, name: str) -> str:
    """Inner text of the top-level ``name { ... }`` block ('' if absent)."""
    m = re.search(rf"^{re.escape(name)} \{{\n", text, re.M)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _code_lines(text: str, block: str) -> list[str]:
    inner = re.sub(r"/\*.*?\*/", "", _block(text, block), flags=re.S)
    kept = []
    for raw in inner.splitlines():
        line = raw.strip()
        if line and not line.startswith("//"):
            kept.append(line)
    return kept


def _env_vars(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in _block(path.read_text(), "vars").splitlines():
        key, sep, value = raw.strip().partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _seq(path: Path) -> int:
    m = re.search(r"^\s*seq: (\d+)$", _block(path.read_text(), "meta"), re.M)
    assert m, f"{path.name} has no seq"
    return int(m.group(1))


def test_there_are_environment_files_to_check() -> None:
    """Positive control: an empty glob would make every per-environment test vacuous."""
    assert len(ENV_FILES) >= 2
    assert {"cluster.bru", "local.bru"} <= {p.name for p in ENV_FILES}


class TestRequest:
    def test_exists_with_seq_16_and_the_seq_is_unique_in_chat(self) -> None:
        assert _seq(REQUEST) == 16
        sharing = [p.name for p in sorted(CHAT.glob("*.bru")) if _seq(p) == 16]
        assert sharing == [REQUEST.name]

    def test_posts_to_the_chat_route_with_the_access_token(self) -> None:
        text = REQUEST.read_text()
        assert _code_lines(text, "post") == [
            "url: {{baseUrl}}/v1/chat/completions",
            "body: json",
            "auth: bearer",
        ]
        assert _code_lines(text, "auth:bearer") == ["token: {{accessToken}}"]

    def test_reads_exactly_the_documented_environment_variables(self) -> None:
        text = REQUEST.read_text()
        code = "\n".join(
            _code_lines(text, "script:pre-request") + _code_lines(text, "tests")
        )
        assert set(re.findall(r'bru\.getEnvVar\("(\w+)"\)', code)) == EXPECTED_ENV_READS
        assert set(re.findall(r'bru\.setVar\("(\w+)"', code)) == {
            "imagePartSessionId",
            "imagePartTraceId",
        }

    def test_pre_request_refuses_a_value_that_is_not_an_image_data_url(self) -> None:
        lines = _code_lines(REQUEST.read_text(), "script:pre-request")
        at = lines.index(EXPECTED_GUARD_LINES[0])
        assert lines[at : at + len(EXPECTED_GUARD_LINES)] == EXPECTED_GUARD_LINES
        assert at < lines.index("req.setBody({"), "the refusal must precede the body"
        assert '{ type: "image_url", image_url: { url: image } }' in lines

    def test_no_default_hides_in_the_script(self) -> None:
        """Defaults live in environments/*.bru, the collection's convention."""
        code = "\n".join(_code_lines(REQUEST.read_text(), "script:pre-request"))
        assert not re.search(
            r'getEnvVar\("imagePart(Prompt|Data|Expect)"\)\s*\|\|', code
        )

    def test_assertion_lines_are_pinned(self) -> None:
        assert _code_lines(REQUEST.read_text(), "tests") == EXPECTED_TEST_LINES

    def test_readme_lists_the_request(self) -> None:
        assert (
            "chat/16-chat-image-part-non-stream.bru"
            in (COLLECTION / "README.md").read_text()
        )


@pytest.mark.parametrize("env_file", ENV_FILES, ids=lambda p: p.name)
class TestEnvironmentDefaults:
    def test_every_read_variable_has_a_default_entry(self, env_file: Path) -> None:
        defined = set(_env_vars(env_file))
        assert EXPECTED_ENV_READS <= defined
        assert not (RUNTIME_VARS & defined), "a runtime variable must not get a default"

    def test_default_image_is_the_pinned_png(self, env_file: Path) -> None:
        value = _env_vars(env_file)["imagePartData"]
        assert value.startswith(DATA_URL_PREFIX)
        png = base64.b64decode(value[len(DATA_URL_PREFIX) :], validate=True)
        assert png.startswith(PNG_MAGIC)
        assert len(png) == PNG_LEN
        assert hashlib.sha256(png).hexdigest() == PNG_SHA256

    def test_prompt_and_expected_token_defaults(self, env_file: Path) -> None:
        env = _env_vars(env_file)
        assert env["imagePartExpect"] == "ADR-049"
        assert "decision number" in env["imagePartPrompt"]
        assert env["imagePartSessionId"] == ""


def test_defaults_are_identical_in_every_environment() -> None:
    seen = {
        tuple(_env_vars(p).get(name) for name in sorted(EXPECTED_ENV_READS))
        for p in ENV_FILES
    }
    assert len(seen) == 1
    assert None not in next(iter(seen)), "a default is missing everywhere"
