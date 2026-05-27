"""Unit tests for the in-cluster LLM stub (``images/llm-stub/server.py``).

Guards the #218 fix: the stub's ``prompt_tokens`` must scale with the
request payload so memory-augmentation assertions — which infer "context
was injected" from a large ``prompt_tokens`` (the Bruno chat pack) — hold
against the stub, not only a real llama-server. A bare prompt stays small;
an augmented one crosses the pack's >100 threshold naturally.

The stub lives outside ``src/`` (it ships as its own image), so it is
loaded by file path rather than imported as a package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_STUB_PATH = (
    Path(__file__).resolve().parent.parent / "images" / "llm-stub" / "server.py"
)


@pytest.fixture(scope="module")
def stub():
    spec = importlib.util.spec_from_file_location(
        "audittrace_llm_stub_server", _STUB_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_estimate_prompt_tokens_scales_with_payload(stub) -> None:
    assert stub._estimate_prompt_tokens([]) == 1
    assert stub._estimate_prompt_tokens("not a list") == 1
    small = stub._estimate_prompt_tokens([{"role": "user", "content": "hi"}])
    big = stub._estimate_prompt_tokens([{"role": "user", "content": "x" * 800}])
    assert small < 10, "a bare prompt must report few tokens"
    assert big >= 100, "an 800-char prompt must cross the augmentation threshold"


def test_estimate_handles_multimodal_content_parts(stub) -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "y" * 400}]}]
    assert stub._estimate_prompt_tokens(msgs) == 100  # 400 chars / 4


def test_estimate_ignores_malformed_entries(stub) -> None:
    # Non-dict messages and missing content must not raise.
    assert stub._estimate_prompt_tokens([None, 42, {"role": "user"}]) == 1


async def test_chat_completion_small_prompt_low_tokens(stub) -> None:
    body = await stub.chat_completions(
        {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert body["choices"][0]["message"]["content"] == "bruno"
    assert body["choices"][0]["finish_reason"] == "stop"
    usage = body["usage"]
    assert usage["prompt_tokens"] < 100
    assert usage["completion_tokens"] >= 1
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


async def test_chat_completion_augmented_prompt_crosses_threshold(stub) -> None:
    # Simulate the memory-server having injected retrieved context: a large
    # system message → prompt_tokens must exceed the Bruno pack's 100.
    augmented = [
        {"role": "system", "content": "CONTEXT:\n" + ("retrieved memory line. " * 60)},
        {"role": "user", "content": "summarise"},
    ]
    body = await stub.chat_completions({"messages": augmented})
    assert body["usage"]["prompt_tokens"] > 100
