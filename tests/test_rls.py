"""Unit tests for the Phase 4 RLS plumbing (DESIGN §16 Phase 4).

This file covers the ``sovereign_memory.db.rls`` module — the ContextVar
that carries ``app.current_user_id`` through a request and the SQLAlchemy
``after_begin`` event listener that emits ``SET LOCAL`` (via
``set_config``) at the start of every Postgres transaction.

The listener's production value depends on a real PostgreSQL engine
(RLS policies do not exist on SQLite). These tests cover the **unit
contract**:

  - ContextVar set/get/reset round-trip
  - The listener silently skips on non-Postgres dialects so the
    existing SQLite-backed test suite keeps working unchanged
  - The listener executes ``set_config(..., true)`` exactly once per
    transaction begin when a user_id is set
  - Setting the ContextVar to ``None`` means "no scope" — the listener
    does not emit any SQL

Live enforcement of the RLS policies is covered by
``tests/test_rls_isolation.py`` which connects to the running
``sovereign-postgres`` container and is skipped if unreachable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovereign_memory.db.rls import (
    _apply_rls_guc,
    current_user_id,
    install_rls_listener,
    set_current_user_id,
    uninstall_rls_listener,
)

# ─────────────────────────── ContextVar plumbing ────────────────────────────


class TestContextVar:
    """The ContextVar is the request-scoped handoff between the auth
    layer and the DB session. Setting it carries the user_id; resetting
    it means 'no user — deny everything'."""

    def test_default_is_none(self):
        # Reset before the assertion so prior tests don't pollute.
        set_current_user_id(None)
        assert current_user_id() is None

    def test_set_then_get(self):
        set_current_user_id("user-alice")
        try:
            assert current_user_id() == "user-alice"
        finally:
            set_current_user_id(None)

    def test_set_none_clears_the_scope(self):
        set_current_user_id("user-alice")
        set_current_user_id(None)
        assert current_user_id() is None

    def test_set_overrides_previous_value(self):
        set_current_user_id("user-alice")
        set_current_user_id("user-bob")
        try:
            assert current_user_id() == "user-bob"
        finally:
            set_current_user_id(None)


# ─────────────────────────── Listener dialect gate ──────────────────────────


class TestListenerDialectGate:
    """The listener MUST NO-OP on non-Postgres dialects so the existing
    SQLite-backed test suite does not break. RLS is Postgres-only."""

    def test_sqlite_is_skipped(self):
        set_current_user_id("user-alice")
        try:
            connection = MagicMock()
            connection.dialect.name = "sqlite"
            _apply_rls_guc(MagicMock(), MagicMock(), connection)
            connection.execute.assert_not_called()
        finally:
            set_current_user_id(None)

    def test_mysql_is_skipped(self):
        """Any non-postgres dialect is skipped defensively."""
        set_current_user_id("user-alice")
        try:
            connection = MagicMock()
            connection.dialect.name = "mysql"
            _apply_rls_guc(MagicMock(), MagicMock(), connection)
            connection.execute.assert_not_called()
        finally:
            set_current_user_id(None)


# ─────────────────────── Listener emits set_config ─────────────────────────


class TestListenerSetConfig:
    """When the dialect IS Postgres and the ContextVar is set, the
    listener emits exactly one ``set_config('app.current_user_id', ...)``
    call against the connection."""

    def test_postgres_with_user_set_emits_set_config(self):
        set_current_user_id("user-alice")
        try:
            connection = MagicMock()
            connection.dialect.name = "postgresql"
            _apply_rls_guc(MagicMock(), MagicMock(), connection)
            assert connection.execute.call_count == 1
            args, _ = connection.execute.call_args
            # First positional arg is the SQLAlchemy text() clause
            stmt = args[0]
            assert "set_config" in str(stmt).lower()
            assert "app.current_user_id" in str(stmt)
            # Second positional arg is the param dict
            params = args[1] if len(args) > 1 else {}
            assert params.get("uid") == "user-alice"
        finally:
            set_current_user_id(None)

    def test_postgres_without_user_does_not_emit(self):
        """When no user is bound to the request, the listener must NOT
        emit set_config. That way the GUC stays at whatever the server
        default is (empty string), which makes the RLS policy deny every
        row — 'safe by default'."""
        set_current_user_id(None)
        connection = MagicMock()
        connection.dialect.name = "postgresql"
        _apply_rls_guc(MagicMock(), MagicMock(), connection)
        connection.execute.assert_not_called()

    def test_postgres_with_sentinel_user_emits(self):
        """Sentinel user_id (bypass mode) is treated like any other
        string: the listener emits set_config with the sentinel value.
        The RLS policy then matches rows whose user_id equals the
        sentinel, which is exactly what Phase 2 rows are tagged with."""
        from sovereign_memory.identity import SENTINEL_SUBJECT

        set_current_user_id(SENTINEL_SUBJECT)
        try:
            connection = MagicMock()
            connection.dialect.name = "postgresql"
            _apply_rls_guc(MagicMock(), MagicMock(), connection)
            assert connection.execute.call_count == 1
            args, _ = connection.execute.call_args
            params = args[1] if len(args) > 1 else {}
            assert params.get("uid") == SENTINEL_SUBJECT
        finally:
            set_current_user_id(None)


# ─────────────────── require_user populates the ContextVar ─────────────────


class TestListenerLifecycle:
    """install_rls_listener and uninstall_rls_listener are idempotent
    and reversible. Used by tests that want to run bare SQLAlchemy
    sessions without the RLS side-effect."""

    def test_install_then_uninstall_round_trip(self):
        # Start from a clean slate — uninstall if already installed.
        uninstall_rls_listener()
        install_rls_listener()
        install_rls_listener()  # idempotent — second call is a no-op
        uninstall_rls_listener()
        uninstall_rls_listener()  # also idempotent — second uninstall is no-op

    def test_uninstall_when_not_installed_is_noop(self):
        """Calling uninstall on a listener that was never installed
        must not raise."""
        uninstall_rls_listener()
        uninstall_rls_listener()


class TestRequireUserPopulatesContextVar:
    """Phase 4 wires the auth dependency to set the ContextVar. Every
    resolved UserContext must end up in ``current_user_id`` so the next
    DB query inside the request inherits the scope."""

    @pytest.mark.asyncio
    async def test_bypass_mode_sets_sentinel(self, monkeypatch):
        """In ``auth_required=false`` bypass mode, ``require_user``
        returns the sentinel UserContext AND sets the ContextVar to
        the sentinel sub."""
        from fastapi import Request

        from sovereign_memory.auth import require_user
        from sovereign_memory.config import get_settings
        from sovereign_memory.identity import SENTINEL_SUBJECT

        # Make sure settings load with auth_required=false.
        get_settings.cache_clear()
        monkeypatch.setenv("SOVEREIGN_AUTH_REQUIRED", "false")

        # Fresh ContextVar
        set_current_user_id(None)
        assert current_user_id() is None

        # Fake request with a User-Agent so agent_type resolves.
        request = MagicMock(spec=Request)
        request.headers = {"user-agent": "curl/8"}
        result = await require_user(request, credentials=None)

        try:
            assert result.user_id == SENTINEL_SUBJECT
            assert current_user_id() == SENTINEL_SUBJECT
        finally:
            set_current_user_id(None)
            get_settings.cache_clear()
