"""Structural guards for the shipped Bruno request ``health/openapi.bru`` (``GET /openapi.json``).

Parsed, not grepped, in the manner of ``tests/test_bruno_chat_image_part.py``. The request
reads the contract a running image serves; its assertions are compared with the vendored
snapshot (``tests/fixtures/openapi.snapshot.yaml``) and with the live app's own document, so
the names it asserts are compared with both.

The names are read out of the assertion CODE and matched EXACTLY (a name only mentioned in a
test title does not count, and ``content_hashes`` or ``input_mediaX`` is not ``content_hash`` /
``input_media``): ``test_a_misspelled_asserted_name_is_not_accepted`` pins that.
"""

from __future__ import annotations

import re
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
    assert _asserted(_block(REQUEST.read_text(), "tests")) == EXPECTED


#: what the request must assert, as exact names
EXPECTED = {
    "paths": {"/health", "/v1/chat/completions"},
    "schemas": {"InteractionListItem"},
    "properties": {"input_media", "content_hash"},
}


def _asserted(tests: str) -> dict[str, set[str]]:
    """The names the assertion code uses: path keys, schema names, schema properties.

    ``expect(body.paths).to.have.property("<path>")`` for the paths, ``components.schemas.<Name>``
    for the schema, ``expect(item.properties).to.have.property("<name>")`` and
    ``item.properties.<name>`` for the properties. Test titles are not read.
    """
    return {
        "paths": set(
            re.findall(r'body\.paths\)\.to\.have\.property\("([^"]+)"\)', tests)
        ),
        "schemas": set(re.findall(r"components\.schemas\.(\w+)", tests)),
        "properties": set(
            re.findall(r'item\.properties\)\.to\.have\.property\("([^"]+)"\)', tests)
        )
        | set(re.findall(r"item\.properties\.(\w+)", tests)),
    }


def test_a_misspelled_asserted_name_is_not_accepted() -> None:
    """Review A3: substring membership accepted ``content_hashes`` and ``input_mediaX``."""
    tests = _block(REQUEST.read_text(), "tests")
    assert _asserted(tests) == EXPECTED, "control: the shipped request is exact"
    for old, new in (
        ('property("content_hash")', 'property("content_hashes")'),
        ('property("input_media")', 'property("input_mediaX")'),
        ("item.properties.input_media.anyOf", "item.properties.input_mediaX.anyOf"),
        ("schemas.InteractionListItem", "schemas.InteractionListItems"),
        ('property("/health")', 'property("/healthz")'),
    ):
        assert old in tests, f"control: {old} is in the request"
        assert _asserted(tests.replace(old, new)) != EXPECTED, new
