"""CONSOLE-AUDIT-EVIDENCE WU-A -- the evidence integrity verdict (G4''', G8', G25', AC5').

* G4''' the hashed field set is frozen: ``_CONTENT_FIELDS`` is the 16-tuple,
  ``_OPTIONAL_CONTENT_FIELDS`` is ``("input_media",)``, ``client_message_id`` is in
  neither and ``recall_matches`` is on no hashed path. The hashed WRITERS are pinned by
  measurement (stdlib ``ast`` over ``src/``, a different instrument from the index) as the
  ``InteractionRecord`` / ``InteractionRow`` CONSTRUCTORS that carry the ``content_hash``
  keyword: 5 hashed, 2 unhashed (A7 C1; call sites of ``content_hash`` are NOT the pin,
  the evidence route itself calls it).
* G8' the verdict grid: untouched -> match (with and without ``input_media``, and a
  pre-1.30 row); any hashed field altered, ``input_media`` included -> mismatch; stored
  hash NULL -> unavailable (also a ``memory_access`` / ``security`` row).
* G25' fixtures v3 and v3-image recompute with ``audittrace.integrity.content_hash`` after
  the canonical re-serialisation of ``interaction.input_media`` (A6 section 1).

Neuters (each alone): G4''' append ``client_message_id`` to either tuple ->
``test_client_message_id_is_in_neither_tuple`` RED; add a hashed constructor ->
``test_the_hashed_writer_set_is_pinned_by_constructors`` RED; G8' call
``verify_content_hash`` directly for the NULL row -> ``test_null_hash_is_unavailable_not_
mismatch`` RED (it reads ``mismatch``); compare the stored hash with itself ->
``test_altered_*_is_a_mismatch`` RED; G25' hash the object (``payload["input_media"] =
interaction["input_media"]``) -> ``test_fixture_v3_image_object_form_digest_differs`` documents
it and ``test_fixtures_recompute`` RED; drop the re-serialisation sentence from the served
``hash_inputs`` -> ``test_hash_inputs_text_is_the_fixtures`` RED.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audittrace import integrity
from audittrace.integrity import content_hash, verify_content_hash
from audittrace.services.evidence.integrity_view import (
    HASH_INPUTS,
    hashed_field_names,
    integrity_block,
)
from tests._evidence_rig import FIXTURE_V2, FIXTURE_V3, FIXTURE_V3_IMAGE, load

SRC = Path(integrity.__file__).parent
FIXTURE_SHA = {
    FIXTURE_V2: "24990751beecbc7fdd8bc5b77bb74014611ac7a7fb302c96f9ffc4d00f906c4c",
    FIXTURE_V3: "e444ce53dd0fe315ecc76b8321a5341039534bbdeaf6fb1383714d8bdc8c1001",
    FIXTURE_V3_IMAGE: "2dae13bd8010ea6a2649ca3e15c5d6fe957204b01b6373a3123cb5794c989864",
}
FROZEN_16 = (
    "project",
    "source",
    "question",
    "answer",
    "prompt_tokens",
    "completion_tokens",
    "timestamp",
    "session_id",
    "model",
    "user_id",
    "status",
    "failure_class",
    "error_detail",
    "duration_ms",
    "trace_id",
    "event_class",
)


@pytest.mark.parametrize("path", sorted(FIXTURE_SHA), ids=lambda p: p.name)
def test_the_fixture_bytes_are_the_frozen_ones(path: Path) -> None:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FIXTURE_SHA[path]


class TestG4HashedFieldSetIsFrozen:
    def test_the_field_tuples(self) -> None:
        assert integrity._CONTENT_FIELDS == FROZEN_16
        assert integrity._OPTIONAL_CONTENT_FIELDS == ("input_media",)

    def test_client_message_id_is_in_neither_tuple(self) -> None:
        assert "client_message_id" not in integrity._CONTENT_FIELDS
        assert "client_message_id" not in integrity._OPTIONAL_CONTENT_FIELDS

    def test_recall_matches_is_on_no_hashed_path(self) -> None:
        text = (SRC / "integrity.py").read_text()
        assert "recall_matches" not in text
        assert "client_message_id" not in text

    def test_the_correlation_id_does_not_move_a_hash(self) -> None:
        fields: dict[str, Any] = {k: f"v-{k}" for k in FROZEN_16}
        assert content_hash({**fields, "client_message_id": "m"}) == content_hash(
            fields
        )
        assert content_hash({**fields, "recall_matches": [1]}) == content_hash(fields)

    def test_the_hashed_writer_set_is_pinned_by_constructors(self) -> None:
        hashed: list[str] = []
        unhashed: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name not in {"InteractionRecord", "InteractionRow"}:
                    continue
                keywords = {k.arg for k in node.keywords}
                bucket = hashed if "content_hash" in keywords else unhashed
                bucket.append(str(path.relative_to(SRC)))
        assert len(hashed) == 5, hashed
        assert sorted(unhashed) == [
            "services/memory_audit.py",
            "services/scan_audit_consumer.py",
        ]
        assert "client_message_id" not in (SRC / "integrity.py").read_text()


def _row(fields: dict[str, Any], **over: Any) -> SimpleNamespace:
    row = {k: fields.get(k) for k in (*FROZEN_16, "input_media")}
    row["content_hash"] = content_hash(fields)
    row.update(over)
    return SimpleNamespace(**row)


BASE: dict[str, Any] = {
    **{k: f"v-{k}" for k in FROZEN_16},
    "prompt_tokens": 3,
    "completion_tokens": 4,
    "duration_ms": 5,
    "failure_class": None,
    "error_detail": None,
    "event_class": None,
}
MEDIA_TEXT = json.dumps(
    {"count": 1, "items": [], "schema": 1}, sort_keys=True, separators=(",", ":")
)


class TestG8VerdictGrid:
    def test_untouched_is_a_match(self) -> None:
        assert integrity_block(_row(BASE))["recomputed"] == "match"

    def test_a_pre_1_30_row_is_a_match(self) -> None:
        """A row written before the column existed has no ``client_message_id``
        attribute at all."""
        row = _row(BASE)
        assert not hasattr(row, "client_message_id")
        assert integrity_block(row)["recomputed"] == "match"

    def test_untouched_image_row_is_a_match(self) -> None:
        fields = {**BASE, "input_media": MEDIA_TEXT}
        assert integrity_block(_row(fields))["recomputed"] == "match"

    @pytest.mark.parametrize("field", FROZEN_16)
    def test_altered_hashed_field_is_a_mismatch(self, field: str) -> None:
        row = _row(BASE)
        old = getattr(row, field)
        setattr(row, field, 99 if isinstance(old, int) else f"{old}-tampered")
        assert integrity_block(row)["recomputed"] == "mismatch"

    def test_altered_input_media_is_a_mismatch(self) -> None:
        row = _row({**BASE, "input_media": MEDIA_TEXT})
        row.input_media = MEDIA_TEXT.replace('"count":1', '"count":2')
        assert integrity_block(row)["recomputed"] == "mismatch"

    def test_an_image_added_after_the_fact_is_a_mismatch(self) -> None:
        row = _row(BASE)
        row.input_media = MEDIA_TEXT
        assert integrity_block(row)["recomputed"] == "mismatch"

    @pytest.mark.parametrize(
        "over",
        [{}, {"event_class": "security"}, {"event_class": "memory_access"}],
        ids=["plain", "security", "memory-audit"],
    )
    def test_null_hash_is_unavailable_not_mismatch(self, over: dict[str, Any]) -> None:
        row = _row({**BASE, **over}, content_hash=None, **over)
        block = integrity_block(row)
        assert block["recomputed"] == "unavailable"
        assert block["content_hash"] is None
        assert verify_content_hash(row) is False  # the neuter's reading: "mismatch"

    def test_hashed_fields_are_emitted_from_the_tuples(self) -> None:
        assert hashed_field_names(_row(BASE)) == list(integrity._CONTENT_FIELDS)
        with_media = _row({**BASE, "input_media": MEDIA_TEXT})
        assert hashed_field_names(with_media) == [
            *integrity._CONTENT_FIELDS,
            *integrity._OPTIONAL_CONTENT_FIELDS,
        ]

    def test_the_block_serves_the_recipe_text(self) -> None:
        assert integrity_block(_row(BASE))["hash_inputs"] == HASH_INPUTS


def _third_party_hash(interaction: dict[str, Any], hashed_fields: list[str]) -> str:
    """The recipe served in ``integrity.hash_inputs``, stdlib only (a different
    instrument from ``integrity.content_hash``)."""
    payload = {k: interaction[k] for k in hashed_fields}
    if interaction["input_media"] is not None:
        payload["input_media"] = json.dumps(
            interaction["input_media"], sort_keys=True, separators=(",", ":")
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _repo_hash(fixture: dict[str, Any], *, canonical_media: bool) -> str:
    interaction = dict(fixture["interaction"])
    if interaction["input_media"] is not None and canonical_media:
        interaction["input_media"] = json.dumps(
            interaction["input_media"], sort_keys=True, separators=(",", ":")
        )
    return content_hash(interaction)


@pytest.mark.parametrize("path", [FIXTURE_V3, FIXTURE_V3_IMAGE], ids=lambda p: p.name)
def test_fixtures_recompute(path: Path) -> None:
    """G25'. Both recipes (repo module, stdlib) reproduce the served hash."""
    fixture = load(path)
    served = fixture["integrity"]
    assert _repo_hash(fixture, canonical_media=True) == served["content_hash"]
    assert (
        _third_party_hash(fixture["interaction"], served["hashed_fields"])
        == served["content_hash"]
    )
    assert served["recomputed"] == "match"


