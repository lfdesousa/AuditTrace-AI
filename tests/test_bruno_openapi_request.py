"""Structural guards for the shipped Bruno request ``health/openapi.bru`` (``GET /openapi.json``).

Parsed, not grepped, in the manner of ``tests/test_bruno_chat_image_part.py``. The request
reads the contract a running image serves; its assertions are compared with the vendored
snapshot (``tests/fixtures/openapi.snapshot.yaml``) and with the live app's own document, so
the names it asserts cannot drift away from the contract.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.test_bruno_chat_image_part import _block, _code_lines, _seq

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "bruno" / "audittrace" / "health" / "openapi.bru"
README = ROOT / "bruno" / "audittrace" / "README.md"
SNAPSHOT = ROOT / "tests" / "fixtures" / "openapi.snapshot.yaml"


def test_it_gets_the_served_contract_without_a_token() -> None:
    """The route has no auth dependency; the request must not pretend otherwise."""
    assert _code_lines(REQUEST.read_text(), "get") == [
        "url: {{baseUrl}}/openapi.json",
        "body: none",
        "auth: none",
    ]
    assert "auth:bearer" not in REQUEST.read_text()


def test_seq_is_unique_in_the_health_folder() -> None:
    seqs = [_seq(p) for p in sorted(REQUEST.parent.glob("*.bru"))]
    assert len(seqs) == 3, "control: the folder was read"
    assert len(set(seqs)) == len(seqs)
    assert _seq(REQUEST) == 3


def test_it_carries_assertions_and_the_readme_lists_it() -> None:
    assert _block(REQUEST.read_text(), "tests").count("test(") == 3
    assert "health/openapi.bru" in README.read_text()


def test_the_asserted_schema_names_exist_in_the_snapshot_and_in_the_app() -> None:
    from audittrace.server import app

    snapshot = yaml.safe_load(SNAPSHOT.read_text(encoding="utf-8"))
    for doc in (snapshot, app.openapi()):
        item = doc["components"]["schemas"]["InteractionListItem"]
        assert {"input_media", "content_hash"} <= set(item["properties"])
        kinds = {s.get("type") for s in item["properties"]["input_media"]["anyOf"]}
        assert kinds == {"object", "null"}
        assert "/health" in doc["paths"] and "/v1/chat/completions" in doc["paths"]
    text = _block(REQUEST.read_text(), "tests")
    for needle in ("InteractionListItem", "input_media", "content_hash", "/health"):
        assert needle in text, needle
