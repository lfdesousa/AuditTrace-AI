"""Tests for bff/config.py — env-parameterization + fail-closed startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bff.config import Settings, get_settings


class TestSettingsDefaults:
    def test_laptop_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        settings = Settings()
        # ``env`` itself is "test" under the harness (conftest sets
        # AUDITTRACE_BFF_ENV=test so .env never loads); the class-level
        # default ("local") is asserted directly against the field.
        assert Settings.model_fields["env"].default == "local"
        assert settings.port == 8766
        assert settings.orchestrator_base_url == "http://localhost:8765"
        assert settings.orchestrator_chat_path == "/v1/chat/completions"
        assert settings.exchange_client_id == "audittrace-librechat-bff"
        assert settings.exchange_audience == "audittrace-librechat"
        assert settings.proxy_source_label == "librechat"
        assert settings.keycloak_issuer_extras == []
        assert settings.ca_bundle_path == ""
        assert settings.console_files_forced_layer == "session"

    def test_ca_bundle_path_env_override_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        monkeypatch.setenv("AUDITTRACE_BFF_CA_BUNDLE_PATH", "/etc/audittrace/ca.crt")
        settings = Settings()
        assert settings.ca_bundle_path == "/etc/audittrace/ca.crt"

    def test_env_override_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        monkeypatch.setenv(
            "AUDITTRACE_BFF_ORCHESTRATOR_BASE_URL", "http://audittrace-server:8765"
        )
        monkeypatch.setenv("AUDITTRACE_BFF_PORT", "9999")
        settings = Settings()
        assert settings.orchestrator_base_url == "http://audittrace-server:8765"
        assert settings.port == 9999

    def test_console_files_forced_layer_env_override_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        monkeypatch.setenv(
            "AUDITTRACE_BFF_CONSOLE_FILES_FORCED_LAYER", "different-layer"
        )
        settings = Settings()
        assert settings.console_files_forced_layer == "different-layer"


class TestExchangeSecretFailClosed:
    def test_missing_secret_raises_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The confidential-client secret has NO default — an unset
        secret must fail Settings() construction (startup), never
        silently fall through to an empty/static value at request time."""
        monkeypatch.delenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", raising=False)
        with pytest.raises(ValidationError):
            Settings()


class TestGetSettingsCaching:
    def test_cached_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET", "s3cr3t")
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b
