"""LC-10a Bruno folders added after the main session-recall folder:

* ``session-recall-userb/`` - a SECOND identity (``{{tokenUserB}}``) tries the
  first user's chat id over MCP and chat (expects nothing) and audits its own
  interactions;
* ``session-recall-negative/`` - the unresolved LibreChat header template is
  sent byte-for-byte and must be stored unscoped / find nothing;
* ``session-recall/04`` assembles the streamed ``delta.content`` before
  asserting (the file name is split across token deltas).

Parsed, not substring-matched; neuters: swap ``tokenUserB`` for ``accessToken``
in one userb request (RED), delete a file (RED), revert the 04 assertion to the
raw SSE text (RED).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
USERB = COLLECTION / "session-recall-userb"
NEGATIVE = COLLECTION / "session-recall-negative"
ENV = COLLECTION / "environments" / "cluster-session-e2e.bru"
STREAM = COLLECTION / "session-recall" / "04-chat-recall-stream-true.bru"
# Supplied per run with --env-var (the second identity's token; the shared
# chat id) - deliberately not stored in any environment file.
RUNTIME_VARS = {"tokenUserB", "chatSessionId"}


def _block(text: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)} \{{\n(.*?)^\}}\n", text, re.S | re.M)
    return m.group(1) if m else None


def _names(folder: Path) -> list[str]:
    return [p.name for p in sorted(folder.glob("*.bru"))]


def test_the_userb_folder_holds_exactly_its_three_requests_in_order() -> None:
    assert _names(USERB) == [
        "01-userb-mcp-recall-same-chat-id-expect-empty.bru",
        "02-userb-chat-recall-same-chat-id-expect-empty.bru",
        "03-userb-audit-own-interactions-by-session.bru",
    ]
    seqs = [
        int(re.search(r"seq: (\d+)", p.read_text()).group(1))  # type: ignore[union-attr]
        for p in sorted(USERB.glob("*.bru"))
    ]
    assert seqs == [1, 2, 3]


def test_the_negative_folder_holds_exactly_its_two_requests_in_order() -> None:
    assert _names(NEGATIVE) == [
        "01-upload-with-unresolved-placeholder-is-stored-unscoped.bru",
        "02-recall-with-the-placeholder-finds-nothing.bru",
    ]
    seqs = [
        int(re.search(r"seq: (\d+)", p.read_text()).group(1))  # type: ignore[union-attr]
        for p in sorted(NEGATIVE.glob("*.bru"))
    ]
    assert seqs == [1, 2]


@pytest.mark.parametrize("path", sorted(USERB.glob("*.bru")), ids=lambda p: p.name)
def test_every_userb_request_authenticates_as_user_b_only(path: Path) -> None:
    auth = _block(path.read_text(), "auth:bearer")
    assert auth is not None
    assert auth.strip() == "token: {{tokenUserB}}", path.name
    assert "accessToken" not in path.read_text(), path.name


@pytest.mark.parametrize(
    "path",
    [
        USERB / "01-userb-mcp-recall-same-chat-id-expect-empty.bru",
        USERB / "02-userb-chat-recall-same-chat-id-expect-empty.bru",
    ],
    ids=lambda p: p.name,
)
def test_the_userb_recalls_use_the_first_users_chat_id(path: Path) -> None:
    headers = _block(path.read_text(), "headers") or ""
    assert "X-Session-Id: {{chatSessionId}}" in headers, path.name


@pytest.mark.parametrize("path", sorted(NEGATIVE.glob("*.bru")), ids=lambda p: p.name)
def test_the_negative_requests_send_the_literal_placeholder_from_script(
    path: Path,
) -> None:
    script = _block(path.read_text(), "script:pre-request") or ""
    assert (
        'req.setHeader("X-Session-Id", "{" + "{LIBRECHAT_BODY_CONVERSATIONID}" + "}")'
        in script
    ), path.name
    # a headers block would be interpolated by Bruno and lose the template
    assert "X-Session-Id" not in (_block(path.read_text(), "headers") or "")


def test_every_variable_the_new_folders_use_is_defined_or_a_runtime_var() -> None:
    env_vars = set(
        re.findall(r"^\s+(\w+):", _block(ENV.read_text(), "vars") or "", re.M)
    )
    used: set[str] = set()
    for folder in (USERB, NEGATIVE):
        for p in folder.glob("*.bru"):
            text = p.read_text()
            for prose in ("docs", "script:pre-request"):
                body = _block(text, prose)
                if body:  # prose / JS may quote the literal template
                    text = text.replace(body, "")
            used.update(re.findall(r"\{\{(\w+)\}\}", text))
    # accessToken comes from the login bridge, baseUrl from the environment
    undefined = sorted(used - env_vars - RUNTIME_VARS - {"accessToken"})
    assert not undefined, f"undefined in cluster-session-e2e.bru: {undefined}"


def test_the_stream_true_assertion_scans_the_assembled_delta_text() -> None:
    tests = _block(STREAM.read_text(), "tests") or ""
    assert 'startsWith("data:")' in tests
    assert "choice.delta && choice.delta.content" in tests
    assert "expect(assembled).to.include(" in tests
    # the old form: the raw SSE text asserted directly
    assert "expect(raw).to.include" not in tests


# O4 (LC-10a review r8): the assertion text above is pinned, but the JS that
# ASSEMBLES the stream must actually run. Bruno's `tests {}` body is executed
# here under node with Bruno's globals stubbed (test / expect / res / bru), on
# a fixture whose note name is split across token deltas. Neuters: `assembled
# = piece` (overwrite) and asserting the raw SSE both go RED. node is a hard
# requirement (zero-skip policy): its absence FAILS this test.
_NODE_HARNESS = r"""
const results = [];
const test = (name, fn) => {
  try { fn(); results.push([name, "pass"]); }
  catch (e) { results.push([name, "fail: " + e.message]); }
};
const expect = (actual) => ({
  to: {
    include: (needle) => {
      if (!String(actual).includes(needle)) {
        throw new Error("expected " + JSON.stringify(actual) + " to include " + needle);
      }
    },
    equal: (want) => {
      if (actual !== want) throw new Error("expected " + actual + " to equal " + want);
    },
  },
});
const fixture = JSON.parse(process.env.SSE_FIXTURE);
const res = { getStatus: () => 200, getBody: () => fixture.body };
const bru = { getEnvVar: (k) => fixture.env[k] };
%s
console.log(JSON.stringify(results));
"""


def _run_stream_tests_under_node(sse: str, note_name: str) -> dict[str, str]:
    import json
    import os
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node is not None, "node is required to execute the Bruno test JS"
    body = _block(STREAM.read_text(), "tests")
    assert body is not None
    env = {
        **os.environ,
        "SSE_FIXTURE": json.dumps({"body": sse, "env": {"sessionNoteName": note_name}}),
    }
    proc = subprocess.run(  # noqa: S603
        [node, "-e", _NODE_HARNESS % body],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return dict(json.loads(proc.stdout))


def _frame(piece: str | None, role: bool = False) -> str:
    import json

    delta: dict[str, str] = {"role": "assistant"} if role else {}
    if piece is not None:
        delta["content"] = piece
    return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"


def test_the_stream_assembly_js_finds_a_name_split_across_token_deltas() -> None:
    sse = (
        _frame(None, role=True)
        + _frame("The file is lc10")
        + "data: not-json\n\n"
        + _frame("a-sess")
        + ": keepalive comment\n\n"
        + _frame("ion-note.md")
        + "data: [DONE]\n\n"
    )
    results = _run_stream_tests_under_node(sse, "lc10a-session-note.md")
    assert results["returns 200"] == "pass"
    assert results["the streamed answer names the uploaded file"] == "pass"


def test_the_stream_assembly_js_fails_when_the_name_is_absent() -> None:
    sse = _frame("nothing relevant here") + "data: [DONE]\n\n"
    results = _run_stream_tests_under_node(sse, "lc10a-session-note.md")
    assert results["the streamed answer names the uploaded file"].startswith("fail:")
