"""Inject-path twin of T5: the request reaches llama-server unmodified (ADR-024).

Review r1 F2: the non-text ``file`` part must arrive at the upstream stub
byte-for-byte on the inject path, streaming and not, so a mutation that strips
or rewrites parts after augmentation reddens here directly.
"""

from __future__ import annotations

from tests.test_chat_proxy import _patch_async_client
from tests.test_chat_unsupported_content_error import (
    LLAMA_UNSUPPORTED_BODY,
    UPLOAD_TO_PROVIDER_MESSAGES,
    _post,
    _Upstream400Client,
    inject_mode,  # noqa: F401 - fixture re-exported
)


def _user_turn(messages: list[dict]) -> dict:
    return [m for m in messages if m.get("role") == "user"][-1]


class TestInjectRequestForwardedUnmodified:
    async def test_inject_stream_file_part_reaches_upstream(
        self,
        client,
        inject_mode,  # noqa: F811
    ):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=True)
        sent = _user_turn(fake.stream_calls[0]["json"]["messages"])
        assert sent["content"] == UPLOAD_TO_PROVIDER_MESSAGES[0]["content"]

    async def test_inject_non_stream_file_part_reaches_upstream(
        self,
        client,
        inject_mode,  # noqa: F811
    ):
        fake = _Upstream400Client(LLAMA_UNSUPPORTED_BODY)
        with _patch_async_client(fake):
            _post(client, UPLOAD_TO_PROVIDER_MESSAGES, stream=False)
        sent = _user_turn(fake.post_calls[0]["json"]["messages"])
        assert sent["content"] == UPLOAD_TO_PROVIDER_MESSAGES[0]["content"]
