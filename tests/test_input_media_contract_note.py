"""LIBRECHAT-IMAGE-SLICE ADDENDUM-2 AC-9 additions (gate r2 S1, S2, S4, A2) -- the published note.

* S1: the note's list of refusal reasons is an EQUALITY with the reasons the service can
  emit (the keys of the one message table every refusal body is built from, and every
  ``REASON_*`` constant of the media package), and each of them is observed through the route.
* S2: the note states residual R-1 (a short raw payload in a non-image part can appear in
  span attributes), with its bound and what stays clean. The bound is an upper bound derived
  from the code (every site that writes request messages to a span attribute) and pinned here
  against the spans a request really ends, for a typed part and for a part of an unknown type.
* S4: the note names the canonical serialisation of ``input_media``, and an image row's
  ``content_hash`` is recomputed here from nothing but the JSON of ``GET /interactions`` and
  the note's words.
* A2: the writers of ``interactions`` rows are the known set whatever the call form
  (``Name(...)`` under any import alias, or ``module.InteractionRecord(...)``).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import json
import pkgutil
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import conftest as net
from tests._image_slice import (
    ARM_IDS,
    ARMS,
    EXPECTED_RECORD,
    QUESTION,
    flip_mode,
    image_messages,
    ok_fake,
    post_chat,
)

ROOT = Path(__file__).resolve().parents[1]
NOTE = ROOT / "docs" / "reference" / "audittrace" / "audit-record-integrity.md"
SRC = ROOT / "src" / "audittrace"
REFUSAL_REASONS = {
    "image_url_not_supported",
    "image_data_invalid",
    "media_link_not_supported",
}
#: 72 bytes -> 96 base64 characters: under the 128-character run rule (residual R-1)
SHORT = base64.b64encode(bytes(range(72))).decode()
#: The attributes the note's section 4 counts (typed part: 5 of 3 spans; unknown type: 8 of 4).
#: ``sovereign-chat-request`` exists as an ended span only under a live Langfuse client, so a
#: request through the test client can carry at most the others; the subset check below is
#: therefore against the full set and the control asserts the carried ones are non-empty.
TYPED_BOUND = {
    "chat-merge-system :: output.value",
    "chat-merge-system :: langfuse.observation.output",
    "sovereign-chat-request :: input.value",
    "sovereign-chat-request :: langfuse.observation.input",
    "llm.chat.completions :: input.value",
}
UNKNOWN_BOUND = TYPED_BOUND | {
    "chat-merge-system :: input.value",
    "chat-merge-system :: langfuse.observation.input",
    "POST /v1/chat/completions :: langfuse.observation.input",
}
SERIALISATION = 'json.dumps(obj, sort_keys=True, separators=(",", ":"))'
#: ADDENDUM-1's set (CODE-INDEX, ``InteractionRecord`` and its alias), restated here so
#: this file stands alone; the equality below is what binds it.
ROW_WRITER_MODULES = {
    "routes/audit.py",
    "routes/chat.py",
    "routes/refusals.py",
    "services/console_acl/_audit.py",
    "services/memory_audit.py",
    "services/scan_audit_consumer.py",
}


def _note() -> str:
    return NOTE.read_text(encoding="utf-8")


def _flat() -> str:
    return " ".join(_note().split())


def _block(tag: str) -> str:
    found = re.search(rf"<!-- {tag} -->\n```\w*\n(.*?)\n```", _note(), re.S)
    assert found is not None, tag
    return found.group(1)


@pytest.fixture(params=ARMS, ids=ARM_IDS)
def arm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, bool]]:
    mode, stream = request.param
    for _ in flip_mode(monkeypatch, mode):
        yield mode, stream


class TestRefusalReasonsArePublished:
    def test_the_published_reasons_equal_the_reasons_the_service_emits(self) -> None:
        import audittrace.media as media
        from audittrace.media import refusal

        constants: set[str] = set()
        for info in pkgutil.iter_modules(media.__path__):
            module = importlib.import_module(f"audittrace.media.{info.name}")
            constants |= {
                value
                for name, value in vars(module).items()
                if name.startswith("REASON_") and isinstance(value, str)
            }
        listed = set(re.findall(r"`(\w+)`", _block("request-refused-reasons")))
        assert listed == REFUSAL_REASONS
        assert set(refusal.REFUSAL_MESSAGES) == listed
        assert constants == listed

    def test_every_published_reason_is_observed_through_the_route(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        link = "https://example.invalid/x"
        video = {"type": "input_video", "input_video": {"url": link}}
        requests = [
            image_messages("https://example.invalid/a.png"),
            image_messages("data:image/png;base64,@@@"),
            [{"role": "user", "content": [{"type": "text", "text": QUESTION}, video]}],
        ]
        seen: set[str] = set()
        for messages in requests:
            response = post_chat(
                client, ok_fake(mode, stream), stream=stream, messages=messages
            )
            assert response.status_code == 400
            seen.add(response.json()["error"]["code"])
        rows = client.get("/interactions?limit=10").json()["interactions"]
        assert {r["failure_class"] for r in rows} == {"request_refused"}
        assert {r["error_detail"].split(":")[0] for r in rows} == seen
        assert seen == set(re.findall(r"`(\w+)`", _block("request-refused-reasons")))


class TestResidualIsStated:
    def test_the_note_states_r1_with_its_bound_and_what_stays_clean(self) -> None:
        text = _flat()
        for needle in (
            "128 characters or fewer",
            "that is not an image",
            "raw base64",
            "can appear in span attributes",
            "at most 5 attributes of 3 spans",
            "at most 8 attributes of 4 spans",
            "up to four more model-call spans",
            "never in the audit record",
            "Images are not affected",
            "`url_sha256`",
            "can be matched by guessing",
        ):
            assert needle in text, needle

    def test_the_short_payload_is_in_no_audit_field(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        """What the note says stays clean, does; and R-1 is still real (else the note is stale)."""
        mode, stream = arm
        audio = {"type": "input_audio", "input_audio": {"data": SHORT, "format": "wav"}}
        messages = [
            {"role": "user", "content": [{"type": "text", "text": QUESTION}, audio]}
        ]
        response = post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=messages
        )
        assert response.status_code == 200
        item = client.get("/interactions?limit=5").json()["interactions"][0]
        assert item["input_media"] is None
        assert SHORT not in json.dumps(item)
        spans = [json.loads(s) for s in net._SPAN_END_SNAPSHOTS]
        carrying = sorted(
            {
                f"{s['name']} :: {k}"
                for s in spans
                for k, v in s["attributes"].items()
                if SHORT in str(v)
            }
        )
        assert carrying != [], "R-1 is closed: remove section 4 of the note"
        assert "POST /v1/chat/completions :: langfuse.observation.input" not in carrying
        assert set(carrying) <= TYPED_BOUND, sorted(set(carrying) - TYPED_BOUND)

    def test_a_part_of_an_unknown_type_stays_inside_its_stated_bound(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        """The note's second paragraph: no redaction by type, so the entry tag carries it too."""
        mode, stream = arm
        blob = {"type": "custom_blob", "data": SHORT}
        messages = [
            {"role": "user", "content": [{"type": "text", "text": QUESTION}, blob]}
        ]
        response = post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=messages
        )
        assert response.status_code == 200
        item = client.get("/interactions?limit=5").json()["interactions"][0]
        assert SHORT not in json.dumps(item), "the audit record is still clean"
        carrying = {
            f"{s['name']} :: {k}"
            for s in map(json.loads, net._SPAN_END_SNAPSHOTS)
            for k, v in s["attributes"].items()
            if SHORT in str(v)
        }
        assert carrying != set(), "control: an unknown part type is not redacted"
        assert carrying <= UNKNOWN_BOUND, sorted(carrying - UNKNOWN_BOUND)
        assert "POST /v1/chat/completions :: langfuse.observation.input" in carrying
        assert len(TYPED_BOUND) == 5 and len(UNKNOWN_BOUND) == 8


