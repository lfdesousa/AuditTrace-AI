"""CONSOLE-AUDIT-EVIDENCE WU-A (#480 live plan): the evidence routes as a second user
and as the restricted client, from shipped ``identities/`` requests.

Neuters (each alone): change a URL to another route -> the URL cell RED; swap a bearer
slot (B <-> A) -> the token cell RED; widen a status to a second value or drop the exact
body -> the assert cell RED; drop the control's comparison with request 10 -> the
comparison cell RED; remove ``unknownMessageId`` from an environment file -> that env's
cell RED.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parent.parent / "bruno" / "audittrace"
IDENT = COLLECTION / "identities"
ENVS = ("cluster", "cluster-session-e2e", "local")

NOT_FOUND = '    expect(res.getBody()).to.deep.equal({ detail: "Not Found" });'
FORBIDDEN = '    expect(res.getBody()).to.deep.equal({ detail: "Required scope: audittrace:audit" });'
# file -> (url, token slot, status, exact body line)
REQUESTS = {
    "10-userb-evidence-by-usera-message.bru": (
        "{{baseUrl}}/evidence/messages/{{clientMessageId}}",
        "tokenUserB",
        404,
        NOT_FOUND,
    ),
    "11-userb-evidence-unknown-message-control.bru": (
        "{{baseUrl}}/evidence/messages/{{unknownMessageId}}",
        "tokenUserB",
        404,
        NOT_FOUND,
    ),
    "12-userb-evidence-by-usera-interaction.bru": (
        "{{baseUrl}}/evidence/interactions/{{evidenceInteractionId}}",
        "tokenUserB",
        404,
        NOT_FOUND,
    ),
    "13-restricted-evidence-by-message.bru": (
        "{{baseUrl}}/evidence/messages/{{clientMessageId}}",
        "tokenUserA",
        403,
        FORBIDDEN,
    ),
    "14-restricted-evidence-by-interaction.bru": (
        "{{baseUrl}}/evidence/interactions/{{evidenceInteractionId}}",
        "tokenUserA",
        403,
        FORBIDDEN,
    ),
}


def _text(name: str) -> str:
    return (IDENT / name).read_text()


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_request_url_token_exact_status_and_exact_body(name: str) -> None:
    url, token, status, body_line = REQUESTS[name]
    text = _text(name)
    assert f"  url: {url}\n" in text
    assert f"auth:bearer {{\n  token: {{{{{token}}}}}\n}}" in text
    assert text.count(f"expect(res.getStatus()).to.equal({status});") == 1
    assert body_line in text
    # house rule: never an either-or status
    assert not re.search(r"oneOf\(\s*\[\s*40[34]", text)


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_docs_name_the_login_and_the_restore(name: str) -> None:
    docs = _text(name).split("\ndocs {", 1)[1]
    slot = REQUESTS[name][1]
    assert f"--env-key {slot}" in docs
    assert "scripts/audittrace-login" in docs
    assert "Restore the default" in docs
    if slot == "tokenUserA":
        assert "CLIENT_ID=audittrace-restricted" in docs
    else:
        assert "CLIENT_ID" in docs and "unset" in docs


def test_request_10_stores_what_the_control_compares() -> None:
    first, control = (_text(n) for n in sorted(REQUESTS)[:2])
    assert 'bru.setVar("evidenceNotYoursBody", JSON.stringify(res.getBody()));' in first
    assert 'bru.setVar("evidenceNotYoursStatus", res.getStatus());' in first
    assert 'bru.getVar("evidenceNotYoursBody")' in control
    assert 'bru.getVar("evidenceNotYoursStatus")' in control
    assert "expect(JSON.stringify(res.getBody())).to.equal(body);" in control
    assert "expect(String(res.status)).to.equal(String(status));" in control
    assert "throw new Error(" in control  # fail closed when 10 did not run first


@pytest.mark.parametrize("env", ENVS)
def test_every_environment_defaults_the_unknown_id_to_a_placeholder(env: str) -> None:
    text = (COLLECTION / "environments" / f"{env}.bru").read_text()
    vars_block = text.split("vars:secret", 1)[0]
    assert (
        "  unknownMessageId: replace-with-a-random-unknown-message-id\n" in vars_block
    )
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", text)
