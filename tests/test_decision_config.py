"""#459 WU-459-1 T10: decision config defaults + one test per validator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audittrace.config import Settings

DIGEST = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"
UNTABLED_DIGEST = "ab" * 32  # valid hex, but no vendored added-token table


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_defaults_leave_everything_off() -> None:
    s = _s()
    assert s.decision_url == ""
    assert s.decision_api_key == ""
    assert s.decision_model == ""
    assert s.decision_model_alias == ""
    assert s.decision_model_digest == ""
    assert s.decision_timeout_ms == 2000
    assert s.decision_temperature == 1.0
    assert s.decision_backend == ""
    assert s.decision_quantisation == ""
    assert s.memory_routing_mode == "off"


def test_valid_enabled_config_is_accepted() -> None:
    s = _s(
        decision_url="http://x:1",
        decision_model_digest=DIGEST,
        decision_model_alias="a",
        memory_routing_mode="shadow",
    )
    assert s.memory_routing_mode == "shadow"


def test_env_prefix_is_audittrace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDITTRACE_DECISION_TIMEOUT_MS", "1234")
    monkeypatch.setenv("AUDITTRACE_DECISION_TEMPERATURE", "0.5")
    s = _s()
    assert (s.decision_timeout_ms, s.decision_temperature) == (1234, 0.5)


@pytest.mark.parametrize("mode", ["acting", "ACTING", "Off", "on", ""])
def test_mode_outside_off_shadow_rejected(mode: str) -> None:
    with pytest.raises(ValidationError, match="memory_routing_mode"):
        _s(memory_routing_mode=mode)


@pytest.mark.parametrize(
    "digest",
    ["", "abc", "AB" * 32, "zz" * 32, "ab" * 31, "ab" * 33, "ab" * 32 + "\n"],
)
def test_bad_digest_rejected_when_url_set(digest: str) -> None:
    with pytest.raises(ValidationError, match="decision_model_digest"):
        _s(
            decision_url="http://x:1",
            decision_model_alias="a",
            decision_model_digest=digest,
        )


def test_digest_not_required_when_url_unset() -> None:
    assert _s(decision_model_digest="not-a-digest").decision_url == ""


def test_alias_required_when_url_set() -> None:
    with pytest.raises(ValidationError, match="decision_model_alias"):
        _s(decision_url="http://x:1", decision_model_digest=DIGEST)


def test_shadow_without_url_rejected() -> None:
    with pytest.raises(ValidationError, match="requires decision_url"):
        _s(memory_routing_mode="shadow")


@pytest.mark.parametrize("value", [0, -1, -2000])
def test_timeout_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError, match="decision_timeout_ms"):
        _s(decision_timeout_ms=value)


@pytest.mark.parametrize(
    "value", [0.0, -0.5, float("nan"), float("inf"), float("-inf")]
)
def test_temperature_must_be_positive_and_finite(value: float) -> None:
    with pytest.raises(ValidationError, match="decision_temperature"):
        _s(decision_temperature=value)


def test_digest_without_a_vendored_token_table_is_rejected_when_url_set() -> None:
    """An unknown model has no control-token denylist, so it may not decide."""
    with pytest.raises(ValidationError, match="vendored added-token table"):
        _s(
            decision_url="http://x:1",
            decision_model_alias="a",
            decision_model_digest=UNTABLED_DIGEST,
        )


def test_untabled_digest_is_harmless_while_disabled() -> None:
    assert _s(decision_model_digest=UNTABLED_DIGEST).decision_url == ""
