"""Structural guards for the LC-9 WP4 Bruno collection changes.

No live Bruno run (the live evidence is a later work package). These tests PARSE
the requests (blocks, comment-stripped code lines, ``seq``, bearer token) instead
of looking for a substring, so a status set spelled another way, a flipped
negation, a swapped token or a re-ordered request each turn a test RED:

* every ``{{var}}`` used by the paged lists, the identity requests and the MCP
  read request is defined in BOTH environments, except the per-run runtime
  variable ``mcpSessionId`` (set by the request's pre-request script and NOT
  defined in an environment, so a fixed default cannot come back);
* each identity request uses exactly the sanctioned token for its role;
* each identity request asserts exactly one status, the expected one;
* the assertion lines of 07, 08 and 08b are pinned exactly (count one, in code,
  not in a comment);
* the order of the identity requests follows the file order (08b after 08);
* the MCP read request sends ``X-Session-Id`` from ``mcpSessionId``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
IDENT = COLLECTION / "identities"
ENVS = ("cluster", "local")
IDENTITIES = sorted(IDENT.glob("*.bru"))
PAGED_LISTS = [
    COLLECTION / "memory" / "episodic" / "01-list.bru",
    COLLECTION / "memory" / "procedural" / "01-list.bru",
    COLLECTION / "memory" / "semantic" / "01-list.bru",
    COLLECTION / "memory" / "semantic" / "01b-list-paged.bru",
    IDENT / "03-usera-semantic-list.bru",
    IDENT / "06-userb-semantic-list.bru",
    IDENT / "09-operator-audit-list.bru",
]
MCP_READ = COLLECTION / "mcp" / "02-tools-call-read.bru"
RUNTIME_VARS = {"mcpSessionId"}

# role -> bearer token variable, per identity request
TOKEN_BY_FILE = {
    "01-usera-semantic-create.bru": "tokenUserA",
    "02-usera-semantic-read.bru": "tokenUserA",
    "03-usera-semantic-list.bru": "tokenUserA",
    "04-userb-semantic-create.bru": "tokenUserB",
    "05-userb-semantic-read.bru": "tokenUserB",
    "06-userb-semantic-list.bru": "tokenUserB",
    "07-userb-read-usera-text-check.bru": "tokenUserB",
    "08-curator-update-usera-key.bru": "accessToken",
    "08b-usera-read-after-curator-attempt.bru": "tokenUserA",
    "09-operator-audit-list.bru": "accessToken",
}
# expected single status per identity request (the curator write answers 404)
STATUS_BY_FILE = {name: 200 for name in TOKEN_BY_FILE}
STATUS_BY_FILE["08-curator-update-usera-key.bru"] = 404


def _block(text: str, name: str) -> str:
    """Return the inner text of the top-level ``name { ... }`` block ('' if absent)."""
    m = re.search(rf"^{re.escape(name)} \{{\n", text, re.M)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _code_lines(path: Path, block: str = "tests") -> list[str]:
    inner = _strip_comments(_block(path.read_text(), block))
    return [ln.strip() for ln in inner.splitlines() if ln.strip()]


def _token(path: Path) -> str:
    m = re.search(
        r"^auth:bearer \{\n  token: \{\{(\w+)\}\}\n\}", path.read_text(), re.M
    )
    assert m, f"{path.name}: no bearer token block"
    return m.group(1)


def _seq(path: Path) -> int:
    m = re.search(r"^  seq: (\d+)$", _block(path.read_text(), "meta"), re.M)
    assert m, f"{path.name}: no seq"
    return int(m.group(1))


def _env_names(env: str) -> set[str]:
    text = (COLLECTION / "environments" / f"{env}.bru").read_text()
    vars_block = re.search(r"^vars \{\n(.*?)^\}", text, re.S | re.M)
    secret_block = re.search(r"^vars:secret \[\n(.*?)^\]", text, re.S | re.M)
    assert vars_block and secret_block, f"{env}: vars blocks missing"
    names = set(re.findall(r"^  (\w+):", vars_block.group(1), re.M))
    names |= set(re.findall(r"^  (\w+),?$", secret_block.group(1), re.M))
    return names


def _used(path: Path) -> set[str]:
    return set(re.findall(r"\{\{(\w+)\}\}", path.read_text()))


def test_identity_requests_are_the_expected_set() -> None:
    assert [p.name for p in IDENTITIES] == sorted(TOKEN_BY_FILE)


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize(
    "path", [*IDENTITIES, *PAGED_LISTS, MCP_READ], ids=lambda p: p.name
)
def test_every_variable_is_defined_in_both_environments(path: Path, env: str) -> None:
    missing = _used(path) - _env_names(env) - RUNTIME_VARS
    assert not missing, f"{path.name}: undefined in {env}: {sorted(missing)}"


@pytest.mark.parametrize("env", ENVS)
def test_session_id_is_a_runtime_variable_not_an_environment_default(env: str) -> None:
    assert "mcpSessionId" not in _env_names(env)


@pytest.mark.parametrize("path", PAGED_LISTS, ids=lambda p: p.name)
def test_paged_lists_pass_limit_and_offset(path: Path) -> None:
    text = path.read_text()
    assert "limit={{listLimit}}" in text
    assert "offset={{listOffset}}" in text
    assert "  limit: {{listLimit}}" in text
    assert "  offset: {{listOffset}}" in text


def test_mcp_read_sends_session_header_from_a_per_run_variable() -> None:
    text = MCP_READ.read_text()
    assert _block(text, "headers").strip() == "X-Session-Id: {{mcpSessionId}}"
    script = _strip_comments(_block(text, "script:pre-request"))
    assert 'bru.setVar(\n      "mcpSessionId"' in script
    assert (
        'if (!bru.getEnvVar("mcpSessionId") && !bru.getVar("mcpSessionId"))' in script
    )
    assert "Math.random()" in script and "Date.now()" in script


@pytest.mark.parametrize("path", IDENTITIES, ids=lambda p: p.name)
def test_each_identity_request_uses_its_own_token(path: Path) -> None:
    assert _token(path) == TOKEN_BY_FILE[path.name]
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", path.read_text())


def test_curator_request_uses_the_operator_token_not_the_owner() -> None:
    assert _token(IDENT / "08-curator-update-usera-key.bru") == "accessToken"


@pytest.mark.parametrize("path", IDENTITIES, ids=lambda p: p.name)
def test_each_identity_request_asserts_exactly_one_status(path: Path) -> None:
    code = _code_lines(path)
    status = [ln for ln in code if "getStatus" in ln]
    assert status == [f"expect(res.getStatus()).to.equal({STATUS_BY_FILE[path.name]});"]


def _expect_lines(path: Path, lines: list[str]) -> None:
    code = _code_lines(path)
    for line in lines:
        assert code.count(line) == 1, f"{path.name}: expected exactly once: {line}"


def test_user_b_read_pins_b_text_present_and_a_text_absent() -> None:
    _expect_lines(
        IDENT / "07-userb-read-usera-text-check.bru",
        [
            'expect(content).to.contain("Identity check written by user B.");',
            'expect(content).to.not.contain("written by user A");',
        ],
    )


def test_curator_request_pins_configuration_check_and_body_check() -> None:
    path = IDENT / "08-curator-update-usera-key.bru"
    _expect_lines(
        path,
        [
            'if (!v || v.startsWith("replace-with-")) {',
            'throw new Error(name + " is empty or still a placeholder: set it before this run");',
            'expect(raw).to.not.contain("Identity check written by user A");',
            "expect(raw).to.not.contain(v);",
        ],
    )
    loops = [ln for ln in _code_lines(path) if ln.startswith("for (const name of")]
    assert (
        loops == ['for (const name of ["identityUserASub", "identityUserAName"]) {'] * 2
    )


def test_owner_read_after_curator_attempt_pins_original_text_and_absent_edit() -> None:
    _expect_lines(
        IDENT / "08b-usera-read-after-curator-attempt.bru",
        [
            'expect(content).to.contain("Identity check written by user A.");',
            'expect(content).to.not.contain("Curator write attempt");',
        ],
    )


def test_asserted_texts_are_the_ones_the_create_requests_write() -> None:
    a = (IDENT / "01-usera-semantic-create.bru").read_text()
    b = (IDENT / "04-userb-semantic-create.bru").read_text()
    assert '"text": "Identity check written by user A."' in a
    assert '"text": "Identity check written by user B."' in b
    put = (IDENT / "08-curator-update-usera-key.bru").read_text()
    assert '"text": "Curator write attempt on a key owned by user A."' in put


def test_identity_requests_run_in_file_order_with_unique_seq() -> None:
    seqs = [_seq(p) for p in IDENTITIES]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), dict(
        zip([p.name for p in IDENTITIES], seqs, strict=True)
    )
    assert _seq(IDENT / "08b-usera-read-after-curator-attempt.bru") > _seq(
        IDENT / "08-curator-update-usera-key.bru"
    )


def test_collection_bridge_copies_the_named_tokens() -> None:
    code = _strip_comments((COLLECTION / "collection.bru").read_text())
    assert 'bridge("tokenUserA");' in code
    assert 'bridge("tokenUserB");' in code


def test_no_temporary_requests_shipped() -> None:
    assert not list(COLLECTION.rglob("_tmp*.bru"))


_BANNED = re.compile("|".join(["lea" + "k", "pro" + "be", "another user" + "'s"]), re.I)


@pytest.mark.parametrize(
    "path",
    [
        *IDENTITIES,
        Path(__file__),
        Path(__file__).with_name("test_audittrace_login_env_key.py"),
    ],
    ids=lambda p: p.name,
)
def test_neutral_wording_in_new_files(path: Path) -> None:
    assert not _BANNED.findall(path.read_text()), path.name
