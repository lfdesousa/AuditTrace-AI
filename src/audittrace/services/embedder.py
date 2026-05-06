"""Cached default embedder for ChromaDB collections.

ChromaDB's stock ``DefaultEmbeddingFunction.__call__`` does
``return ONNXMiniLM_L6_V2()(input)`` — it instantiates a fresh ONNX
session on every call, holding ~80 MiB of model weights plus an ONNX
runtime context that Python GC does not promptly release because of
internal C-level references in onnxruntime. Across long-lived
processes this leaks ~1 GiB per few requests and OOMs the pod.

We need both:

* **Cached model** — exactly one ``ONNXMiniLM_L6_V2`` lives for the
  lifetime of the process; every embedding call goes through it.
* **Stable EmbeddingFunction identity** — ChromaDB 1.5+ persists
  the EF's ``name()`` (``"default"``) on the collection at first
  write, then refuses to query/upsert with a different EF on
  subsequent calls. Subclassing or wrapping with a different name
  triggers ``"Embedding function conflict: new: X vs persisted:
  default"`` 100% of the time on existing collections.
* **Modern protocol surface** — 1.5+ collection.query() routes
  through ``embed_query()`` / ``embed_documents()`` on the EF; a
  custom wrapper that only implements ``__call__`` raises
  ``AttributeError: '_X' object has no attribute 'embed_query'``.

The cleanest fix that keeps all three: **monkey-patch the stock
DefaultEmbeddingFunction.__call__ at import time** to delegate to a
module-level ``ONNXMiniLM_L6_V2`` instance instead of constructing a
new one. Class identity is preserved (``DefaultEmbeddingFunction``,
name ``"default"``, full inherited protocol), so persisted
collections stay queryable. The patch happens once at module import
and persists for the process lifetime.

Caught live 2026-05-06 on the per-file ``ai_research_papers`` index
loop. See ``feedback_use_context_managers`` and the PYTHON-ENGINEERING
skill (``~/work/claude-config/skills/PYTHON-ENGINEERING/SKILL.md``)
§2 "Singleton heavy state — never reload per call" for the broader
pattern.
"""

from __future__ import annotations

import logging
from typing import Any

from chromadb.api.types import DefaultEmbeddingFunction
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

logger = logging.getLogger(__name__)


# One ONNX model for the life of the process. Eagerly constructed
# at import time; the side-load happens once when this module is
# first imported (typically at FastAPI app startup). Subsequent
# embedding calls reuse this instance via the patched __call__
# below.
_cached_onnx = ONNXMiniLM_L6_V2()
logger.info(
    "Initialised cached ONNXMiniLM_L6_V2 embedder "
    "(replaces ChromaDB's default per-call instantiation)"
)


def _cached_default_call(self: Any, input: Any) -> Any:  # noqa: ARG001
    """Replacement for ``DefaultEmbeddingFunction.__call__``.

    Delegates to the module-level cached ``ONNXMiniLM_L6_V2`` instead
    of constructing a new one. ChromaDB's
    ``EmbeddingFunction.embed_query`` and ``embed_documents`` route
    through ``__call__`` on the base class, so patching here covers
    both the query and the upsert paths.

    Parameter names ``self``/``input`` are required: ChromaDB
    introspects the bound ``__call__``'s signature via
    ``inspect.signature`` to validate the EmbeddingFunction protocol
    and rejects mismatched parameter names (caught live 2026-05-06
    when ``_self`` triggered "Expected ('self', 'input'), got
    ('_self', 'input')"). Hence the ``noqa: ARG001`` — ``self`` is
    intentionally unused but the name is fixed.
    """
    return _cached_onnx(input)


DefaultEmbeddingFunction.__call__ = _cached_default_call  # type: ignore[method-assign]


# The instance every consumer should pass as ``embedding_function=...``.
# Using the stock class so the persisted collection metadata's
# ``name`` field stays ``"default"`` and existing collections remain
# queryable without re-indexing.
SINGLETON_EMBEDDER: DefaultEmbeddingFunction = DefaultEmbeddingFunction()
