"""LIBRECHAT-IMAGE-SLICE builder's unit pins for ``audittrace.media`` (pure functions).

Each guard here has a neuter that turns it red (see the build record): the data-URL scrub
covers EVERY type (not only ``image/``) and a truncated payload; the run rule drops a
base64 run above 128 characters and keeps a SHA-512 hex digest; the span attributes count
refused parts; the placeholder never carries a payload.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from audittrace.media import InputMediaRecord, digest_input_media
from audittrace.media.payload_scrub import (
    BASE64_RUN_MIN,
    MEDIA_MARK,
    scrub_base64_runs,
    scrub_data_url_payloads,
    scrub_strings,
)

PNG = "data:image/png;base64,iVBORw0KGgo="


class TestDataUrlScrub:
    @pytest.mark.parametrize(
        "kind", ["image/png", "application/pdf", "audio/wav", "application/x-blob"]
    )
    def test_the_payload_of_every_type_is_dropped_even_when_short(
        self, kind: str
    ) -> None:
        out = scrub_data_url_payloads(f"see data:{kind};base64,QUJDRA== end")
        assert out == f"see data:{kind};base64,{MEDIA_MARK} end"

    def test_a_truncated_payload_is_dropped_too(self) -> None:
        assert scrub_data_url_payloads("data:image/png;base64,QUJD").endswith(
            MEDIA_MARK
        )

    def test_text_without_a_marker_is_returned_unchanged(self) -> None:
        text = "no payload here, only data:text/plain,hello"
        assert scrub_data_url_payloads(text) is text


class TestBase64RunScrub:
    def test_a_run_above_the_threshold_is_dropped(self) -> None:
        run = "A" * (BASE64_RUN_MIN + 1)
        assert scrub_base64_runs(f"x {run} y") == f"x {MEDIA_MARK} y"

    def test_a_sha512_hex_digest_survives(self) -> None:
        digest = hashlib.sha512(b"x").hexdigest()
        assert len(digest) == BASE64_RUN_MIN
        assert scrub_base64_runs(f"sha={digest}") == f"sha={digest}"

    def test_a_short_string_is_returned_unchanged(self) -> None:
        assert scrub_base64_runs("QUJD") == "QUJD"


def test_scrub_strings_reaches_every_leaf_and_copies() -> None:
    value = {"a": [PNG, {"b": "ok"}], "n": 3}
    out = scrub_strings(value, scrub_data_url_payloads)
    assert out == {"a": [f"data:image/png;base64,{MEDIA_MARK}", {"b": "ok"}], "n": 3}
    assert value["a"][0] == PNG, "the input is not modified"


class TestRecordAttributes:
    def _record(self) -> InputMediaRecord:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": PNG}},
                    {"type": "image_url", "image_url": {"url": "https://x.invalid/a"}},
                ],
            }
        ]
        record = digest_input_media(messages)
        assert record is not None
        return record

    def test_span_attributes_count_refused_parts_and_sum_recorded_bytes(self) -> None:
        record = self._record()
        attrs = record.span_attributes()
        item = record.items[0]
        assert attrs == {
            "audittrace.input.image.count": 2,
            "audittrace.input.image.refused": 1,
            "audittrace.input.image.bytes_total": item.size,
            "audittrace.input.image.sha256": [item.sha256],
        }

    def test_a_refused_item_is_identified_by_the_url_digest_never_the_url(self) -> None:
        item = self._record().refused[0]
        assert item.placeholder() == (
            f"[image refused: image_url_not_supported url_sha256={item.url_sha256}]"
        )
        stored = json.loads(self._record().to_json())["items"][1]
        assert stored["url_chars"] == len("https://x.invalid/a")
        assert "x.invalid" not in json.dumps(stored)
