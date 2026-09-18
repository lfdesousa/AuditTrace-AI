"""FLEET-ROUTE — model-field routing for the audited chat path.

Resolves which LLM upstream base URL a chat request should be forwarded
to, keyed off the OpenAI-compatible ``model`` field already present on
every ``/v1/chat/completions`` request. This lets fleet agents (builder
on Mistral, reviewer on Qwen — see
``project_local_sovereign_fleet_opencode``) pick their engine by role
while every call still passes through the SAME audited front door
(telemetry-by-AuditTrace, ADR-025/ADR-049).

Generalises the existing ``summarizer_url`` per-role-upstream precedent
(``config.py`` — "falls back to llama_url when the dedicated endpoint is
not running so the feature degrades rather than crashes") to the
interactive chat path, rather than inventing a parallel mechanism.

Kept as a standalone module — deliberately pure and side-effect free —
so it carries its own ≥90% per-file coverage independent of the much
larger ``chat.py``/``_memory_tool_loop.py`` call sites that consume it.

**Disclosed, deliberately deferred gap (M3 WU-4 review round 1, F11,
non-blocking).** Every ``chat.py`` call site sends ``settings.
llama_api_key`` (ONE shared key) regardless of which upstream this
function resolves to — so a ``model_routes`` entry pointing at a
different host, including a hypothetical third-party upstream, would
receive the SAME bearer credential the primary ``llama_url`` endpoint
uses. Today's actual ``model_routes`` values are other locked-down
endpoints on this SAME host and SAME trust boundary (``llama-fleet``
``:11438``, ``llama-mistral-server`` ``:11437`` — see
``scripts/systemd/llama-fleet.service`` / ``llama-mistral-server.
service``, also brought under the WU-4 locks), so there is no live
credential-leak today. Per-route keys (~3 new ``Settings`` fields plus
rewiring every ``llama_auth_headers(settings.llama_api_key)`` call site
to look up the key for ``resolve_chat_upstream``'s OWN resolved host)
is a real, judged, ~day-sized follow-up — deliberately NOT bundled into
this security-only fix round to keep it reviewable, not because the
class of risk is dismissed. Track before ``model_routes`` is ever
pointed at a host outside this box's trust boundary.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def resolve_chat_upstream(model: str, routes: dict[str, str], default: str) -> str:
    """Resolve the base ``/v1`` upstream URL to use for *model*.

    Iterates *routes* in insertion order and returns the URL for the
    first key whose value is a case-insensitive prefix of *model* — so
    a caller who sets ``{"mistral": "...", "mistral-small": "..."}``
    gets first-match-wins, most-specific-first behaviour by ordering
    the dict accordingly (SPEC invariant 4).

    Falls back to *default* (``settings.llama_url``) when *model* is
    empty/falsy or matches no configured prefix — this is what
    byte-preserves today's single-upstream behaviour when
    ``model_routes`` is the empty dict (SPEC invariant 1:
    default-unchanged; SPEC invariant 3: unknown/empty model never
    crashes, it just falls back).
    """
    if model:
        model_lower = model.lower()
        for prefix, url in routes.items():
            if model_lower.startswith(prefix.lower()):
                return url
    return default


def upstream_host(url: str) -> str:
    """``netloc`` (``host[:port]``) of *url*.

    Used to populate the additive ``audittrace.upstream.host`` trace
    attribute (SPEC invariant 6) so a reviewer can see which engine
    actually served a call without having to reconstruct it from the
    full URL. Falls back to the raw *url* when it has no parseable
    netloc (e.g. a malformed override) so this never raises — trace
    attribution is best-effort, not a correctness gate.
    """
    return urlsplit(url).netloc or url
