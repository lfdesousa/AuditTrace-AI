"""LC-10a (operator ruling "A: all BFF write scopes", 2026-11-08).

Keycloak 24's legacy token exchange keeps only the requested scopes the
SUBJECT token already carries. The subject token is the user's LibreChat
login token, so the login request (``console.librechat.openidScope`` ->
``OPENID_SCOPE``) must name every scope any BFF token exchange requests, or
that exchange silently drops it and the memory-server answers 403.

The expected set is DERIVED from ``bff/*_scopes.py`` (every ``*_SCOPES``
tuple, plus the durable promote layers' single write scope), never typed
here. Each scope must also be offered to ``audittrace-librechat`` (bound by the provisioning hook, r8 O3; the realm file alone is not enough).
"""

from __future__ import annotations

import functools
import importlib
import re

from tests.test_chart_drift_guards import REPO_ROOT
from tests.test_chart_drift_guards_acl_write import SCRIPT, _client
from tests.test_chart_drift_guards_session_e2e import _chart_realm
from tests.test_console_k3s_guards import _container_env, _find, _render

LIBRECHAT = "audittrace-librechat"


@functools.lru_cache(maxsize=1)
def bff_exchange_scopes() -> frozenset[str]:
    scopes: set[str] = set()
    for path in sorted((REPO_ROOT / "bff").glob("*_scopes.py")):
        module = importlib.import_module(f"bff.{path.stem}")
        for name, value in vars(module).items():
            if re.fullmatch(r"[A-Z_]+_SCOPES", name) and isinstance(value, tuple):
                scopes.update(value)
        layers: frozenset[str] = getattr(module, "DURABLE_PROMOTE_LAYERS", frozenset())
        for layer in layers:
            scopes.add(module.promote_scope_string_for_layer(layer))
    return frozenset(scopes)


def _rendered_openid_scope() -> list[str]:
    docs = _render(console_enabled=True)
    env = _container_env(_find(docs, "Deployment", "-librechat"), "librechat")
    value = env["OPENID_SCOPE"]
    assert isinstance(value, str)
    return value.split()


def _hook_bound_to_librechat(text: str) -> set[str]:
    """Scopes the provisioning hook binds to audittrace-librechat: every
    ``for SCOPE in "${ARR[@]}"`` loop whose body binds that client."""
    bound: set[str] = set()
    for m in re.finditer(
        r'for SCOPE in "\$\{(\w+)\[@\]\}"; do\s*\n\s*bind_scope "audittrace-librechat"',
        text,
    ):
        arr = re.search(rf"^{m.group(1)}=\(\n(.*?)\n\)", text, re.M | re.S)
        assert arr is not None, m.group(1)
        bound.update(re.findall(r'"([^"]+)"', arr.group(1)))
    return bound


class TestDerivedExchangeScopes:
    def test_the_derivation_is_not_vacuous(self) -> None:
        scopes = bff_exchange_scopes()
        assert len(scopes) >= 20
        for must in (
            "memory:files:write",
            "memory:session:write",
            "memory:episodic:write",
        ):
            assert must in scopes
        assert not any(s.startswith(("audittrace:", "memory:corpus")) for s in scopes)


class TestLoginRequestCoversEveryExchange:
    def test_openid_scope_is_a_superset_of_every_bff_exchange_scope(self) -> None:
        rendered = set(_rendered_openid_scope())
        missing = sorted(bff_exchange_scopes() - rendered)
        assert not missing, (
            f"console.librechat.openidScope lacks {missing}: Keycloak 24's token "
            "exchange would drop them and the write would 403"
        )

    def test_the_login_baseline_is_kept(self) -> None:
        rendered = set(_rendered_openid_scope())
        assert {"openid", "profile", "email", "offline_access"} <= rendered

    def test_every_login_scope_beyond_the_baseline_is_an_exchange_scope(self) -> None:
        """No stray extra scope (least privilege): the login request is the
        baseline plus exactly the derived set."""
        rendered = set(_rendered_openid_scope())
        extra = (
            rendered
            - bff_exchange_scopes()
            - {
                "openid",
                "profile",
                "email",
                "offline_access",
            }
        )
        assert not extra, f"unexpected scopes in OPENID_SCOPE: {sorted(extra)}"


# The four core read scopes every realm has carried since its first import:
# an already-imported realm has them as defaults for audittrace-librechat, so
# the hook never needed to bind them. Every OTHER exchange scope reaches an
# existing realm ONLY through the provisioning hook (the realm file is
# first-boot-only), so it must be hook-bound. Explicit, not derived: a new
# scope never silently joins this set.
BASELINE_REALM_DEFAULT_READS = frozenset(
    {
        "memory:conversational:read-own",
        "memory:episodic:read",
        "memory:procedural:read",
        "memory:semantic:read",
    }
)


class TestHookOffersEveryExchangeScopeToLibrechat:
    """LC-10a review r8, O3. The old form accepted realm-file OR hook, so a
    scope present only in the realm file passed here and then 403'd live
    (an existing realm is never re-imported). Neuter: unbind a write scope
    from the hook while the realm file still lists it -> RED."""

    def test_every_exchange_scope_is_hook_bound_or_a_baseline_read(self) -> None:
        bound = _hook_bound_to_librechat(SCRIPT.read_text(encoding="utf-8"))
        unbound = sorted(bff_exchange_scopes() - bound - BASELINE_REALM_DEFAULT_READS)
        assert not unbound, (
            f"not bound to {LIBRECHAT} by the provisioning hook: {unbound} "
            "(an already-imported realm only gets new bindings from the hook)"
        )

    def test_the_baseline_exemption_is_real_and_not_widened(self) -> None:
        client = _client(_chart_realm(True), LIBRECHAT)
        assert client is not None
        defaults = set(client.get("defaultClientScopes", []))
        assert BASELINE_REALM_DEFAULT_READS <= defaults
        assert BASELINE_REALM_DEFAULT_READS <= bff_exchange_scopes()
        assert all(
            s.endswith(("read", "read-own")) for s in BASELINE_REALM_DEFAULT_READS
        )

    def test_the_realm_file_still_offers_every_scope_on_a_fresh_import(self) -> None:
        client = _client(_chart_realm(True), LIBRECHAT)
        assert client is not None
        offered = set(client.get("defaultClientScopes", [])) | set(
            client.get("optionalClientScopes", [])
        )
        unoffered = sorted(bff_exchange_scopes() - offered)
        assert not unoffered, f"realm file does not offer {unoffered} to {LIBRECHAT}"
