"""LIBRECHAT-IMAGE-SLICE ADDENDUM-1 AC-9 -- the published contract of the new audit fields.

The operator waived the audit-schema freeze for this release on three conditions; this
file pins the published text: the hash recipe (the 16 content fields, plus ``input_media``
when it is set), the closed ``failure_class`` addition, the rollback effect, and the
meaning of NULL ("no image recorded", with its boundaries) in the API field description,
the vendored OpenAPI contract, the integrity note and the public guide.

Equalities, not lists: the note's field list is compared with ``integrity._CONTENT_FIELDS``;
its worked example is recomputed here by an independent implementation of the recipe.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from audittrace import integrity
from audittrace.models import InteractionListItem

ROOT = Path(__file__).resolve().parents[1]
NOTE = ROOT / "docs" / "reference" / "audittrace" / "audit-record-integrity.md"
GUIDE = ROOT / "docs" / "guides" / "librechat-attachments-support.md"
VENDORED = ROOT / "docs" / "reference" / "audittrace" / "openapi.yaml"
SNAPSHOT = ROOT / "tests" / "fixtures" / "openapi.snapshot.yaml"
SRC = ROOT / "src" / "audittrace"

INPUT_MEDIA_DESCRIPTION = (
    "Images carried by the chat request of this row: one entry per image part with "
    "its position, declared media type, decoded size in bytes and SHA-256 of the "
    "decoded bytes; never the image. null means no image recorded, which is not "
    "always the same as no image sent: rows written before this field existed, rows "
    "written by a server of an earlier release, and rows that are not chat turns are "
    "always null."
)
CONTENT_HASH_DESCRIPTION = (
    "SHA-256 over the canonical JSON of the row's content fields. input_media is "
    "part of the hashed content only when it is not null; see the audit record "
    "integrity note for the exact recipe."
)
CHAT_FAILURE_CLASSES = {
    "proxy_timeout",
    "upstream_error",
    "upstream_unreachable",
    "internal_error",
    "request_refused",
}
#: the modules that construct an ``interactions`` row today (CODE-INDEX at 6e532a8b,
#: ``InteractionRecord`` and its ``InteractionRow`` alias); only the chat writer sets
#: ``input_media``. A new writer changes the NULL boundaries the note publishes.
ROW_WRITER_MODULES = {
    "routes/audit.py",
    "routes/chat.py",
    "routes/refusals.py",
    "services/console_acl/_audit.py",
    "services/memory_audit.py",
    "services/scan_audit_consumer.py",
}


def _recipe(fields: dict[str, Any], names: list[str]) -> str:
    """The published recipe, written from the note (not imported from the service)."""
    payload = {name: fields.get(name) for name in names}
    if fields.get("input_media") is not None:
        payload["input_media"] = fields["input_media"]
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note() -> str:
    return NOTE.read_text(encoding="utf-8")


def _block(tag: str) -> str:
    found = re.search(rf"<!-- {tag} -->\n```\w*\n(.*?)\n```", _note(), re.S)
    assert found is not None, tag
    return found.group(1)


class TestApiFieldDescription:
    def test_null_means_no_image_recorded_with_its_boundaries(self) -> None:
        field = InteractionListItem.model_fields["input_media"]
        assert field.description == INPUT_MEDIA_DESCRIPTION
        assert field.default is None

    def test_content_hash_names_the_conditional_field(self) -> None:
        field = InteractionListItem.model_fields["content_hash"]
        assert field.description == CONTENT_HASH_DESCRIPTION

    def test_the_vendored_contract_and_its_snapshot_carry_the_field(self) -> None:
        for path in (VENDORED, SNAPSHOT):
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
            props = spec["components"]["schemas"]["InteractionListItem"]["properties"]
            assert props["input_media"]["description"] == INPUT_MEDIA_DESCRIPTION, path
            assert props["content_hash"]["description"] == CONTENT_HASH_DESCRIPTION


class TestIntegrityNote:
    def test_the_published_field_list_equals_the_hashed_fields(self) -> None:
        listed = re.findall(r"^\d+\. `(\w+)`$", _block("hash-fields"), re.M)
        assert listed == list(integrity._CONTENT_FIELDS)
        assert f"these {len(listed)} fields" in _note()

    def test_the_worked_example_follows_the_published_recipe(self) -> None:
        names = re.findall(r"^\d+\. `(\w+)`$", _block("hash-fields"), re.M)
        fields = json.loads(_block("example-row"))
        media = _block("example-input-media")
        assert json.loads(media)["count"] == 1
        without = _recipe(fields, names)
        with_media = _recipe({**fields, "input_media": media}, names)
        assert without != with_media
        assert _block("example-hash-without-image") == without
        assert _block("example-hash-with-image") == with_media
        assert integrity.content_hash(fields) == without
        assert integrity.content_hash({**fields, "input_media": media}) == with_media

    def test_states_null_the_refusal_class_and_the_rollback_effect(self) -> None:
        text = " ".join(_note().split())
        for needle in (
            "no image recorded",
            "`request_refused`",
            "only when it is not null",
            "written for the 16-field recipe",
            "rolled back",
            "never back-filled",
        ):
            assert needle in text, needle

    def test_the_guide_says_what_null_means(self) -> None:
        text = " ".join(GUIDE.read_text(encoding="utf-8").split())
        assert "no image recorded" in text
        assert "audit-record-integrity.md" in text


class TestClosedVocabularyAndWriters:
    def test_the_chat_failure_classes_are_the_five(self) -> None:
        from audittrace.routes import chat

        values = {v for k, v in vars(chat).items() if k.startswith("FAILURE_")}
        assert values == CHAT_FAILURE_CLASSES
        assert max(len(v) for v in values) <= 32, (
            "interactions.failure_class is String(32)"
        )
        listed = set(re.findall(r"`(\w+)`", _block("chat-failure-classes")))
        assert listed == CHAT_FAILURE_CLASSES

    def test_the_writers_of_interaction_rows_are_the_known_set(self) -> None:
        """Control (green before and after): alias-aware, so ``InteractionRow`` counts."""
        writers: set[str] = set()
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            local = {
                (alias.asname or alias.name)
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module == "audittrace.db.models"
                for alias in node.names
                if alias.name == "InteractionRecord"
            }
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in local
                for node in ast.walk(tree)
            ):
                writers.add(path.relative_to(SRC).as_posix())
        assert writers == ROW_WRITER_MODULES


class TestRoleIsBounded:
    """Gate r1 A6: ``role`` is caller-chosen text; the record keeps it only from a closed set."""

    def _role(self, role: object) -> object:
        from audittrace.media import digest_input_media

        url = "data:image/png;base64,QUJD"
        part = {"type": "image_url", "image_url": {"url": url}}
        record = digest_input_media([{"role": role, "content": [part]}])
        assert record is not None
        return json.loads(record.to_json())["items"][0]["role"]

    def test_an_openai_role_is_recorded_as_sent(self) -> None:
        for role in ("system", "developer", "user", "assistant", "tool", "function"):
            assert self._role(role) == role

    def test_anything_else_is_recorded_as_other(self) -> None:
        for role in ("x" * 5000, "User", "", None, 7, ["user"], "user\n"):
            assert self._role(role) == "other"
