"""Fake llama.cpp servers for the #459 decision-client tests.

Two flavours, both ``httpx.MockTransport`` handlers that record every call:

* :class:`FakeLlama`: a synthetic server with a char-level tokenizer that
  emulates the MEASURED control-token behaviour of the pinned tokenizer
  (``parse_special:false`` splits the role tokens but still returns
  ``<think>``/``</think>``/``<tool_call>``/``</tool_call>`` as single ids).
* :class:`RecordedLlama`: replays responses RECORDED from the live eval server
  (``tests/fixtures/decision/recorded_tev1_f16.json``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from audittrace.config import Settings
from tests.fakes.decision_added_tokens import ID_OF, LEAKY

DIGEST = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"
URL = "http://decision.test"
ALIAS = "tev1-test"

# Added-token ids of the fake tokenizer (same ids the real tokenizer returns).
CONTROL_IDS = dict(ID_OF)

RECORDED = (
    Path(__file__).resolve().parents[1] / "fixtures/decision/recorded_tev1_f16.json"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "decision_url": URL,
        "decision_api_key": "",
        "decision_model": "tev1-test-model",
        "decision_model_alias": ALIAS,
        "decision_model_digest": DIGEST,
        "decision_timeout_ms": 2000,
        "decision_temperature": 1.0,
        "decision_backend": "vulkan",
        "decision_quantisation": "f16",
        "memory_routing_mode": "shadow",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def fake_tokenize(text: str, parse_special: bool) -> list[int]:
    """Char-level ids (``ord``) with the measured added-token behaviour.

    ``parse_special:true``: every added token is ONE id. ``parse_special:false``:
    only the measured ``LEAKY`` six stay single ids; the rest split to text.
    """
    ids: list[int] = []
    i = 0
    while i < len(text):
        for ctl, cid in CONTROL_IDS.items():
            if text.startswith(ctl, i) and (parse_special or ctl in LEAKY):
                ids.append(cid)
                i += len(ctl)
                break
        else:
            ids.append(ord(text[i]))
            i += 1
    return ids


def letter_top(
    letter_lps: dict[str, float], *, extra: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """A ``top_logprobs`` list keyed by the fake tokenizer's letter ids."""
    top = [
        {"id": ord(letter), "token": letter, "logprob": lp}
        for letter, lp in letter_lps.items()
    ]
    return top + (extra or [])


DEFAULT_TOP = letter_top({"A": -3.0, "B": -0.4, "C": -2.5, "D": -1.4, "E": -4.0})


class FakeLlama:
    """Synthetic llama.cpp server; records every request."""

    def __init__(
        self,
        *,
        top: list[dict[str, Any]] | None = None,
        alias: str = ALIAS,
        model_path: str = "/home/someone/models/tev1-test.gguf",
        build_info: str = "b-test",
        tokenize: Callable[[str, bool], list[int]] = fake_tokenize,
        on_request: Callable[[httpx.Request], Awaitable[httpx.Response | None]]
        | None = None,
        completion_body: dict[str, Any] | None = None,
    ) -> None:
        self.top = DEFAULT_TOP if top is None else top
        self.alias = alias
        self.model_path = model_path
        self.build_info = build_info
        self.tokenize = tokenize
        self.on_request = on_request
        self.completion_body = completion_body
        self.requests: list[httpx.Request] = []

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            override = await self.on_request(request)
            if override is not None:
                return override
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "/props":
            return httpx.Response(
                200,
                json={
                    "model_path": self.model_path,
                    "model_alias": self.alias,
                    "build_info": self.build_info,
                    "total_slots": 1,
                },
            )
        if request.method == "POST" and path == "/tokenize":
            ids = self.tokenize(body["content"], body["parse_special"])
            return httpx.Response(200, json={"tokens": ids})
        if request.method == "POST" and path == "/completion":
            if self.completion_body is not None:
                return httpx.Response(200, json=self.completion_body)
            return httpx.Response(
                200,
                json={
                    "content": "B",
                    "completion_probabilities": [
                        {"id": 66, "token": "B", "top_logprobs": self.top}
                    ],
                },
            )
        return httpx.Response(404, json={"error": "unexpected"})


def load_recorded() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RECORDED.read_text(encoding="utf-8"))
    return loaded


class RecordedLlama:
    """Replays the recorded live-server responses (T17 / T14 recorded)."""

    def __init__(self) -> None:
        self.fx = load_recorded()
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def tokens(self, content: str, parse_special: bool) -> list[int]:
        key = json.dumps([content, parse_special])
        tokens: list[int] = self.fx["tokenize"][key]
        return tokens

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "/props":
            return httpx.Response(200, json=self.fx["props"])
        if request.method == "POST" and path == "/tokenize":
            key = json.dumps([body["content"], body["parse_special"]])
            if key not in self.fx["tokenize"]:
                return httpx.Response(500, json={"error": "not recorded"})
            return httpx.Response(200, json={"tokens": self.fx["tokenize"][key]})
        if request.method == "POST" and path == "/completion":
            return httpx.Response(200, json=self.fx["completion"]["response"])
        return httpx.Response(404, json={"error": "unexpected"})


async def sleep_then(
    seconds: float, response: httpx.Response | None = None
) -> httpx.Response | None:
    await asyncio.sleep(seconds)
    return response
