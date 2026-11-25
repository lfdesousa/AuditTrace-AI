"""#61 WU-2: the three shipped Bruno requests of the AC-21 leg (create, update, read).

AC-21 needs an UNSTAMPED corpus-tier semantic document. The sanctioned path is
``POST /memory/semantic`` with ``"tier": "corpus"``; these pins tie the three
requests to that route (the tier resolver, the corpus scope names, the exact
status) so a request that asks for the wrong tier, or asserts a text the
update did not write, fails here and not in front of the operator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from audittrace.auth import ALL_SCOPES
from audittrace.routes.memory import _resolve_requested_tier
from tests.test_bruno_chat_image_part import _block, _code_lines, _env_vars, _seq

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
SEMANTIC = COLLECTION / "memory" / "semantic"
CREATE = SEMANTIC / "02c-create-corpus-document.bru"
UPDATE = SEMANTIC / "04d-update-corpus-document.bru"
READ = SEMANTIC / "03c-read-corpus-document-text.bru"
REQUESTS = [CREATE, UPDATE, READ]
ENV_FILES = [COLLECTION / "environments" / f"{n}.bru" for n in ("cluster", "local")]
UPDATED_TEXT = "Scratch corpus document, UPDATED by 04d."


def _body(path: Path) -> dict[str, object]:
    parsed = json.loads(_block(path.read_text(), "body:json"))
    assert isinstance(parsed, dict)
    return parsed


def test_there_is_something_to_check() -> None:
    """Positive control: an empty glob or a missing request makes every pin vacuous."""
    assert all(p.is_file() for p in REQUESTS)
    assert all(p.is_file() for p in ENV_FILES)


def test_the_three_requests_have_their_own_seq_in_the_folder() -> None:
    seqs = [_seq(p) for p in REQUESTS]
    assert seqs == [9, 10, 11]
    others = [_seq(p) for p in SEMANTIC.glob("*.bru") if p not in REQUESTS]
    assert not set(seqs) & set(others)


class TestCreate:
    def test_is_the_semantic_post_with_the_bearer_bridge(self) -> None:
        text = CREATE.read_text()
        assert _code_lines(text, "post") == [
            "url: {{baseUrl}}/memory/semantic",
            "body: json",
            "auth: bearer",
        ]
        assert _code_lines(text, "auth:bearer") == ["token: {{accessToken}}"]

    def test_asks_for_the_corpus_tier_and_the_route_resolves_it_so(self) -> None:
        body = _body(CREATE)
        assert body["tier"] == "corpus"
        assert body["collection"] == "{{exampleCollection}}"
        assert body["document_id"] == "{{exampleDocId}}"
        assert _resolve_requested_tier(body, None) == "corpus"

    def test_carries_no_fold_marker_so_the_document_is_unstamped(self) -> None:
        assert set(_body(CREATE)) == {
            "collection",
            "document_id",
            "text",
            "title",
            "tier",
        }

    def test_asserts_the_exact_status_the_tier_and_the_key(self) -> None:
        lines = _code_lines(CREATE.read_text(), "tests")
        assert "expect(res.getStatus()).to.equal(200);" in lines
        assert 'expect(res.getBody().tier).to.equal("corpus");' in lines
        assert "expect(e.key).to.equal(expected);" in lines
        assert "expect(e.deleted_at_ms).to.equal(null);" in lines


class TestUpdate:
    def test_is_the_semantic_put_of_the_same_document(self) -> None:
        text = UPDATE.read_text()
        assert _code_lines(text, "put") == [
            "url: {{baseUrl}}/memory/semantic/{{exampleCollection}}/{{exampleDocId}}",
            "body: json",
            "auth: bearer",
        ]
        assert _body(UPDATE) == {"text": UPDATED_TEXT}

    def test_asserts_the_exact_status_and_the_corpus_tier(self) -> None:
        lines = _code_lines(UPDATE.read_text(), "tests")
        assert "expect(res.getStatus()).to.equal(200);" in lines
        assert 'expect(res.getBody().tier).to.equal("corpus");' in lines
        assert not [ln for ln in lines if "409" in ln or "private" in ln]


class TestRead:
    def test_is_the_semantic_get_of_the_same_document(self) -> None:
        assert _code_lines(READ.read_text(), "get") == [
            "url: {{baseUrl}}/memory/semantic/{{exampleCollection}}/{{exampleDocId}}",
            "body: none",
            "auth: bearer",
        ]

    def test_asserts_the_text_the_update_wrote_and_the_corpus_tier(self) -> None:
        lines = _code_lines(READ.read_text(), "tests")
        assert "expect(res.getStatus()).to.equal(200);" in lines
        assert f'expect(res.getBody().content).to.equal("{UPDATED_TEXT}");' in lines
        assert 'expect(res.getBody().manifest.tier).to.equal("corpus");' in lines


@pytest.mark.parametrize("env", ENV_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("request_path", REQUESTS, ids=lambda p: p.name)
def test_every_variable_of_the_request_is_defined(
    request_path: Path, env: Path
) -> None:
    used = set(re.findall(r"\{\{(\w+)\}\}", request_path.read_text()))
    assert not used - set(_env_vars(env)) - {"accessToken"}


def test_the_docs_name_scopes_the_server_declares() -> None:
    """The login hint is only useful if every scope in it exists."""
    docs = "\n".join(_code_lines(CREATE.read_text(), "docs"))
    named = set(re.findall(r"memory:(?:corpus:semantic|semantic):(?:read|write)", docs))
    assert {"memory:corpus:semantic:write", "memory:semantic:write"} <= named
    assert named <= set(ALL_SCOPES)
