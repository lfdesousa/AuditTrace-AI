"""Server-side embedding client (ADR-047).

Embedding runs on the dedicated ``nomic-embed-server`` (``embed_url``),
not in the memory-server process. The request gateway therefore hosts no
ML model: there is no ONNX session, no model weights resident per pod, and
no per-call model instantiation. This module is just a thin, pooled HTTP
client around the embed server's OpenAI-compatible ``/v1/embeddings``
endpoint.

History: until 2026-06-19 embedding ran in-process via ChromaDB's stock
``DefaultEmbeddingFunction`` (ONNX all-MiniLM-L6-v2, 384-dim). That leaked
the model on every call and OOM'd the pod; a cached singleton contained the
OOM but left the architectural smell of a gateway hosting a 1–1.5 GiB model.
ADR-047 moved embedding onto the dedicated nomic server (768-dim); this file
is the result — the in-process path is gone.

The outbound httpx call is covered by the process-wide
``HTTPXClientInstrumentor`` (server.py), so each embed call appears on the
trace as a ``peer.service=nomic-embed-server`` span — the model topology
stays inside the audit trail.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class EmbeddingServerError(RuntimeError):
    """The nomic embed server could not be reached or returned a bad shape.

    Callers in the recall path swallow this and degrade to "no results for
    this collection" (mirroring ChromaSemanticService.search's per-collection
    try/except); callers in the index path propagate it so a failed embed
    surfaces rather than silently dropping the document.
    """


# One shared async client for the life of the process (PYTHON-ENGINEERING §2
# — reuse the connection pool, never construct per call). Lazily created so
# import stays side-effect-free for unit tests that never embed.
_embed_client: httpx.AsyncClient | None = None


def _embed_client_singleton() -> httpx.AsyncClient:
    global _embed_client
    if _embed_client is None:
        _embed_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _embed_client


async def embed_via_nomic(
    texts: list[str],
    *,
    embed_url: str,
    model: str = "nomic-embed-text",
    client: httpx.AsyncClient | None = None,
    max_attempts: int = 3,
) -> list[list[float]]:
    """Embed ``texts`` on the dedicated nomic-embed-server (ADR-047).

    POSTs the OpenAI-compatible batch ``{"model", "input": [...]}`` to
    ``{embed_url}/embeddings`` and returns the 768-dim vectors **in input
    order** (sorted by the response ``index`` for safety). Bounded retry
    with linear backoff on transient transport/HTTP errors; raises
    :class:`EmbeddingServerError` once attempts are exhausted.

    ``client`` is injectable for tests; in production the module-level
    pooled :class:`httpx.AsyncClient` is reused.
    """
    if not texts:
        return []

    url = embed_url.rstrip("/") + "/embeddings"
    cl = client or _embed_client_singleton()
    payload = {"model": model, "input": texts}

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await cl.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()["data"]
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            vectors = [d["embedding"] for d in ordered]
            if len(vectors) != len(texts):
                raise EmbeddingServerError(
                    f"embed server returned {len(vectors)} vectors for "
                    f"{len(texts)} inputs"
                )
            return vectors
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(0.2 * attempt)

    raise EmbeddingServerError(
        f"nomic embed failed after {max_attempts} attempts: {last_exc}"
    ) from last_exc
