"""LIBRECHAT-IMAGE-SLICE AC-2 -- the row hash covers ``input_media`` and no old hash moves.

``HASH_BEFORE`` is ``integrity.content_hash`` of ``FIELDS`` computed on main BEFORE this
change: every row written so far, and every future row without an image, must keep
hashing to exactly that (a recomputation over the whole table must not turn red).
"""

from __future__ import annotations

from audittrace.integrity import content_hash, verify_content_hash

FIELDS: dict[str, object] = {
    "project": "default",
    "source": "librechat",
    "question": "What do we have on this image please ?",
    "answer": "A settings page.",
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "timestamp": "2026-11-14T11:58:35",
    "session_id": "s-1",
    "model": "audittrace-default",
    "user_id": "u-1",
    "status": "success",
    "failure_class": None,
    "error_detail": None,
    "duration_ms": 1200,
    "trace_id": "5c04872063658f34558efaaeb3642a98",
    "event_class": None,
}
HASH_BEFORE = "7ae04559cfd800f8fd9b4e05ed407c09b9f97e02a9f5b761724d38006bb5c793"
MEDIA = '{"count":1,"items":[],"schema":1}'


class _Row:
    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def test_a_row_without_images_hashes_exactly_as_before() -> None:
    assert content_hash(FIELDS) == HASH_BEFORE
    assert content_hash({**FIELDS, "input_media": None}) == HASH_BEFORE


def test_an_old_row_without_the_attribute_still_verifies() -> None:
    assert verify_content_hash(_Row(**FIELDS, content_hash=HASH_BEFORE)) is True


def test_the_image_record_changes_the_hash() -> None:
    with_media = content_hash({**FIELDS, "input_media": MEDIA})
    assert with_media != HASH_BEFORE
    assert content_hash({**FIELDS, "input_media": MEDIA + " "}) != with_media


def test_a_tampered_image_record_fails_verification() -> None:
    stored = content_hash({**FIELDS, "input_media": MEDIA})
    assert verify_content_hash(_Row(**FIELDS, input_media=MEDIA, content_hash=stored))
    tampered = MEDIA.replace('"count":1', '"count":0')
    assert not verify_content_hash(
        _Row(**FIELDS, input_media=tampered, content_hash=stored)
    )
    assert not verify_content_hash(
        _Row(**FIELDS, input_media=None, content_hash=stored)
    )
