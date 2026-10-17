"""#370 silent-admin lock — ``scripts/mint-dev-jwt.sh`` must request an
EXPLICIT, minimal, read-only scope so a dev JWT can never silently
inherit a future admin default bound to the ``audittrace-dev`` Keycloak
client.

Background (see ``docs/architecture/sequence-oauth2-flow.md`` and the
2026-08-11 ratified spec at
``~/work/audittrace-private/specs/2026-08-11-SPEC-370-keycloak-scope-decision-and-mint-dev-jwt-lock.md``):
#370 found the live realm had drifted so ``audittrace-dev`` carried
``audittrace:admin`` as a **default** scope while the committed
``keycloak/realm-audittrace.json`` never declared it. Because
``mint-dev-jwt.sh`` sent no ``scope`` request parameter, every dev token
silently inherited whatever the client's live defaults happened to be —
admin included. The live drift was corrected, and the committed
``audittrace-dev`` definition is already read-only-correct, but relying
on "the client's current defaults happen to be safe" is not a lock: a
future re-provision could re-add an admin default and nothing here
would notice. This module is that lock.

``TestMintDevJwtRequestsExplicitScope`` pins the *mechanism* (the script
sends a ``scope=`` parameter at all — omitting it is the #370 bug
verbatim). ``TestMintDevJwtDefaultScopeIsReadOnly`` pins the *content*
of that scope (no admin-ish scope, reusing the exact same
``is_admin_scope`` predicate production uses to set
``UserContext.is_admin``, so this guard cannot silently drift out of
step with what "admin" means at runtime). ``TestAuditTraceDevClientStaysReadOnly``
pins the committed realm definition itself (spec decision #2 — the
dev-client re-provision-hygiene lock).

Falsifiable: deleting or commenting out the ``-d "scope=${SCOPE}"``
line in ``scripts/mint-dev-jwt.sh`` makes
``test_curl_request_carries_scope_parameter`` fail (RED); appending
``audittrace:admin`` (or ``memory:admin``, or any ``admin:*`` scope) to
the script's ``SCOPE`` default makes
``test_default_scope_carries_no_admin_scope`` fail (RED). Both were
run against a manually neutered copy of the script during development
and observed RED before being reverted — see the build record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from audittrace.identity import is_admin_scope

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "mint-dev-jwt.sh"
REALM_PATH = REPO_ROOT / "keycloak" / "realm-audittrace.json"


def _script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _default_scope_string() -> str:
    """Extract the ``SCOPE`` default from the script's
    ``SCOPE="${SCOPE:-...}"`` assignment.

    Raises (as a test failure via ``AssertionError``) if the assignment
    is missing entirely — the #370 fix is precisely that this default
    exists and is non-empty.
    """
    match = re.search(r'SCOPE="\$\{SCOPE:-([^}]*)\}"', _script_text())
    if match is None:
        raise AssertionError(
            "scripts/mint-dev-jwt.sh no longer sets a SCOPE default via "
            'SCOPE="${SCOPE:-...}" — #370 requires an explicit, minimal, '
            "read-only default so a dev token can never silently inherit "
            "a future admin scope."
        )
    return match.group(1)


class TestMintDevJwtRequestsExplicitScope:
    """The token request itself must carry ``scope=`` — the #370 bug was
    literally the absence of this parameter."""

    def test_curl_request_carries_scope_parameter(self) -> None:
        """``curl`` must send ``-d "scope=${SCOPE}"`` to the Keycloak
        token endpoint.

        Falsifiable: remove this ``-d`` line from ``mint-dev-jwt.sh`` and
        this assertion goes RED — that line, restored verbatim, is the
        entire #370 fix.
        """
        text = _script_text()
        assert re.search(r'-d\s+"scope=\$\{SCOPE\}"', text), (
            "scripts/mint-dev-jwt.sh no longer sends an explicit "
            '`-d "scope=${SCOPE}"` parameter in its curl call to the '
            "Keycloak token endpoint. Without it, a minted dev token "
            "inherits whatever scopes the audittrace-dev client happens "
            "to carry live by default — this is the exact #370 "
            "silent-admin footgun."
        )

    def test_scope_variable_is_assigned_before_the_curl_call(self) -> None:
        """The ``SCOPE`` assignment must precede the ``curl`` invocation
        that references it — a ``scope=${SCOPE}`` referencing an
        as-yet-unset variable would silently send an empty scope, which
        Keycloak (per RFC 6749 §3.3) treats as "grant every scope
        assigned to the client" — i.e. right back to the #370 bug."""
        text = _script_text()
        scope_assign = text.index('SCOPE="${SCOPE:-')
        curl_scope_use = text.index('-d "scope=${SCOPE}"')
        assert 0 <= scope_assign < curl_scope_use, (
            "SCOPE must be assigned before the curl call references it, "
            "or the request silently sends scope= empty (Keycloak then "
            "grants every scope the client is assigned — the #370 bug)."
        )


