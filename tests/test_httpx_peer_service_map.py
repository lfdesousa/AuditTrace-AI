"""Unit tests for the peer.service port → name map used on outbound HTTPX.

The map is derived from Settings at startup; breaking it silently
collapses edges in Tempo's service graph (e.g., Langfuse + the 3
LLMs all shown as a single 192.168.1.231 edge). These tests lock the
mapping so changes to the envelope surface in a test, not in
production dashboards.
"""

from __future__ import annotations

from types import SimpleNamespace

from audittrace.server import _build_httpx_peer_service_map


def _settings(**kwargs: str | None) -> SimpleNamespace:
    """Build a minimal settings-like stub with only the URL attrs we care about."""
    return SimpleNamespace(
        llama_url=kwargs.get("llama_url"),
        embed_url=kwargs.get("embed_url"),
        summarizer_url=kwargs.get("summarizer_url"),
        langfuse_host=kwargs.get("langfuse_host"),
    )


def test_map_includes_all_four_known_destinations() -> None:
    mapping = _build_httpx_peer_service_map(
        _settings(
            llama_url="http://host.docker.internal:11435",
            embed_url="http://host.docker.internal:11436",
            summarizer_url="http://host.docker.internal:11437",
            langfuse_host="http://192.168.1.231:3000",
        )
    )
    assert mapping == {
        11435: "qwen-chat-llm",
        11436: "nomic-embed-server",
        11437: "mistral-summariser-llm",
        3000: "langfuse",
    }


def test_map_skips_missing_urls() -> None:
    mapping = _build_httpx_peer_service_map(
        _settings(llama_url="http://host.docker.internal:11435")
    )
    assert mapping == {11435: "qwen-chat-llm"}


def test_map_skips_url_without_explicit_port() -> None:
    # urlparse returns None for the port when the scheme's default is implicit.
    mapping = _build_httpx_peer_service_map(_settings(llama_url="http://some-llm-host"))
    assert mapping == {}


def test_map_tolerates_langfuse_on_alternate_port() -> None:
    # Langfuse in k8s lives at host port 3000; docker-compose picks the same.
    # Any re-hosting (e.g. :3000 → :3001) should propagate naturally.
    mapping = _build_httpx_peer_service_map(
        _settings(langfuse_host="http://127.0.0.1:3001")
    )
    assert mapping == {3001: "langfuse"}
