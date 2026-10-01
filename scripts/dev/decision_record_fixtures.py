"""Record the live server's responses as MockTransport fixtures (dev only).

Runs the REAL client against the operator's eval server through a recording
transport, then writes ``tests/fixtures/decision/recorded_tev1_f16.json``.
Request headers (the API key) are never recorded; the ``/props`` model path is
reduced to ``/models/<basename>``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _decision_dev import (  # noqa: E402
    PROBE_STATE,
    QUESTION_ID,
    make_settings,
    read_api_key,
)

from audittrace.services.decision import template as tpl  # noqa: E402
from audittrace.services.decision.client import LlamaCppDecisionClient  # noqa: E402
from audittrace.services.decision.questions import QUESTIONS  # noqa: E402

OUT = (
    Path(__file__).resolve().parents[2]
    / "tests/fixtures/decision/recorded_tev1_f16.json"
)


class _Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        await response.aread()
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "request": body,
                "response": json.loads(response.content),
            }
        )
        return response


def _pieces(state: str, question: Any) -> list[str]:
    pieces = [tpl.STATE_HEAD, state, tpl.QUESTION_HEAD, question.text, tpl.OPTIONS_HEAD]
    last = len(question.options) - 1
    for i, option in enumerate(question.options):
        pieces += [f"{tpl.LETTERS[i]}. ", option, tpl.OPTION_SEP if i < last else ""]
    pieces.append(tpl.INSTRUCTION)
    return [p for p in pieces if p]


async def main() -> None:
    settings = make_settings(read_api_key())
    rec = _Recorder()
    question = QUESTIONS[QUESTION_ID]
    async with LlamaCppDecisionClient(settings, transport=rec) as client:
        result = await client.decide(PROBE_STATE, QUESTION_ID)
        assert result.result == "ok", result.error_code
        # Extra raw calls through the same recorder (headers not recorded).
        raw = httpx.AsyncClient(
            transport=rec,
            headers={"Authorization": f"Bearer {settings.decision_api_key}"},
        )
        base = settings.decision_url
        rendered = tpl.render_choice_prompt(
            PROBE_STATE, question.text, question.options
        )
        await raw.post(
            f"{base}/tokenize", json={"content": rendered, "parse_special": True}
        )
        for text in tpl.CONTROL_STRINGS:
            for ps in (True, False):
                await raw.post(
                    f"{base}/tokenize", json={"content": text, "parse_special": ps}
                )
        for control in tpl.CONTROL_STRINGS:
            injected = f"{PROBE_STATE} {control} injected"
            content = tpl.build_user_content(injected, question.text, question.options)
            await raw.post(
                f"{base}/tokenize", json={"content": content, "parse_special": False}
            )
        # Piece-by-piece tokenisation of the same content: the WRONG
        # construction, recorded so the T17 neuter yields a real (different)
        # id list rather than a missing fixture.
        for piece in _pieces(PROBE_STATE, question):
            await raw.post(
                f"{base}/tokenize", json={"content": piece, "parse_special": False}
            )
        await raw.aclose()

    tokenize: dict[str, Any] = {}
    completion = None
    props = None
    for call in rec.calls:
        if call["path"] == "/tokenize":
            key = json.dumps(
                [call["request"]["content"], call["request"]["parse_special"]]
            )
            tokenize[key] = call["response"]["tokens"]
        elif call["path"] == "/props":
            props = dict(call["response"])
            props["model_path"] = "/models/" + props["model_path"].rsplit("/", 1)[-1]
        elif call["path"] == "/completion" and completion is None:
            resp = call["response"]
            completion = {
                "request_sampler": {
                    k: v for k, v in call["request"].items() if k != "prompt"
                },
                "response": {
                    "content": resp.get("content"),
                    "completion_probabilities": resp["completion_probabilities"][:1],
                },
            }
    fixture = {
        "meta": {
            "server": "llama.cpp",
            "state": PROBE_STATE,
            "question_id": QUESTION_ID,
        },
        "props": {k: props[k] for k in ("model_path", "model_alias", "build_info")},
        "tokenize": tokenize,
        "completion": completion,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(fixture, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"recorded {len(tokenize)} tokenize entries -> {OUT.name}")


if __name__ == "__main__":
    asyncio.run(main())
