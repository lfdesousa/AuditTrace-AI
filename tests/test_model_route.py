"""Tests for FLEET-ROUTE's model-field resolver (routes/_model_route.py).

Falsifiable per-invariant (SPEC 2026-08-06-SPEC-fleet-route-model-upstream):
  1 default-unchanged   2 route-match   3 unknown/empty -> fallback
  4 first-match-wins + case-insensitive prefix   (5/6 covered in
  test_chat_proxy.py / test_memory_tool_loop.py, where the resolver is
  wired through the real chat path).
"""

from audittrace.routes._model_route import resolve_chat_upstream, upstream_host

_QWEN_URL = "http://host.docker.internal:11435/v1"
_MISTRAL_URL = "http://host.docker.internal:11438/v1"
_ROUTES = {"qwen": _QWEN_URL, "mistral": _MISTRAL_URL}


class TestResolveChatUpstreamDefaultUnchanged:
    """Invariant 1 — empty model_routes byte-preserves today's single
    -upstream behaviour, for ANY requested model."""

    def test_empty_routes_dict_always_returns_default(self):
        assert resolve_chat_upstream("qwen3.6", {}, _QWEN_URL) == _QWEN_URL

    def test_empty_routes_dict_returns_default_for_unrelated_model(self):
        assert resolve_chat_upstream("gpt-4o", {}, _QWEN_URL) == _QWEN_URL

    def test_empty_routes_dict_returns_default_for_empty_model(self):
        assert resolve_chat_upstream("", {}, _QWEN_URL) == _QWEN_URL


class TestResolveChatUpstreamRouteMatch:
    """Invariant 2 — the FLEET-ROUTE laptop map: builder (Mistral) and
    reviewer (Qwen) each land on their own port."""

    def test_mistral_prefixed_model_routes_to_mistral(self):
        assert (
            resolve_chat_upstream("mistral-small-3.1-24b", _ROUTES, _QWEN_URL)
            == _MISTRAL_URL
        )

    def test_qwen_prefixed_model_routes_to_qwen(self):
        assert resolve_chat_upstream("qwen3.6-35b-a3b", _ROUTES, _QWEN_URL) == _QWEN_URL


class TestResolveChatUpstreamFallback:
    """Invariant 3 — unknown or empty model never crashes, always falls
    back to *default* (never returns None / raises)."""

    def test_unknown_model_falls_back_to_default(self):
        assert resolve_chat_upstream("gpt-4o", _ROUTES, _QWEN_URL) == _QWEN_URL

    def test_empty_model_falls_back_to_default(self):
        assert resolve_chat_upstream("", _ROUTES, _QWEN_URL) == _QWEN_URL

    def test_none_like_falsy_model_falls_back_to_default(self):
        # payload.get("model", "") on a request with no `model` field
        # yields "" — never None — but guard the falsy branch directly
        # too so a future caller passing None can't crash the resolver.
        assert resolve_chat_upstream(None, _ROUTES, _QWEN_URL) == _QWEN_URL  # type: ignore[arg-type]


class TestResolveChatUpstreamFirstMatchWinsCaseInsensitive:
    """Invariant 4 — first-match-wins (dict insertion order) and prefix
    matching is case-insensitive on both the model and the route key."""

    def test_first_match_wins_when_two_prefixes_could_match(self):
        routes = {"mistral": _MISTRAL_URL, "mistral-small": "http://other:1/v1"}
        # "mistral" is inserted first, so it wins even though
        # "mistral-small" is a more specific match further down.
        assert (
            resolve_chat_upstream("mistral-small-3.1-24b", routes, _QWEN_URL)
            == _MISTRAL_URL
        )

    def test_case_insensitive_model_matches_lowercase_route_key(self):
        assert (
            resolve_chat_upstream("MISTRAL-Small-3.1-24B", _ROUTES, _QWEN_URL)
            == _MISTRAL_URL
        )

    def test_case_insensitive_route_key_matches_lowercase_model(self):
        routes = {"MISTRAL": _MISTRAL_URL}
        assert resolve_chat_upstream("mistral-small", routes, _QWEN_URL) == _MISTRAL_URL


class TestUpstreamHost:
    """``upstream_host`` — the additive trace-attribute helper
    (SPEC invariant 6)."""

    def test_extracts_netloc_from_base_url(self):
        assert upstream_host(_MISTRAL_URL) == "host.docker.internal:11438"

    def test_extracts_netloc_from_full_chat_completions_url(self):
        assert (
            upstream_host(_MISTRAL_URL.rstrip("/") + "/chat/completions")
            == "host.docker.internal:11438"
        )

    def test_falls_back_to_raw_string_when_unparseable(self):
        # No scheme/netloc at all — urlsplit's netloc is empty, so the
        # helper falls back to returning the input unchanged rather than
        # silently emitting "" (best-effort, never raises).
        assert upstream_host("not-a-url") == "not-a-url"