class TestMintDevJwtDefaultScopeIsReadOnly:
    """The ``SCOPE`` default's *content* must be minimal and read-only."""

    def test_default_scope_is_non_empty(self) -> None:
        assert _default_scope_string().split(), (
            "mint-dev-jwt.sh's SCOPE default is empty — an empty `scope=` "
            "request is treated by Keycloak as 'grant every scope "
            "assigned to the client', which is exactly the #370 bug."
        )

    def test_default_scope_carries_no_admin_scope(self) -> None:
        """Reuses ``audittrace.identity.is_admin_scope`` — the SAME
        predicate production uses to set ``UserContext.is_admin`` — so
        this guard cannot drift out of step with what "admin" means at
        runtime.

        Falsifiable: append ``audittrace:admin`` (or ``memory:admin``, or
        any ``admin:*`` scope) to the SCOPE default in
        ``scripts/mint-dev-jwt.sh`` and this assertion goes RED.
        """
        scopes = _default_scope_string().split()
        assert not is_admin_scope(scopes), (
            f"mint-dev-jwt.sh's default SCOPE {scopes!r} contains a scope "
            "that audittrace.identity.is_admin_scope recognises as "
            "admin-ish — this is exactly the #370 silent-admin footgun "
            "the explicit scope request exists to close."
        )

    def test_default_scope_excludes_write_triggering_reindex_scope(self) -> None:
        """``audittrace:index`` triggers a semantic-store reindex (see
        ``audittrace.auth.ALL_SCOPES["audittrace:index"]``) — a write-side
        action, not a read. The #370 ratified decision is "minimal
        READ-ONLY", so it must be excluded even though the
        ``audittrace-dev`` client is granted it as a default."""
        from audittrace.auth import ALL_SCOPES

        assert "audittrace:index" in ALL_SCOPES, (
            "audittrace:index no longer exists in ALL_SCOPES — if it was "
            "renamed or removed, update this test and the SCOPE default "
            "together rather than deleting the check."
        )
        scopes = set(_default_scope_string().split())
        assert "audittrace:index" not in scopes, (
            "mint-dev-jwt.sh's default SCOPE requests audittrace:index, a "
            "write-triggering reindex scope — the #370 ratified decision "
            "is a minimal READ-ONLY default."
        )

    def test_default_scope_is_subset_of_audittrace_dev_declared_scopes(self) -> None:
        """The requested scope must never ask for more than
        ``audittrace-dev`` actually carries. Keycloak silently drops
        unassigned scopes rather than erroring, so requesting extra would
        not grant anything — but it would make the "minimal" claim a
        lie."""
        realm = json.loads(REALM_PATH.read_text(encoding="utf-8"))
        dev_client = next(
            c for c in realm["clients"] if c.get("clientId") == "audittrace-dev"
        )
        declared = set(dev_client.get("defaultClientScopes", [])) | set(
            dev_client.get("optionalClientScopes", [])
        )
        requested = set(_default_scope_string().split())
        extra = requested - declared
        assert not extra, (
            f"mint-dev-jwt.sh requests scope(s) {sorted(extra)} that "
            "audittrace-dev is not even assigned in keycloak/"
            "realm-audittrace.json — Keycloak would silently drop them, "
            "but the SCOPE default should only ever list scopes the "
            "client actually carries."
        )


class TestAuditTraceDevClientStaysReadOnly:
    """#370 decision #2 — dev-client re-provision hygiene lock: the
    COMMITTED ``audittrace-dev`` definition (the source ``--import-realm``
    uses on first boot, and what any realm re-import returns to) must
    never declare an admin-ish scope, in either scope set."""

    def _dev_client(self) -> dict[str, object]:
        realm = json.loads(REALM_PATH.read_text(encoding="utf-8"))
        for client in realm["clients"]:
            if client.get("clientId") == "audittrace-dev":
                return client
        raise AssertionError(
            "audittrace-dev is missing from keycloak/realm-audittrace.json"
        )

    def test_committed_dev_client_has_no_admin_scope(self) -> None:
        """Falsifiable: add ``audittrace:admin`` to either
        ``defaultClientScopes`` or ``optionalClientScopes`` on
        ``audittrace-dev`` in ``keycloak/realm-audittrace.json`` and this
        goes RED — a realm re-import (first boot, or disaster recovery)
        would then re-introduce the exact #370 drift."""
        client = self._dev_client()
        both = list(client.get("defaultClientScopes", [])) + list(
            client.get("optionalClientScopes", [])
        )
        offenders = [s for s in both if is_admin_scope([s])]
        assert not offenders, (
            f"audittrace-dev was granted admin-ish scope(s) {offenders} in "
            "keycloak/realm-audittrace.json — this is the exact drift "
            "#370 found on the live realm; a re-import would now bake it "
            "into every fresh cluster."
        )

    def test_committed_dev_client_has_no_write_scope(self) -> None:
        """The dev client is documented (script header,
        ``docs/architecture/sequence-oauth2-flow.md``) as read-only.
        Pin that as a structural property, not just prose."""
        client = self._dev_client()
        both = list(client.get("defaultClientScopes", [])) + list(
            client.get("optionalClientScopes", [])
        )
        write_scopes = [s for s in both if s.endswith(":write")]
        assert not write_scopes, (
            f"audittrace-dev was granted write scope(s) {write_scopes} — "
            "the dev client is documented as read-only; a write scope "
            "here contradicts scripts/mint-dev-jwt.sh's own header "
            "comment and docs/architecture/sequence-oauth2-flow.md."
        )
