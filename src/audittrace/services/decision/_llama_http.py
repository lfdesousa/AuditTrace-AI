"""llama.cpp server HTTP calls + response parsing (#459 WU-459-1).

Private to the decision package. Every failure is raised as a
:class:`DecisionError` with a closed code, or leaves as an ``httpx``
exception that :func:`map_transport_error` classifies. Response BODIES are
never read into messages or logs: an upstream error body may echo the prompt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from audittrace.services.decision.errors import DecisionError


@dataclass(frozen=True)
class ServerIdentity:
    """What ``GET /props`` reported (the path is reduced to its basename)."""

    model_file: str
    model_alias: str
    build_info: str


def map_transport_error(exc: BaseException) -> str | None:
    """Closed code for an httpx transport error, or ``None`` if not one."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "connect_error"
    return None


async def _json(
    http: httpx.AsyncClient, method: str, url: str, body: dict[str, Any] | None
) -> Any:
    response = await http.request(method, url, json=body)
    if response.status_code != 200:
        raise DecisionError("http_status")
    try:
        return response.json()
    except ValueError as exc:
        raise DecisionError("malformed_response") from exc


def _basename(path: str) -> str:
    # The full path is never recorded (it would expose the operator's home).
    return path.replace("\\", "/").rsplit("/", 1)[-1]


async def get_props(http: httpx.AsyncClient, base: str) -> ServerIdentity:
    """``GET {base}/props``. GET ONLY: POST /props mutates server settings."""
    body = await _json(http, "GET", f"{base}/props", None)
    if not isinstance(body, dict):
        raise DecisionError("malformed_response")
    path, alias, build = (
        body.get(k) for k in ("model_path", "model_alias", "build_info")
    )
    if not (
        isinstance(path, str) and isinstance(alias, str) and isinstance(build, str)
    ):
        raise DecisionError("malformed_response")
    return ServerIdentity(_basename(path), alias, build)


async def tokenize(
    http: httpx.AsyncClient, base: str, content: str, *, parse_special: bool
) -> list[int]:
    """``POST {base}/tokenize``; ``parse_special`` is always explicit."""
    body = await _json(
        http,
        "POST",
        f"{base}/tokenize",
        {"content": content, "parse_special": parse_special},
    )
    tokens = body.get("tokens") if isinstance(body, dict) else None
    if not isinstance(tokens, list) or not all(
        isinstance(t, int) and not isinstance(t, bool) for t in tokens
    ):
        raise DecisionError("malformed_response")
    return list(tokens)


async def complete_top_logprobs(
    http: httpx.AsyncClient, base: str, request: dict[str, Any]
) -> dict[int, float]:
    """``POST {base}/completion``; return ``{token_id: logprob}`` as returned."""
    body = await _json(http, "POST", f"{base}/completion", request)
    probs = body.get("completion_probabilities") if isinstance(body, dict) else None
    if not isinstance(probs, list) or not probs:
        raise DecisionError("missing_probabilities")
    first = probs[0]
    top = first.get("top_logprobs") if isinstance(first, dict) else None
    if not isinstance(top, list) or not top:
        raise DecisionError("missing_probabilities")
    raw: dict[int, float] = {}
    for entry in top:
        if not isinstance(entry, dict):
            raise DecisionError("malformed_response")
        token_id, logprob = entry.get("id"), entry.get("logprob")
        if (
            not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or not isinstance(logprob, (int, float))
            or isinstance(logprob, bool)
            or not math.isfinite(logprob)
        ):
            raise DecisionError("malformed_response")
        raw[token_id] = float(logprob)
    return raw
