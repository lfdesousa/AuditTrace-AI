"""Tests for bff/console_promote_scopes.py — the config-driven durable
promote scope helper (M3 Sovereign-Attach WU-4).

The load-bearing test class is ``TestNeverSessionOrAdminScope`` — the
falsifiable proof that the promote exchange can never carry the session
scope, the broad Souvenirs set, or ``audittrace:admin``. Widen
``DURABLE_PROMOTE_LAYERS`` to include ``"session"`` (or drop the
``ValueError`` guard in :func:`promote_scope_string_for_layer`) and
these tests go RED.
"""

from __future__ import annotations

import pytest

from bff.console_promote_scopes import (
    DURABLE_PROMOTE_LAYERS,
    promote_scope_string_for_layer,
)


class TestDurablePromoteLayers:
    def test_exact_expected_set(self) -> None:
        assert DURABLE_PROMOTE_LAYERS == frozenset({"episodic", "semantic"})

    def test_session_not_a_durable_promote_target(self) -> None:
        assert "session" not in DURABLE_PROMOTE_LAYERS

    def test_procedural_not_a_durable_promote_target(self) -> None:
        """The EPIC decision text names only episodic/semantic — not
        procedural, even though procedural is itself durable/S3-backed
        for other routes."""
        assert "procedural" not in DURABLE_PROMOTE_LAYERS

    def test_conversational_not_a_durable_promote_target(self) -> None:
        assert "conversational" not in DURABLE_PROMOTE_LAYERS


class TestPromoteScopeStringForLayer:
    def test_episodic_maps_to_exact_scope(self) -> None:
        assert promote_scope_string_for_layer("episodic") == "memory:episodic:write"

    def test_semantic_maps_to_exact_scope(self) -> None:
        assert promote_scope_string_for_layer("semantic") == "memory:semantic:write"

    def test_result_is_a_single_scope_no_extras(self) -> None:
        result = promote_scope_string_for_layer("episodic")
        assert " " not in result
        assert len(result.split(" ")) == 1


class TestNeverSessionOrAdminScope:
    """The falsifiable non-negotiable invariant: the promote exchange
    must never be able to request the session scope, any scope from the
    broad Souvenirs set, or ``audittrace:admin``."""

    @pytest.mark.parametrize("layer", sorted(DURABLE_PROMOTE_LAYERS))
    def test_never_session_scope(self, layer: str) -> None:
        assert promote_scope_string_for_layer(layer) != "memory:session:write"

    @pytest.mark.parametrize("layer", sorted(DURABLE_PROMOTE_LAYERS))
    def test_never_admin_scope(self, layer: str) -> None:
        assert promote_scope_string_for_layer(layer) != "audittrace:admin"

    @pytest.mark.parametrize("layer", sorted(DURABLE_PROMOTE_LAYERS))
    def test_never_the_full_broad_scope_set(self, layer: str) -> None:
        """The promote exchange requests ONE scope, never the memory
        route's full seven-scope set — the requested-scope STRING sent
        to Keycloak must never equal ``MEMORY_SCOPE_STRING`` wholesale
        (individual scope NAMES legitimately overlap: the durable write
        scopes exist in both sets, since the Souvenirs panel already has
        broad durable access — the invariant is about the SHAPE of the
        request, not the scope names)."""
        from bff.memory_scopes import MEMORY_SCOPE_STRING

        result = promote_scope_string_for_layer(layer)
        assert result != MEMORY_SCOPE_STRING

    @pytest.mark.parametrize("layer", sorted(DURABLE_PROMOTE_LAYERS))
    def test_never_intersects_ingest_scope_string(self, layer: str) -> None:
        from bff.console_files_scopes import INGEST_SCOPE_STRING

        result = promote_scope_string_for_layer(layer)
        assert result != INGEST_SCOPE_STRING


class TestUnknownLayerRejectedFailClosed:
    """A non-durable / unknown ``target_layer`` must never silently
    produce SOME scope string — it must raise, fail-closed."""

    @pytest.mark.parametrize(
        "bad_layer", ["session", "conversational", "procedural", "", "admin"]
    )
    def test_non_durable_layer_raises(self, bad_layer: str) -> None:
        with pytest.raises(ValueError, match="target_layer must be one of"):
            promote_scope_string_for_layer(bad_layer)
