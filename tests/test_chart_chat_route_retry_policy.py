"""AT-NONSTREAM-UPSTREAM-STATUS -- the chat route's mesh retry policy is explicit.

``POST /v1/chat/completions`` is a model call: a proxy on the path must never send it a
second time because of a response it received. The memory VirtualService therefore pins
the route's retry policy instead of inheriting the mesh default (which has changed
between Istio releases). Rendered with ``helm template``; the other routes keep the
default.
"""

from __future__ import annotations

from typing import Any

from tests.test_memory_virtualservice_chart import _find, _render_chart

#: Operator ruling Q4: the exact ``retries`` block of the chat route.
CHAT_ROUTE_RETRIES: dict[str, Any] = {
    "attempts": 2,
    "retryOn": "connect-failure,refused-stream",
}
CHAT_PREFIX = "/v1/chat/completions"
#: Retry conditions that fire before any response exists (Envoy ``retry_on`` names).
BEFORE_A_RESPONSE = {"connect-failure", "refused-stream", "reset-before-request"}


def _routes() -> list[dict[str, Any]]:
    vs = _find(_render_chart(), "VirtualService", "audittrace-memory")
    routes: list[dict[str, Any]] = vs["spec"]["http"]
    return routes


def _is_chat(route: dict[str, Any]) -> bool:
    return any(
        m.get("uri", {}).get("prefix") == CHAT_PREFIX for m in route.get("match") or []
    )


def test_chat_route_retry_policy_is_exactly_the_ruled_one() -> None:
    chat = [r for r in _routes() if _is_chat(r)]
    assert len(chat) == 1
    assert chat[0].get("retries") == CHAT_ROUTE_RETRIES


def test_chat_route_never_retries_on_a_response() -> None:
    """No status code and no response-based condition, whatever the ruling."""
    chat = next(r for r in _routes() if _is_chat(r))
    retries = chat.get("retries")
    assert retries is not None
    conditions = {c for c in str(retries.get("retryOn", "")).split(",") if c}
    assert retries["attempts"] == 0 or conditions <= BEFORE_A_RESPONSE
    assert retries["attempts"] == 0 or conditions


def test_chat_route_keeps_its_timeout() -> None:
    chat = next(r for r in _routes() if _is_chat(r))
    assert chat["timeout"] == "1800s"


def test_no_other_route_gets_a_retry_policy() -> None:
    others = [r for r in _routes() if not _is_chat(r)]
    assert len(others) == 2
    assert all("retries" not in r for r in others)