class TestHashFromTheApiResponse:
    def test_the_note_names_the_canonical_serialisation(self) -> None:
        assert SERIALISATION in _flat()
        assert "returns `input_media` as an object" in _flat()

    def test_an_image_row_hash_is_recomputed_from_get_interactions(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        """Only the API response and the note are read: no service code computes here."""
        mode, stream = arm
        post_chat(
            client, ok_fake(mode, stream), stream=stream, messages=image_messages()
        )
        item = client.get("/interactions?limit=5").json()["interactions"][0]
        assert item["input_media"] == EXPECTED_RECORD
        assert isinstance(item["content_hash"], str) and len(item["content_hash"]) == 64
        names = re.findall(r"^\d+\. `(\w+)`$", _block("hash-fields"), re.M)
        payload = {name: item[name] for name in names}
        as_note = dict(payload)
        as_note["input_media"] = json.dumps(
            item["input_media"], sort_keys=True, separators=(",", ":")
        )

        def digest(value: dict[str, Any]) -> str:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        assert digest(as_note) == item["content_hash"]
        # the two readings the note excludes do NOT reproduce the stored hash
        assert (
            digest({**payload, "input_media": item["input_media"]})
            != (item["content_hash"])
        )
        assert digest(payload) != item["content_hash"]

    def test_a_row_without_image_is_recomputed_from_the_sixteen_fields(
        self, client: Any, arm: tuple[str, bool]
    ) -> None:
        mode, stream = arm
        post_chat(
            client,
            ok_fake(mode, stream),
            stream=stream,
            messages=[{"role": "user", "content": "hello"}],
        )
        item = client.get("/interactions?limit=5").json()["interactions"][0]
        assert item["input_media"] is None
        names = re.findall(r"^\d+\. `(\w+)`$", _block("hash-fields"), re.M)
        text = json.dumps(
            {name: item[name] for name in names},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == item["content_hash"]


class TestWritersWhateverTheCallForm:
    def test_the_writers_are_the_known_set_under_names_and_attributes(self) -> None:
        """Gate r2 A2: ``m.InteractionRecord(...)`` is a writer too."""
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
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                by_name = isinstance(func, ast.Name) and func.id in local
                by_attribute = (
                    isinstance(func, ast.Attribute) and func.attr == "InteractionRecord"
                )
                if by_name or by_attribute:
                    writers.add(path.relative_to(SRC).as_posix())
        assert writers == ROW_WRITER_MODULES
