"""OpenAI-compatible LLM stub.

Returns deterministic canned responses so the memory-server proxy /
auth / RLS / telemetry pipeline can be exercised end-to-end without a
real llama-server (or any GPU). Used by:

  * the kind integration test (chart `llmStub.enabled=true`),
  * docker-compose (`mock-llm` profile),
  * any cluster that wants to validate wiring without inference.

The three llama-server roles (chat / embed / summarizer) are all
served by this single app — the chart fronts it with three Services
(`*-llm-chat`, `*-llm-embed`, `*-llm-summarizer`) that port-remap to
this app's single listen port. Chat and summarizer are both
`/v1/chat/completions` calls; embed is `/v1/embeddings`.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI

app = FastAPI(title="audittrace-llm-stub")

# nomic-embed-text-v1.5 dimensionality (matches
# config.py::memory_embedding_dim). Deterministic non-zero vector so
# downstream cosine-similarity math stays well-defined.
_EMBEDDING_DIM = 1024
_CANNED_EMBEDDING = [0.1] * _EMBEDDING_DIM


# Canned model id + assistant content preserved from the original mock
# so this stub is a byte-faithful drop-in for every existing consumer
# (test-models.sh asserts the id; the Bruno chat collection + e2e
# scripts assume content="bruno").
_MODEL_ID = "audittrace-default"
_CANNED_CONTENT = "bruno"


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": _MODEL_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "audittrace-stub",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: dict[str, Any]) -> dict[str, Any]:
    """Chat + summarizer path. Canned assistant content = "bruno"."""
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.get("model", _MODEL_ID),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _CANNED_CONTENT},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 1,
            "total_tokens": 5,
        },
    }


@app.post("/v1/embeddings")
async def embeddings(req: dict[str, Any]) -> dict[str, Any]:
    """Embed path. Returns one canned 1024-dim vector per input.

    Accepts the OpenAI shape where `input` is a string or list of
    strings; emits one embedding object per input element.
    """
    raw = req.get("input", "")
    inputs = raw if isinstance(raw, list) else [raw]
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": _CANNED_EMBEDDING,
        }
        for i, _ in enumerate(inputs)
    ]
    return {
        "object": "list",
        "data": data,
        "model": req.get("model", _MODEL_ID),
        "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
