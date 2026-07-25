"""Tests for the shared layer-listing helper (backlog #15, Defect A).

``list_layer_objects`` + ``is_listable_md_object`` are the single source of
truth for "what objects exist in a memory layer", so the list, the
/memory/index walk, and the cache cannot diverge again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from audittrace.services.layer_listing import (
    is_listable_md_object,
    list_layer_objects,
)


class _Obj:
    """Minimal ObjectMetadata-shaped double."""

    def __init__(
        self,
        object_name: str | None,
        size: int | None = None,
        last_modified: Any = None,
    ) -> None:
        self.object_name = object_name
        self.size = size
        self.last_modified = last_modified


class _Client:
    def __init__(self, objects: list[_Obj]) -> None:
        self._objects = objects
        self.calls: list[tuple[str, str]] = []

    def list_objects(self, bucket: str, prefix: str = "") -> list[_Obj]:
        self.calls.append((bucket, prefix))
        return self._objects


class TestListLayerObjects:
    def test_returns_key_filename_size_and_timestamp(self) -> None:
        lm = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        client = _Client([_Obj("episodic/decision-x.md", size=42, last_modified=lm)])
        got = list_layer_objects(client, "bucket", "episodic/")
        assert got == [
            {
                "key": "episodic/decision-x.md",
                "filename": "decision-x.md",
                "size_bytes": 42,
                "last_modified_ms": int(lm.timestamp() * 1000),
            }
        ]

    def test_prefix_is_passed_as_keyword(self) -> None:
        client = _Client([])
        list_layer_objects(client, "bucket", "episodic/")
        assert client.calls == [("bucket", "episodic/")]

    def test_directory_markers_and_none_names_are_skipped(self) -> None:
        client = _Client(
            [
                _Obj("episodic/ADR-1.md"),
                _Obj("episodic/"),  # directory marker → empty leaf
                _Obj(None),  # defensive
            ]
        )
        got = list_layer_objects(client, "bucket", "episodic/")
        assert [o["filename"] for o in got] == ["ADR-1.md"]

    def test_unprefixed_key_keeps_its_whole_name(self) -> None:
        client = _Client([_Obj("top-level.md")])
        got = list_layer_objects(client, "bucket", "")
        assert got[0]["key"] == "top-level.md"
        assert got[0]["filename"] == "top-level.md"

    def test_non_int_size_and_non_datetime_ts_coalesce_to_none(self) -> None:
        """Test doubles / backends that omit size or last_modified must not
        crash enumeration — the fields degrade to None."""
        client = _Client(
            [_Obj("episodic/x.md", size="not-an-int", last_modified="nope")]
        )
        got = list_layer_objects(client, "bucket", "episodic/")
        assert got[0]["size_bytes"] is None
        assert got[0]["last_modified_ms"] is None


class TestIsListableMdObject:
    def test_md_is_listable(self) -> None:
        assert is_listable_md_object("decision-2026-07-24.md") is True
        assert is_listable_md_object("README.md") is True
        assert is_listable_md_object("ADR-001.md") is True

    def test_non_md_is_not_listable(self) -> None:
        assert is_listable_md_object("paper.pdf") is False
        assert is_listable_md_object("notes.txt") is False
        assert is_listable_md_object("nodotmd") is False
