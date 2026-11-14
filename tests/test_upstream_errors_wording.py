"""MODEL-MMPROJ-PERMANENT -- the explicit unsupported-content error says only what is true.

The chat llama-server now runs with a vision projector, so ``image_url`` parts are
accepted. The message must not say the model cannot receive images. It states what
the proxy knows: the model server rejected one of the content part types of the
message, and which non-text part types the message carried. The OpenAI error
envelope (``type``, ``param``, ``code``, extension ``status``) does not move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from audittrace.routes import _upstream_errors

GUIDE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "guides"
    / "librechat-attachments-support.md"
)

LEAD = "The model server does not accept one of the content part types in this message"
HINT = "To attach a document, use 'Upload as Text' instead."
MSG_FILE = f"{LEAD} (non-text part types sent: 'file'). {HINT}"
MSG_FILE_AND_IMAGE = f"{LEAD} (non-text part types sent: 'file', 'image_url'). {HINT}"
MSG_NO_READABLE_TYPE = f"{LEAD}. {HINT}"


def _payload(*types: object) -> dict[str, Any]:
    parts: list[dict[str, object]] = [{"type": "text", "text": "hi"}]
    parts += [{"type": t} for t in types]
    return {"messages": [{"role": "user", "content": parts}]}


def _message(*types: object) -> str:
    message = _upstream_errors.unsupported_content_error_body(_payload(*types))[
        "error"
    ]["message"]
    assert isinstance(message, str)
    return message


class TestWordingIsTrueWithOrWithoutAProjector:
    def test_one_type(self) -> None:
        assert _message("file") == MSG_FILE

    def test_two_types_lists_what_was_sent(self) -> None:
        assert _message("file", "image_url") == MSG_FILE_AND_IMAGE

    def test_no_readable_type_has_no_parenthetical(self) -> None:
        assert _message({"a": 1}) == MSG_NO_READABLE_TYPE

    @pytest.mark.parametrize(
        "types", [("file",), ("file", "image_url"), ("image_url", "file"), ()]
    )
    def test_never_claims_images_are_unsupported(self, types: tuple[str, ...]) -> None:
        message = _message(*types)
        assert "image" not in message.replace("'image_url'", "")
        assert "unsupported content part type" not in message

    def test_the_whole_body_is_pinned_envelope_included(self) -> None:
        """PIN on type / param / code / status (green before and after); the
        equality on ``message`` is what is red before the change."""
        assert _upstream_errors.unsupported_content_error_body(_payload("file")) == {
            "error": {
                "message": MSG_FILE,
                "type": "invalid_request_error",
                "param": "messages",
                "code": "unsupported_content_type",
                "status": 400,
            }
        }


class TestEnvelopeDoesNotMove:
    """PIN (green before and after): ADR-024, the envelope is not this change."""

    def test_envelope_fields(self) -> None:
        err = _upstream_errors.unsupported_content_error_body(_payload("file"))["error"]
        assert {k: v for k, v in err.items() if k != "message"} == {
            "type": "invalid_request_error",
            "param": "messages",
            "code": "unsupported_content_type",
            "status": 400,
        }

    def test_the_match_still_needs_the_exact_upstream_message(self) -> None:
        image_500 = (
            '{"error":{"code":500,"message":"image input is not supported - hint: '
            'if this is unexpected, you may need to provide the mmproj",'
            '"type":"server_error"}}'
        )
        assert _upstream_errors.match_unsupported_content(500, image_500) is False
        assert _upstream_errors.match_unsupported_content(400, image_500) is False


class TestPublicGuideMatchesTheCode:
    def test_guide_quotes_the_message_the_code_builds(self) -> None:
        assert _message("file") in GUIDE.read_text()

    def test_guide_no_longer_says_files_or_images(self) -> None:
        assert "files or images" not in GUIDE.read_text()

    def test_guide_says_image_parts_are_accepted(self) -> None:
        row = next(
            line
            for line in GUIDE.read_text().splitlines()
            if line.startswith("| Images (`image_url` parts)")
        )
        assert "accepted" in row and "unchanged" not in row
