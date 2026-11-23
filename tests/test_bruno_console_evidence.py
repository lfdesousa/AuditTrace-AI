"""CONSOLE-AUDIT-EVIDENCE WU-A: the three shipped Bruno requests and their env defaults.

The live evidence of the slice is run from these shipped requests (never a throwaway
``.bru``): ``audit/06`` (by message id), ``audit/07`` (by interaction id), ``audit/08``
(list filter). Neuters (each alone): delete a request -> the existence cell RED; remove a
default from one environment file -> that env's cell RED; point a URL at another route ->
the URL cell RED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parent.parent / "bruno" / "audittrace"
ENVS = ("cluster", "cluster-session-e2e", "local")
REQUESTS = {
    "06-evidence-by-message.bru": "{{baseUrl}}/evidence/messages/{{clientMessageId}}",
    "07-evidence-by-interaction.bru": (
        "{{baseUrl}}/evidence/interactions/{{evidenceInteractionId}}"
    ),
    "08-list-by-client-message-id.bru": (
        "{{baseUrl}}/interactions?client_message_id={{clientMessageId}}"
    ),
}


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_each_request_is_a_get_on_its_route_with_the_bearer_bridge(name: str) -> None:
    text = (COLLECTION / "audit" / name).read_text()
    assert f"url: {REQUESTS[name]}" in text
    assert "token: {{accessToken}}" in text and "auth: bearer" in text
    assert "getEnvVar" in text  # tests read the --env-var form, not a runtime var


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("var", ["clientMessageId", "evidenceInteractionId"])
def test_every_environment_defaults_the_variables(env: str, var: str) -> None:
    text = (COLLECTION / "environments" / f"{env}.bru").read_text()
    vars_block = text.split("vars:secret", 1)[0]
    assert f"  {var}: " in vars_block
