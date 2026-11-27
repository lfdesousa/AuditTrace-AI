"""#61 WU-2 (live finding, rc2): the two shipped procedural requests of the repair.

``04b`` is the plan's leg "a PUT over a corpus skill is refused (409), nothing is
written" (the shipped ``04-update`` asserts a 200 and so cannot run it);
``05b`` removes the caller's own orphan private object with the admin hard
delete. Each pin ties a request to the exact text or shape the route gives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_bruno_chat_image_part import _code_lines, _env_vars, _seq
from tests.test_memory_put_without_manifest_row import CORPUS_REFUSAL

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
PROCEDURAL = COLLECTION / "memory" / "procedural"
REFUSE = PROCEDURAL / "04b-update-corpus-skill-is-refused.bru"
REPAIR = PROCEDURAL / "05b-delete-orphan-hard.bru"
ENV_FILES = [COLLECTION / "environments" / f"{n}.bru" for n in ("cluster", "local")]


def test_there_is_something_to_check() -> None:
    assert REFUSE.is_file() and REPAIR.is_file()
    assert all(p.is_file() for p in ENV_FILES)


def test_the_two_requests_have_their_own_seq() -> None:
    seqs = [_seq(REFUSE), _seq(REPAIR)]
    others = [_seq(p) for p in PROCEDURAL.glob("*.bru") if p not in (REFUSE, REPAIR)]
    assert seqs == [7, 8]
    assert not set(seqs) & set(others)


def test_the_refusal_request_is_a_put_that_expects_exactly_409_and_the_route_text() -> (
    None
):
    text = REFUSE.read_text()
    assert _code_lines(text, "put")[0] == (
        "url: {{baseUrl}}/memory/procedural/{{exampleSkillFile}}"
    )
    lines = _code_lines(text, "tests")
    assert "expect(res.getStatus()).to.equal(409);" in lines
    (asserted,) = re.findall(r'"(Not supported: [^"]*)"', "\n".join(lines))
    assert asserted in CORPUS_REFUSAL.format(key="SKILL-x.md", layer="procedural")


def test_the_repair_request_is_the_hard_delete_of_the_named_skill() -> None:
    text = REPAIR.read_text()
    assert _code_lines(text, "delete") == [
        "url: {{baseUrl}}/memory/procedural/{{exampleSkillFile}}?hard=true",
        "body: none",
        "auth: bearer",
    ]
    assert _code_lines(text, "query") == ["hard: true"]


def test_the_repair_request_asserts_the_exact_status_and_the_route_shape() -> None:
    lines = _code_lines(REPAIR.read_text(), "tests")
    for expected in (
        "expect(res.getStatus()).to.equal(200);",
        "expect(e.orphan).to.equal(true);",
        "expect(e.deleted).to.equal(true);",
        'expect(e.tier).to.equal("private");',
        'expect(e.layer).to.equal("procedural");',
        'expect(e.key).to.equal(bru.getEnvVar("exampleSkillFile"));',
    ):
        assert expected in lines


def test_the_repair_login_line_is_the_write_scope_alone() -> None:
    """The owner removes their own orphan: the Login line names the layer's write
    scope and not the admin scope; the admin scope is named only for a row that exists."""
    docs = "\n".join(_code_lines(REPAIR.read_text(), "docs"))
    login = [ln for ln in docs.splitlines() if ln.startswith("Login:")]
    assert login == [
        "Login: `memory:procedural:write` (the default login has it). Without it the answer is 403"
    ]
    assert "no admin is needed" in docs
    assert (
        "needs `audittrace:admin` (403 otherwise)" in docs
        and docs.count("audittrace:admin") == 1
    )


@pytest.mark.parametrize("env", ENV_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("request_path", [REFUSE, REPAIR], ids=lambda p: p.name)
def test_every_variable_of_the_request_is_defined(
    request_path: Path, env: Path
) -> None:
    used = set(re.findall(r"\{\{(\w+)\}\}", request_path.read_text()))
    assert not used - set(_env_vars(env)) - {"accessToken"}
