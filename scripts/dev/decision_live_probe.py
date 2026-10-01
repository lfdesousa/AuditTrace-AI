"""Live dev probe for the #459 decision client (NOT shipped; ADR-049 evidence).

Calls ``LlamaCppDecisionClient`` against the operator's eval server and checks,
from an INDEPENDENT source, that the server is the model the config pins.

* ``GET /props`` only (POST /props mutates server settings; never sent here).
* The ``/props`` ``model_path`` file is hashed ON THE HOST by this script
  (``hashlib``, not the client) and compared with the configured digest and the
  recorded Phase-0 digest.
* T17 live: the client's constructed token ids == ``/tokenize(rendered,
  parse_special:true)``; the piece-by-piece construction is measured to differ.
* The 7 control strings are measured live (single id with parse_special:true;
  which ones survive ``parse_special:false``).
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

        # Control strings, measured live.
        print("-- control strings (parse_special true / false)")
        single_true = {}
        for s in tpl.CONTROL_STRINGS:
            t_true = await _tokens(http, base, s, True)
            t_false = await _tokens(http, base, s, False)
            single_true[s] = t_true
            print(f"   {s!r}: true={t_true} false={t_false}")
        ok &= _check(
            "each control string is ONE id with parse_special:true",
            all(len(v) == 1 for v in single_true.values()),
        )
        leaky = [
            s
            for s in tpl.CONTROL_STRINGS
            if await _tokens(http, base, s, False) == single_true[s]
        ]
        print(f"   still single control ids with parse_special:false: {leaky}")
        ok &= _check(
            "C2 SF-1 reproduces: <think>,</think>,<tool_call>,</tool_call> leak",
            set(leaky) == {"<think>", "</think>", "<tool_call>", "</tool_call>"},
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
        injected = await client.decide(f"{PROBE_STATE} <think> x", QUESTION_ID)
        ok &= _check(
            "planted <think> -> invalid_input",
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
