"""Logging guard for the tools non-stream image-input arm (review r1 finding F1).

``tests/test_chat_image_input_unsupported_error.py`` captures at WARNING only, so an
added ``logger.info`` / ``logger.error`` carrying the upstream text on this arm stayed
GREEN. Here the capture is at DEBUG: exactly ONE audittrace record may carry the
upstream text (the retry helper's WARNING), and ``_upstream_errors`` itself emits
nothing on this arm (``non_stream_upstream_error`` logs nothing by contract).

Falsifiability: neuter R2a (``logger.info(text)`` in the image branch) and R2b
(``logger.error(text)``) each turn the first test, and the second, RED.
"""

from __future__ import annotations

import logging

import pytest

from tests.test_chat_image_input_unsupported_error import (  # noqa: F401
    LLAMA_IMAGE_500_BODY,
    UPSTREAM_MESSAGE,
    _run,
    tools_mode,
)

_MODULE_LOGGER = "audittrace.routes._upstream_errors"


@pytest.mark.usefixtures("tools_mode")
class TestToolsNonStreamImageArmLogging:
    async def test_exactly_one_audittrace_record_carries_the_upstream_text(  # type: ignore[no-untyped-def]
        self, client, caplog
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            response, _ = _run(client, "tools", False, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 400
        hits = [
            r
            for r in caplog.records
            if r.name.startswith("audittrace") and UPSTREAM_MESSAGE in r.getMessage()
        ]
        assert len(hits) == 1, [(r.name, r.levelname) for r in hits]
        assert hits[0].levelno == logging.WARNING

    async def test_the_error_module_emits_nothing_on_this_arm(  # type: ignore[no-untyped-def]
        self, client, caplog
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            response, _ = _run(client, "tools", False, 500, LLAMA_IMAGE_500_BODY)
        assert response.status_code == 400
        assert [r for r in caplog.records if r.name == _MODULE_LOGGER] == []
