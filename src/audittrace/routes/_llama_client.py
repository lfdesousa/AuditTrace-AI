"""Shared httpx connection-pool config for llama-facing clients.

llama.cpp advertises ``Keep-Alive: timeout=5`` and FIN-closes an idle HTTP
connection after 5 s (confirmed live via tcpdump: the llama side sends FIN
first on idle connections). httpx's own default pool keepalive (~5 s) races
that close: on a slow upstream the gap between two llama calls (a tool-loop
turn, or slow generation) can exceed 5 s, so the app's connection pool
reuses a socket llama has already torn down and the next call raises
``httpx.RemoteProtocolError: Server disconnected without sending a
response``. Capping the client-side keepalive comfortably below llama's
means httpx discards the idle connection itself before llama can — pooling
still helps fast (MoE) turns, and the race is gone for slow ones.

Single source of truth (#371 lockstep) — every llama-facing
``httpx.AsyncClient`` in ``chat.py`` and ``_memory_tool_loop.py`` passes
this SAME object; no per-site literals. Kept in its own leaf module
(deliberately pure and side-effect free, same rationale as
``_model_route.py``) so ``chat.py`` and ``_memory_tool_loop.py`` can both
import it without a circular import — ``chat.py`` imports
``_memory_tool_loop.py`` at module scope, so ``_memory_tool_loop.py``
cannot import this constant back FROM ``chat.py`` without the result
depending on which module a caller (or a test) happens to import first.
"""

from __future__ import annotations

import httpx

LLAMA_HTTPX_LIMITS = httpx.Limits(keepalive_expiry=2.0)
