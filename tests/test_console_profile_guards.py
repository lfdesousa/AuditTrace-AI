"""M3-WU-3 — the LibreChat `console` profile config guards.

Rule 1 (Acceptance §5) of the ratified spec
(specs/2026-08-29-SPEC-m3-wu3-librechat-console-profile.md, private repo):
a test asserts the `console` profile wires librechat→BFF only (no LLM/host
route), B4 cloud features are off, and registration is disabled — neuter
any of the underlying config and the corresponding test below goes RED.

Hermetic: parses ``docker-compose.edge.yml`` and
``config/compose/librechat/librechat.yaml`` as plain YAML text. No cluster,
no `docker compose`, no subprocess — same discipline as
``tests/test_compose_drift.py``.

Drift classes covered:

* **D4 — LibreChat routes to the BFF only.** The custom endpoint's
  ``baseURL`` is the ONLY chat-completions route LibreChat carries, and it
  targets `bff`, never the front door. LibreChat's own service definition
  carries no orchestrator/`/v1`/Keycloak-admin env var — only the BFF does.
  (LibreChat's ``extra_hosts`` route to the front door is a SEPARATE,
  legitimate OIDC-login concern — D2 wires LibreChat as a direct OIDC
  relying party — asserted here to be exactly the one entry needed for
  that, not a silent extra route to anything else.)
* **B4 — cloud features off.** ``ENDPOINTS=custom`` (no default
  openAI/anthropic/google/agents/assistants/bedrock endpoints regardless of
  a stray provider key), no provider/search/speech API-key env vars
  anywhere in the service, and ``librechat.yaml`` carries no ``webSearch``/
  ``speech`` config block.
* **Registration disabled.** ``ALLOW_REGISTRATION=false`` +
  ``ALLOW_EMAIL_LOGIN=false`` — the native self-serve signup/login form is
  off; only the pre-registered Keycloak realm can authenticate.
* **No shared/static credential (D4).** The custom endpoint's ``apiKey`` is
  the native ``user_provided`` sentinel (never a literal string), and the
  BFF's confidential-client secret is wired via a fail-closed
  ``${VAR:?...}`` compose interpolation — never a literal default.

Anchors: ``feedback_no_more_drifts``, ``feedback_vacuous_neuter_test_antipattern``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.edge.yml"
LIBRECHAT_YAML = REPO_ROOT / "config" / "compose" / "librechat" / "librechat.yaml"

_PROVIDER_AND_FEATURE_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_KEY",
    "GOOGLE_SEARCH_API_KEY",
    "AZURE_API_KEY",
    "BEDROCK_AWS_ACCESS_KEY_ID",
    "SERPER_API_KEY",
    "FIRECRAWL_API_KEY",
    "JINA_API_KEY",
    "STT_API_KEY",
    "TTS_API_KEY",
)


def _compose_doc() -> dict:
    with COMPOSE_FILE.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _librechat_yaml_doc() -> dict:
    with LIBRECHAT_YAML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _env_list(service: dict) -> list[str]:
    """Normalize a compose service's `environment:` (this repo always
    writes the list-of-"KEY=VALUE" form) into that list, failing loudly if
    a future edit switches it to the mapping form this helper doesn't
    handle — better a clear KeyError here than a guard that silently
    stops checking anything."""
    env = service["environment"]
    assert isinstance(env, list), (
        "expected docker-compose.edge.yml's `environment:` blocks in the "
        "list-of-KEY=VALUE form (this repo's convention) — got a mapping; "
        "update this helper if that convention changed."
    )
    return env


class TestConsoleProfileServicesPresent:
    """All three D1-D3 services exist and are gated by the `console`
    profile — a plain `docker compose up` (no `--profile`) must not bring
    any of them up."""

    def test_bff_present_and_gated(self) -> None:
        svc = _compose_doc()["services"]["bff"]
        assert svc.get("profiles") == ["console"], (
            f"bff service profiles={svc.get('profiles')!r} — must be gated "
            "by ['console'] only, or a plain `docker compose up` (no "
            "--profile) would bring it up unexpectedly."
        )

    def test_librechat_present_and_gated(self) -> None:
        svc = _compose_doc()["services"]["librechat"]
        assert svc.get("profiles") == ["console"]

    def test_librechat_mongodb_present_and_gated(self) -> None:
        svc = _compose_doc()["services"]["librechat-mongodb"]
        assert svc.get("profiles") == ["console"]

    def test_librechat_mongodb_has_no_published_port(self) -> None:
        """D3 — MongoDB carries NO external port; only the two chat-facing
        services (librechat itself) are host-reachable."""
        svc = _compose_doc()["services"]["librechat-mongodb"]
        assert "ports" not in svc, (
            f"librechat-mongodb publishes ports ({svc.get('ports')!r}) — "
            "D3 requires no external port; drop the `ports:` block."
        )


class TestLibreChatRoutesToBffOnly:
    """D4 — the ONLY chat-completions route LibreChat carries points at
    `bff`; nothing in its own service definition names the front door's
    `/v1` or Keycloak's admin surface."""

    def test_custom_endpoint_baseurl_targets_bff_only(self) -> None:
        custom_endpoints = _librechat_yaml_doc()["endpoints"]["custom"]
        assert len(custom_endpoints) == 1, (
            f"expected exactly one custom endpoint, got {len(custom_endpoints)} "
            "— D2 wires a single 'AuditTrace' endpoint."
        )
        base_url = custom_endpoints[0]["baseURL"]
        assert base_url == "http://bff:8080/v1", (
            f"custom endpoint baseURL={base_url!r} — must target the `bff` "
            "compose service, never the front door directly (D4: the BFF "
            "is the only path to /v1)."
        )

    def test_librechat_service_carries_no_orchestrator_or_admin_route(self) -> None:
        env = _env_list(_compose_doc()["services"]["librechat"])
        joined = "\n".join(env)
        for forbidden in ("/v1/chat/completions", "ORCHESTRATOR", "/admin"):
            assert forbidden not in joined, (
                f"librechat service environment contains {forbidden!r} — "
                "D4 forbids LibreChat holding any direct /v1 or "
                "Keycloak-admin route; only `bff` may."
            )

    def test_librechat_extra_hosts_is_exactly_the_oidc_discovery_route(self) -> None:
        """LibreChat DOES need one route to the front door — D2 wires it
        as a direct OIDC relying party (discovery/JWKS/token exchange for
        LOGIN, not chat). This asserts it is exactly that one entry, not a
        silently-added second route to anything else."""
        svc = _compose_doc()["services"]["librechat"]
        assert svc.get("extra_hosts") == ["audittrace.local:host-gateway"], (
            f"librechat extra_hosts={svc.get('extra_hosts')!r} — expected "
            "exactly the one audittrace.local OIDC-discovery route."
        )

    def test_bff_is_the_only_service_with_orchestrator_base_url(self) -> None:
        bff_env = "\n".join(_env_list(_compose_doc()["services"]["bff"]))
        assert "ORCHESTRATOR_BASE_URL" in bff_env, (
            "bff service is missing AUDITTRACE_BFF_ORCHESTRATOR_BASE_URL — "
            "it is the only service that should hold the /v1 route."
        )


