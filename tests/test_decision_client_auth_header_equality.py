"""LC-5 / M3-WU-4 -- read-only equality pin between the decision client and the
shared llama auth helper.

``services/decision/**`` is owned by WU-459-2 (a separate endpoint with its own
key and its own ``decision_api_key`` setting) and is NOT edited by LC-5. The
client builds its ``Authorization`` header inline at construction; this test
pins that header to ``llama_auth_headers`` so the two cannot drift apart in
shape (``Bearer <key>``). Neuter the helper -> RED.
"""

from __future__ import annotations

from audittrace.routes._llama_client import llama_auth_headers
from audittrace.services.decision.client import LlamaCppDecisionClient
from tests.fakes.decision_server import FakeLlama, make_settings


async def test_decision_client_header_equals_the_shared_helper() -> None:
    key = "decision-key-lc5"
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(decision_api_key=key), transport=fake.transport
    ) as client:
        sent = client._http.headers["Authorization"]
    assert sent == llama_auth_headers(key)["Authorization"]
    assert sent == f"Bearer {key}"


async def test_decision_client_sends_no_header_when_no_key_and_helper_agrees() -> None:
    fake = FakeLlama()
    async with LlamaCppDecisionClient(
        make_settings(decision_api_key=""), transport=fake.transport
    ) as client:
        assert "Authorization" not in client._http.headers
    assert llama_auth_headers("") == {}