def test_fixture_v2_still_recomputes_to_a_match() -> None:
    """A3: the superseded v2 (no ``input_media`` key at all) is the shape of every row
    written before migration 040; both recipes reproduce its hash."""
    fixture = load(FIXTURE_V2)
    assert "input_media" not in fixture["interaction"]
    served = fixture["integrity"]
    assert content_hash(fixture["interaction"]) == served["content_hash"]
    assert (
        _third_party_hash_v2(fixture["interaction"], served["hashed_fields"])
        == served["content_hash"]
    )


def _third_party_hash_v2(interaction: dict[str, Any], hashed_fields: list[str]) -> str:
    payload = {k: interaction[k] for k in hashed_fields}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_fixture_v3_image_object_form_digest_differs() -> None:
    """The G25' neuter is real: hashing the served object, not its stored text."""
    fixture = load(FIXTURE_V3_IMAGE)
    assert (
        _repo_hash(fixture, canonical_media=False)
        != fixture["integrity"]["content_hash"]
    )


def test_fixture_v3_hashed_fields_lists_input_media_only_when_not_null() -> None:
    assert "input_media" not in load(FIXTURE_V3)["integrity"]["hashed_fields"]
    assert "input_media" in load(FIXTURE_V3_IMAGE)["integrity"]["hashed_fields"]


@pytest.mark.parametrize("path", [FIXTURE_V3, FIXTURE_V3_IMAGE], ids=lambda p: p.name)
def test_hash_inputs_text_is_the_fixtures(path: Path) -> None:
    assert load(path)["integrity"]["hash_inputs"] == HASH_INPUTS
    assert "json.dumps(interaction['input_media'], sort_keys=True" in HASH_INPUTS