class TestB4CloudFeaturesOff:
    """Every B4 lever, each independently falsifiable."""

    def test_endpoints_restricted_to_custom(self) -> None:
        env = _env_list(_compose_doc()["services"]["librechat"])
        assert "ENDPOINTS=custom" in env, (
            "librechat service is missing ENDPOINTS=custom — without it, "
            "stock LibreChat's built-in openAI/google/anthropic/agents/"
            "assistants/bedrock endpoints become selectable if any of "
            "their provider env vars are ever set."
        )

    def test_no_provider_or_feature_api_keys_configured(self) -> None:
        env = _env_list(_compose_doc()["services"]["librechat"])
        joined = "\n".join(env)
        for var in _PROVIDER_AND_FEATURE_ENV_VARS:
            assert f"{var}=" not in joined, (
                f"librechat service sets {var} — B4 requires no cloud "
                "provider / web-search / speech API key anywhere in this "
                "profile."
            )

    def test_librechat_yaml_has_no_websearch_or_speech_config(self) -> None:
        cfg = _librechat_yaml_doc()
        assert "webSearch" not in cfg, (
            "librechat.yaml declares a `webSearch:` block — B4 requires "
            "web search left unconfigured (absence-is-disable), not merely "
            "unused."
        )
        assert "speech" not in cfg, (
            "librechat.yaml declares a `speech:` block — B4 requires "
            "TTS/STT left unconfigured."
        )


class TestRegistrationDisabled:
    def test_allow_registration_false(self) -> None:
        env = _env_list(_compose_doc()["services"]["librechat"])
        assert "ALLOW_REGISTRATION=false" in env, (
            "librechat service must set ALLOW_REGISTRATION=false (spec D2) "
            "— the native self-serve signup form must stay off."
        )

    def test_allow_email_login_false(self) -> None:
        env = _env_list(_compose_doc()["services"]["librechat"])
        assert "ALLOW_EMAIL_LOGIN=false" in env, (
            "librechat service must set ALLOW_EMAIL_LOGIN=false — OIDC "
            "must be the only login path (spec D2: 'OIDC-only login')."
        )


class TestNoSharedStaticCredential:
    """D4 — 'No shared/static API key anywhere — identity is always the
    exchanged per-user token.'"""

    def test_custom_endpoint_apikey_is_user_provided_sentinel(self) -> None:
        custom_endpoints = _librechat_yaml_doc()["endpoints"]["custom"]
        api_key = custom_endpoints[0]["apiKey"]
        assert api_key == "user_provided", (
            f"custom endpoint apiKey={api_key!r} — must be the native "
            "'user_provided' sentinel (per-user, LibreChat's own "
            "encrypted-per-user-key mechanism), never a literal static "
            "value shared across every caller."
        )

    def test_bff_exchange_secret_is_fail_closed_not_a_literal_default(self) -> None:
        raw = COMPOSE_FILE.read_text(encoding="utf-8")
        assert (
            "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET=${AUDITTRACE_BFF_CLIENT_SECRET:?"
            in raw
        ), (
            "the bff service's exchange-secret line must use the "
            "fail-closed `${AUDITTRACE_BFF_CLIENT_SECRET:?...}` compose "
            "interpolation (a missing shell var is a hard compose-time "
            "error) — never a literal secret or a `:-<default>` fallback."
        )
