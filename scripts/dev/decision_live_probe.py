"""Live dev probe for the #459 decision client (NOT shipped; ADR-049 evidence).

Calls ``LlamaCppDecisionClient`` against the operator's eval server and checks,
from an INDEPENDENT source, that the server is the model the config pins.

* ``GET /props`` only (POST /props mutates server settings; never sent here).
* The ``/props`` ``model_path`` file is hashed ON THE HOST by this script
  (``hashlib``, not the client) and compared with the configured digest and the
  recorded Phase-0 digest.
* T17 live: the client's constructed token ids == ``/tokenize(rendered,
  parse_special:true)``; the piece-by-piece construction is measured to differ.
* The probe ENUMERATES: every entry of the vendored added-token table is
  tokenised live; a single id surviving ``parse_special:false`` must be in the
  denylist, and a /detokenize range scan checks piece/id agreement. Any gap
  makes the probe exit non-zero.
* T9 live: recomputing the distribution from the row's own fields.

Environment (nothing target-shaped is hard-coded):
``AUDITTRACE_DECISION_URL`` and ``AUDITTRACE_DECISION_API_KEY_FILE`` (see
``_decision_dev``). The key is read inside this process and never printed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _decision_dev import (  # noqa: E402
    EXPECTED_F16_DIGEST,
    PROBE_STATE,
    QUESTION_ID,
    make_settings,
    read_api_key,
)

from audittrace.services.decision import template as tpl  # noqa: E402
from audittrace.services.decision.client import LlamaCppDecisionClient  # noqa: E402
from audittrace.services.decision.distribution import renormalise  # noqa: E402
from audittrace.services.decision.provenance import (  # noqa: E402
    ARGS_KEYS,
    build_pending_tool_call,
)
from audittrace.services.decision.questions import QUESTIONS  # noqa: E402
from audittrace.services.decision.token_tables import table_for  # noqa: E402


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _tokens(
    http: httpx.AsyncClient, base: str, text: str, special: bool
) -> list[int]:
    resp = await http.post(
        f"{base}/tokenize", json={"content": text, "parse_special": special}
    )
    resp.raise_for_status()
    tokens: list[int] = resp.json()["tokens"]
    return tokens


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())
    return ok


async def main() -> int:  # noqa: C901 - linear probe script
    settings = make_settings(read_api_key())
    base = settings.decision_url
    question = QUESTIONS[QUESTION_ID]
    ok = True
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {settings.decision_api_key}"}, timeout=60
    ) as http:
        props = (await http.get(f"{base}/props")).json()  # GET only
        model_path = props["model_path"]
        print(
            f"/props model_alias={props['model_alias']} build_info={props['build_info']}"
        )
        print(f"/props model_file={Path(model_path).name}")
        host_sha = _file_sha256(model_path)
        print(f"host_sha256_of_props_model_path = {host_sha}")
        print(f"configured_digest               = {settings.decision_model_digest}")
        print(f"recorded_phase0_f16_digest      = {EXPECTED_F16_DIGEST}")
        ok &= _check(
            "identity: host sha256 == configured == recorded",
            host_sha == settings.decision_model_digest == EXPECTED_F16_DIGEST,
        )

        # Added tokens, ENUMERATED live against the vendored table (B1/C3).
        table = table_for(settings.decision_model_digest)
        assert table is not None, "no vendored added-token table for the digest"
        denylist = table.ids
        print(f"-- added tokens: {len(table.tokens)} vendored, {len(denylist)} denied")
        leaky: list[str] = []
        gaps: list[str] = []
        table_ok = True
        for token_id, token in table.tokens.items():
            t_true = await _tokens(http, base, token.content, True)
            t_false = await _tokens(http, base, token.content, False)
            if t_true != [token_id]:
                table_ok = False
                gaps.append(f"{token.content!r}: vendored id {token_id}, live {t_true}")
            if len(t_false) == 1:
                leaky.append(token.content)
                if t_false[0] not in denylist:
                    gaps.append(
                        f"{token.content!r}: survives as {t_false[0]}, NOT denied"
                    )
        print(f"   still ONE id with parse_special:false: {leaky}")
        ok &= _check(
            "every vendored id matches the live id (parse_special:true)", table_ok
        )
        ok &= _check(
            "every surviving single id is in the denylist", not gaps, str(gaps)
        )
        # Range scan: any id whose piece is a vendored string must be that id.
        lo, hi = min(denylist) - 8, max(denylist) + 64
        by_piece = {t.content: i for i, t in table.tokens.items()}
        scan_gaps: list[str] = []
        ordinary: list[tuple[int, str]] = []
        for token_id in range(lo, hi + 1):
            resp = await http.post(f"{base}/detokenize", json={"tokens": [token_id]})
            piece = resp.json().get("content", "") if resp.status_code == 200 else ""
            if piece in by_piece and by_piece[piece] != token_id:
                scan_gaps.append(
                    f"id {token_id} piece {piece!r} != vendored {by_piece[piece]}"
                )
            if token_id not in denylist and piece:
                t_false = await _tokens(http, base, piece, False)
                if t_false == [token_id]:
                    ordinary.append((token_id, piece))
        ok &= _check(
            f"range scan {lo}..{hi}: no piece/id disagreement with the table",
            not scan_gaps,
            str(scan_gaps),
        )
        # Single ids that survive parse_special:false but are not added tokens
        # are ordinary words: reported, not failed.
        print(
            f"   ordinary single-id words in range (reported, not failed): {ordinary}"
        )
        ok &= _check(
            "measurement reproduces: the six leaky tokens",
            set(leaky)
            == {
                "<tool_call>",
                "</tool_call>",
                "<tool_response>",
                "</tool_response>",
                "<think>",
                "</think>",
            },
        )

        # T17 live.
        rendered = tpl.render_choice_prompt(
            PROBE_STATE, question.text, question.options
        )
        want = await _tokens(http, base, rendered, True)
        pieces = [
            tpl.STATE_HEAD,
            PROBE_STATE,
            tpl.QUESTION_HEAD,
            question.text,
            tpl.OPTIONS_HEAD,
        ]
        last = len(question.options) - 1
        for i, option in enumerate(question.options):
            pieces += [
                f"{tpl.LETTERS[i]}. ",
                option,
                tpl.OPTION_SEP if i < last else "",
            ]
        pieces.append(tpl.INSTRUCTION)
        by_piece: list[int] = []
        for piece in (p for p in pieces if p):
            by_piece += await _tokens(http, base, piece, False)
        whole_content = await _tokens(
            http,
            base,
            tpl.build_user_content(PROBE_STATE, question.text, question.options),
            False,
        )

    async with LlamaCppDecisionClient(settings) as client:
        got = await client.build_prompt_ids(PROBE_STATE, question)
        ok &= _check(
            "T17 live: constructed ids == /tokenize(rendered, true)",
            got == want,
            f"({len(got)} ids)",
        )
        ok &= _check(
            "C2 SF-2 reproduces: piece-by-piece != one call",
            by_piece != whole_content,
            f"(piece-by-piece {len(by_piece)} ids, one call {len(whole_content)} ids)",
        )

        result = await client.decide(PROBE_STATE, QUESTION_ID)
        ok &= _check(
            "decide() result ok",
            result.result == "ok",
            f"error_code={result.error_code}",
        )
        pending = build_pending_tool_call(
            result, user_id="probe-user", agent_type="dev-probe", mode="shadow"
        )
        args: dict[str, Any] = json.loads(pending.args)
        ok &= _check(
            "args keys == ARGS_KEYS", set(args) == set(ARGS_KEYS), f"({len(args)} keys)"
        )
        raw = {int(k): v for k, v in args["raw_top_logprobs"].items()}
        recomputed = renormalise(raw, args["allowed_ids"], args["temperature"])
        ok &= _check(
            "T9 live: recompute == recorded EXACTLY", recomputed == args["distribution"]
        )
        ok &= _check(
            "server_model_file is a basename",
            "/" not in args["server_model_file"],
            args["server_model_file"],
        )
        ok &= _check(
            "input_sha256 matches ids", args["input_sha256"] == tpl.input_sha256(want)
        )
        # No text on the row.
        blob = pending.args + (pending.result_summary or "") + (pending.error or "")
        ok &= _check(
            "no state/question/option text in the row",
            all(t not in blob for t in (PROBE_STATE, question.text, *question.options)),
        )
        # Live denylist: planted control string -> invalid_input, no completion.
        for planted in ("<think>", "<tool_response>", "</tool_response>"):
            injected = await client.decide(f"{PROBE_STATE} {planted} x", QUESTION_ID)
            ok &= _check(
                f"planted {planted} -> invalid_input",
                (injected.result, injected.error_code) == ("error", "invalid_input"),
            )

    print("-- args JSON (no text; pending.tool_name/provenance/granted_scope below)")
    print(json.dumps(args, indent=1, sort_keys=True))
    print(
        f"tool_name={pending.tool_name} provenance={pending.provenance} granted_scope={pending.granted_scope}"
    )
    print(f"result_summary={pending.result_summary} error={pending.error}")
    print("PROBE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
