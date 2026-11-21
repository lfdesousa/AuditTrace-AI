"""LIBRECHAT-IMAGE-SLICE AC-1, AC-5 -- ``audittrace.media.digest_input_media`` (pure).

The digest must be the digest of the bytes the MODEL reads. The model server decodes
base64 with its own decoder (llama-server 360e1349f, ``base64_decode``), which stops at
the first character outside ``A-Za-z0-9+/``. ``UPSTREAM_VECTORS`` are the outputs of that
decoder, compiled from the upstream source: for every payload this module ACCEPTS, its
bytes equal the upstream's; every payload where a lenient decoder would disagree with
the upstream is REFUSED.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tests._image_slice import DATA_URL, EXPECTED_RECORD, PNG_B64, PNG_BYTES, PNG_SHA256

#: (base64 payload, sha256 of the upstream decoder's output, its byte count)
UPSTREAM_VECTORS: list[tuple[str, str, int]] = [
    (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==",
        "8cf8513dd0fb04730604640ba5a61a5277b0bb0f3f446b5047b7ad17438e0732",
        70,
    ),
    (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg",
        "8cf8513dd0fb04730604640ba5a61a5277b0bb0f3f446b5047b7ad17438e0732",
        70,
    ),
    ("QQ==", "559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd", 1),
    ("QQ", "559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd", 1),
    ("QUI=", "38164fbd17603d73f696b8b4d72664d735bb6a7c88577687fd2ae33fd6964153", 2),
    ("QUI", "38164fbd17603d73f696b8b4d72664d735bb6a7c88577687fd2ae33fd6964153", 2),
    ("QUJD", "b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78", 3),
    ("QR==", "559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd", 1),
    ("QUJ=", "38164fbd17603d73f696b8b4d72664d735bb6a7c88577687fd2ae33fd6964153", 2),
    ("+/+/", "337672c9cc7a511cf6fe0529536304247a5abc8584da9f2f1853c1cc74a61003", 3),
    ("AAAA", "709e80c88487a2411e1ee4dfb9f22a861492d20c4765150c0c794abd70f8147c", 3),
    ("AAECAwQ=", "08bb5e5d6eaac1049ede0893d30ed022b1a4d9b5b48db414871f51c9cb35283d", 5),
    ("AAECAwQF", "17e88db187afd62c16e5debf3e6527cd006bc012bc90b51a810cd80c2d511f43", 6),
    (
        "AAECAwQFBg==",
        "57355ac3303c148f11aef7cb179456b9232cde33a818dfda2c2fcb9325749a6b",
        7,
    ),
    (
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+Pw==",
        "fdeab9acf3710362bd2658cdc9a29e8f9c757fcf9811603a8c447cd1d9151108",
        64,
    ),
]
#: payloads the upstream decodes to something a lenient Python decode disagrees with
UPSTREAM_DIVERGENT: list[str] = [
    "QUJD QUJD",
    "QUJD\tQUJD",
    "QUJDQUJD!x",
    "QUJD=QUJD",
    "QUJD-_-_",
    "QUJDR",
    "=QUJD",
    "QUJD\r",
    "QUJD\nQUJD",
]


def _digest(messages: list[dict[str, Any]]) -> Any:
    from audittrace.media import digest_input_media

    return digest_input_media(messages)


def _one(url: str) -> dict[str, Any]:
    record = _digest(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ]
    )
    parsed: dict[str, Any] = json.loads(record.to_json())
    assert parsed["count"] == 1
    item: dict[str, Any] = parsed["items"][0]
    return item


class TestRecord:
    def test_reference_image(self) -> None:
        record = _digest(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "q"},
                        {"type": "image_url", "image_url": {"url": DATA_URL}},
                    ],
                }
            ]
        )
        assert json.loads(record.to_json()) == EXPECTED_RECORD
        assert record.to_json() == json.dumps(
            EXPECTED_RECORD, sort_keys=True, separators=(",", ":")
        )
        assert record.refused == ()

    @pytest.mark.parametrize(
        "messages",
        [
            [],
            [{"role": "user", "content": "plain"}],
            [{"role": "user", "content": [{"type": "text", "text": "t"}]}],
            [{"role": "user", "content": None}, "not a dict", {"role": "tool"}],
            [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}],
        ],
    )
    def test_no_image_part_means_none(self, messages: list[Any]) -> None:
        assert _digest(messages) is None

    def test_the_record_never_contains_the_payload(self) -> None:
        text = _digest(
            [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": DATA_URL}}],
                }
            ]
        ).to_json()
        assert PNG_B64[:24] not in text and "base64" not in text

    def test_declared_type_is_recorded_as_declared(self) -> None:
        item = _one("data:image/jpeg;base64," + PNG_B64)
        assert (item["media_type"], item["sha256"], item["bytes"]) == (
            "image/jpeg",
            PNG_SHA256,
            PNG_BYTES,
        )


class TestBytesEqualWhatTheModelServerDecodes:
    @pytest.mark.parametrize("payload,sha256,size", UPSTREAM_VECTORS)
    def test_accepted_payload_matches_the_upstream_decoder(
        self, payload: str, sha256: str, size: int
    ) -> None:
        item = _one("data:image/png;base64," + payload)
        assert (item["status"], item["sha256"], item["bytes"]) == (
            "recorded",
            sha256,
            size,
        )

    @pytest.mark.parametrize("payload", UPSTREAM_DIVERGENT)
    def test_divergent_payload_is_refused_never_hashed(self, payload: str) -> None:
        url = "data:image/png;base64," + payload
        item = _one(url)
        assert item["status"] == "refused"
        assert item["reason"] == "image_data_invalid"
        assert "sha256" not in item and "bytes" not in item
        assert item["url_sha256"] == hashlib.sha256(url.encode()).hexdigest()
