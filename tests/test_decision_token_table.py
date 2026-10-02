"""#459 WU-459-1: the vendored added-token table is pinned (B1 / C3)."""

from __future__ import annotations

import hashlib
import json

from audittrace.services.decision.token_tables import ADDED_TOKEN_TABLES, table_for
from tests.fakes.decision_added_tokens import ALL_ADDED

F16 = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"


def test_table_is_keyed_by_the_f16_digest_and_only_that_one() -> None:
    assert set(ADDED_TOKEN_TABLES) == {F16}
    assert table_for(F16) is ADDED_TOKEN_TABLES[F16]
    assert table_for("ab" * 32) is None
    assert table_for("") is None


def test_table_equals_the_literal_test_list_exactly() -> None:
    table = table_for(F16)
    assert table is not None
    assert [(i, t.content) for i, t in table.tokens.items()] == list(ALL_ADDED)
    assert table.ids == {i for i, _ in ALL_ADDED}
    assert table.contents == tuple(s for _, s in ALL_ADDED)


def test_table_special_flags_match_the_measured_split() -> None:
    table = table_for(F16)
    assert table is not None
    special = [i for i, t in table.tokens.items() if t.special]
    assert len(table.tokens) == 33 and len(special) == 21
    not_special = {t.content for t in table.tokens.values() if not t.special}
    assert not_special == {
        "<tool_call>",
        "</tool_call>",
        "<|fim_prefix|>",
        "<|fim_middle|>",
        "<|fim_suffix|>",
        "<|fim_pad|>",
        "<|repo_name|>",
        "<|file_sep|>",
        "<tool_response>",
        "</tool_response>",
        "<think>",
        "</think>",
    }


def test_table_provenance_names_the_hf_commit_not_a_local_path() -> None:
    table = table_for(F16)
    assert table is not None
    assert table.source == (
        "HF togethercomputer/Tev1-0.8B-experimental @ "
        "6bb2dff14b38fea90ddb14d870166ccaf77374e9"
    )
    assert table.tokenizer_config_sha256 == (
        "4ed764fafae09ed7390f85ab1ddc28baa34a4805b4119048ba8d922abaaebcaf"
    )
    assert "/home/" not in table.source and "models/" not in table.source


def test_table_content_hash_is_pinned() -> None:
    table = table_for(F16)
    assert table is not None
    blob = json.dumps(
        [[i, t.content, t.special] for i, t in table.tokens.items()],
        separators=(",", ":"),
    )
    assert (
        hashlib.sha256(blob.encode()).hexdigest()
        == "5fbc0d6c4feb2c5eaaecd810063b2dc0bf9e117a9e5783603dce844ed2f7abf7"
    )


def test_table_is_read_only() -> None:
    import pytest

    table = table_for(F16)
    assert table is not None
    with pytest.raises(TypeError):
        table.tokens[1] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        ADDED_TOKEN_TABLES["x"] = table  # type: ignore[index]
