"""Input media of a chat request: digests for the audit row, never the bytes.

This package classifies and redacts; it writes no row and calls no upstream.
"""

from audittrace.media.digest import digest_input_media
from audittrace.media.image_part_record import ImagePartRecord
from audittrace.media.input_media_record import InputMediaRecord
from audittrace.media.redaction import redact_media_payloads
from audittrace.media.refusal import classify_refusal
from audittrace.media.request_refusal import RequestRefusal

__all__ = [
    "ImagePartRecord",
    "InputMediaRecord",
    "RequestRefusal",
    "classify_refusal",
    "digest_input_media",
    "redact_media_payloads",
]
