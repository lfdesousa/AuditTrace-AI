"""#459 WU-459-2 chunk 03: the hash-pinned artefact record and its loader (Q9/B3, AC-MA2/AC-MA4 part).

The shipped record is loaded with the sha256 computed from the file itself; every
fact is asserted by LITERAL value (not read back from ``template.py`` or
``token_tables.py``, which chunk 05 deletes). Neuters: N1 (the sha256 check
skipped) turns :func:`test_sha256_mismatch_refused` RED; N2 (``table_for`` never
finds a table) turns the ``table_for`` tests RED.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from audittrace.services.decision.record import (
    RECORD_CODES,
    TEMPLATE_FAMILIES,
    ArtefactRecord,
    RecordError,
    load_record,
    resolve_record_path,
)
from audittrace.services.decision.token_tables import AddedTokenTable
from tests.fakes.decision_added_tokens import ALL_ADDED

RECORD_NAME = "tev1-0.8B-experimental.json"
DIGEST = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"
HF_COMMIT = "6bb2dff14b38fea90ddb14d870166ccaf77374e9"
RECORD_PATH = resolve_record_path(RECORD_NAME)
RECORD_SHA256 = hashlib.sha256(RECORD_PATH.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def record() -> ArtefactRecord:
    return load_record(RECORD_PATH, RECORD_SHA256)


def _copy_with(tmp_path: Path, mutate: Any) -> tuple[Path, str]:
    """A mutated copy of the shipped record and its OWN sha256."""
    data = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    mutate(data)
    out = tmp_path / RECORD_NAME
    out.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


# ----------------------------------------------------------------- the shipped record


def test_shipped_record_is_schema_v1_under_artefacts(record: ArtefactRecord) -> None:
    assert record.record_version == 1
    assert record.path == RECORD_PATH
    assert record.path.parent.name == "artefacts"
    assert record.path.parent.parent.name == "decision"
    assert record.sha256 == RECORD_SHA256
    assert record.model["id"] == "Tev1-0.8B-experimental"
    assert record.model["hf_commit"] == HF_COMMIT
    assert record.model["family"] == "qwen3.5"
    assert record.evaluation["state_max_chars"] == 4000
    assert record.evaluation["timeout_ms"] == 2000
    assert record.evaluation["temperature"] == 1.0


def test_added_tokens_equal_the_literal_list(record: ArtefactRecord) -> None:
    assert len(record.added_tokens) == len(record.added_token_ids) == 33
    got = [
        (i, t.content)
        for i, t in zip(record.added_token_ids, record.added_tokens, strict=True)
    ]
    assert got == list(ALL_ADDED)  # ids AND contents, in order
    assert record.tokenizer_config_sha256 == (
        "4ed764fafae09ed7390f85ab1ddc28baa34a4805b4119048ba8d922abaaebcaf"
    )
    assert sum(1 for t in record.added_tokens if t.special) == 21
    assert sum(1 for t in record.added_tokens if not t.special) == 12


def test_table_for_known_digest_is_the_table(record: ArtefactRecord) -> None:
    table = record.table_for(DIGEST)
    assert isinstance(table, AddedTokenTable)
    assert table.ids == frozenset(i for i, _ in ALL_ADDED)
    assert table.contents == tuple(s for _, s in ALL_ADDED)
    assert table.tokens[248066].content == "<tool_response>"
    assert table.tokens[248066].special is False
    assert table.tokens[248045].special is True
    assert table.source == f"HF togethercomputer/Tev1-0.8B-experimental @ {HF_COMMIT}"
    assert table.tokenizer_config_sha256 == record.tokenizer_config_sha256


def test_table_for_unknown_digest_is_none(record: ArtefactRecord) -> None:
    assert record.table_for("ab" * 32) is None
    assert record.table_for("") is None
    assert record.table_for(DIGEST.upper()) is None  # digests are lowercase hex


def test_digests_is_the_weights_digest_set(record: ArtefactRecord) -> None:
    assert record.digests == {DIGEST}
    assert len(record.weights) == 1
    w = record.weights[0]
    assert (w.file_basename, w.alias, w.quantisation, w.backend_measured) == (
        "tev1-0.8B-f16.gguf",
        "tev1-0.8B-f16",
        "f16",
        "vulkan",
    )


def test_template_block_by_value(record: ArtefactRecord) -> None:
    t = record.template
    assert t.template_id == "tev1-choice-v1"
    assert t.family in TEMPLATE_FAMILIES
    assert t.family == "chatml-think-off-v1"
    assert t.upstream_chat_template_sha256 == (
        "d78de6bee4c952ca3145eb161921560a6ede7b59a34e7e4be815f3c5386b4364"
    )
    assert dict(t.special_tokens) == {
        "im_start": "<|im_start|>",
        "im_end": "<|im_end|>",
        "think_open": "<think>",
        "think_close": "</think>",
    }
    assert dict(t.pieces) == {
        "user_role": "user\n",
        "assistant_role": "assistant\n",
        "newline": "\n",
        "think_body": "\n\n",
        "after_think": "\n\n",
    }
    assert dict(t.user_content_literals) == {
        "state_head": "State:\n",
        "question_head": "\n\nQuestion: ",
        "options_head": "\nOptions:\n",
        "option_sep": "\n",
        "instruction": "\nAnswer with the option letter only.",
    }


@pytest.mark.parametrize(
    "needle",
    [
        "being finalized",
        HF_COMMIT,
        "Apache-2.0",
        "2026-10-17",
        "accepted the risk",
        "IS ENABLED (shadow)",
        "OFF in the shipped defaults",
    ],
)
def test_licence_block_carries_each_element(
    record: ArtefactRecord, needle: str
) -> None:
    lic = record.licence
    blob = " ".join(
        [
            lic.status,
            lic.source_card_commit,
            lic.checked_on,
            lic.base_model,
            lic.base_licence,
            lic.risk_accepted_by,
            lic.accepted_on,
            lic.disclaimer,
        ]
    )
    assert needle in blob
    assert lic.status == "being finalized"
    assert lic.base_licence == "Apache-2.0"
    assert lic.risk_accepted_by == "operator"


def test_shipped_record_file_is_sorted_one_space_indented_json() -> None:
    text = RECORD_PATH.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, indent=1) + "\n"
    assert len(data["tokenizer"]["added_tokens"]) == 33


# ------------------------------------------------------------------- refusals


def test_sha256_mismatch_refused() -> None:
    with pytest.raises(RecordError) as info:
        load_record(RECORD_PATH, "00" * 32)
    assert info.value.code == "sha256_mismatch"
    assert "sha256_mismatch" in str(info.value)
    with pytest.raises(RecordError) as info2:
        load_record(
            RECORD_PATH, RECORD_SHA256[:-1] + ("0" if RECORD_SHA256[-1] != "0" else "1")
        )
    assert info2.value.code == "sha256_mismatch"


def test_missing_path_refused(tmp_path: Path) -> None:
    with pytest.raises(RecordError) as info:
        load_record(tmp_path / "nope.json", RECORD_SHA256)
    assert info.value.code == "missing"


def test_unknown_family_refused(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["template"]["family"] = "foo"

    path, sha = _copy_with(tmp_path, mutate)
    with pytest.raises(RecordError) as info:
        load_record(path, sha)
    assert info.value.code == "family_unknown"
    assert "foo" in str(info.value)


def test_missing_record_version_is_a_schema_error(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        del d["record_version"]

    path, sha = _copy_with(tmp_path, mutate)
    with pytest.raises(RecordError) as info:
        load_record(path, sha)
    assert info.value.code == "schema"
    assert "record_version" in str(info.value)


@pytest.mark.parametrize(
    ("name", "mutate", "detail"),
    [
        ("version-2", lambda d: d.__setitem__("record_version", 2), "record_version"),
        ("no-weights", lambda d: d.__setitem__("weights", []), "weights"),
        (
            "uppercase-digest",
            lambda d: d["weights"][0].__setitem__("digest", DIGEST.upper()),
            "weights[0].digest",
        ),
        (
            "short-digest",
            lambda d: d["weights"][0].__setitem__("digest", "abc"),
            "weights[0].digest",
        ),
        ("no-licence", lambda d: d.pop("licence"), "licence"),
        (
            "licence-key-missing",
            lambda d: d["licence"].pop("disclaimer"),
            "licence.disclaimer",
        ),
        (
            "token-id-not-increasing",
            lambda d: d["tokenizer"]["added_tokens"][5].__setitem__("id", 248044),
            "not strictly increasing",
        ),
        (
            "token-special-not-bool",
            lambda d: d["tokenizer"]["added_tokens"][0].__setitem__("special", "yes"),
            "added_tokens[0].special",
        ),
        (
            "special-token-missing",
            lambda d: d["template"]["special_tokens"].pop("think_close"),
            "template.special_tokens.think_close",
        ),
        ("no-evaluation", lambda d: d.pop("evaluation"), "evaluation"),
    ],
)
def test_schema_violations_are_schema_errors_naming_the_key(
    tmp_path: Path, name: str, mutate: Any, detail: str
) -> None:
    path, sha = _copy_with(tmp_path, mutate)
    with pytest.raises(RecordError) as info:
        load_record(path, sha)
    assert info.value.code == "schema", name
    assert detail in str(info.value)


def test_schema_error_never_carries_the_file_contents(tmp_path: Path) -> None:
    path = tmp_path / RECORD_NAME
    path.write_text("{this is not json: <|im_start|>}", encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RecordError) as info:
        load_record(path, sha)
    assert info.value.code == "schema"
    assert "<|im_start|>" not in str(info.value)


def test_record_error_refuses_an_unknown_code() -> None:
    with pytest.raises(ValueError, match="unknown record code"):
        RecordError("nope")
    assert RECORD_CODES == {"missing", "sha256_mismatch", "schema", "family_unknown"}
    err = RecordError("missing", "x")
    assert err.code == "missing" and isinstance(err, ValueError)


def test_resolve_record_path_absolute_and_relative(tmp_path: Path) -> None:
    absolute = tmp_path / "r.json"
    assert resolve_record_path(str(absolute)) == absolute
    relative = resolve_record_path("x.json")
    assert relative.name == "x.json"
    assert relative.parent.name == "artefacts"
    assert relative.parent.parent.name == "decision"
    assert relative.is_absolute()
