"""#459 WU-459-1 T10 + WU-459-2 chunk 04: decision config defaults, one test per validator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from audittrace.config import Settings
from audittrace.services.decision.record import resolve_record_path
from tests.fakes.decision_server import RECORD_NAME, RECORD_SHA256

DIGEST = "4f8a3d7fc2c8eda2601751ace44690ba1080e508842df88644cedcc08af82cdf"
UNTABLED_DIGEST = "ab" * 32  # valid hex, but not a weights[].digest of the record


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def _enabled(**kw: object) -> Settings:
    """A valid enabled configuration (URL, record, digest, alias) plus overrides."""
    values: dict[str, object] = {
        "decision_url": "http://x:1",
        "decision_model_digest": DIGEST,
        "decision_model_alias": "a",
        "decision_artefact_record": RECORD_NAME,
        "decision_artefact_record_sha256": RECORD_SHA256,
    }
    values.update(kw)
    return _s(**values)


def _record_copy(tmp_path: Path, mutate: object) -> tuple[str, str]:
    """A mutated copy of the shipped record and its OWN sha256."""
    data = json.loads(resolve_record_path(RECORD_NAME).read_text(encoding="utf-8"))
    mutate(data)  # type: ignore[operator]
    out = tmp_path / RECORD_NAME
    out.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return str(out), hashlib.sha256(out.read_bytes()).hexdigest()


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
    assert s.decision_artefact_record == ""
    assert s.decision_artefact_record_sha256 == ""
    assert s.decision_state_max_chars == 4000
    assert s.decision_shadow_max_inflight == 1
    assert s.decision_shadow_queue_max == 8
    assert s.decision_breaker_threshold == 5
    assert s.decision_breaker_cooldown_s == 60.0
    assert s.decision_identity_ttl_s == 300.0


def test_valid_enabled_config_is_accepted() -> None:
    s = _enabled(memory_routing_mode="shadow")
    assert s.memory_routing_mode == "shadow"
    assert s.decision_artefact_record == RECORD_NAME
    assert s.decision_artefact_record_sha256 == RECORD_SHA256


def test_env_prefix_is_audittrace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDITTRACE_DECISION_TIMEOUT_MS", "1234")
    monkeypatch.setenv("AUDITTRACE_DECISION_TEMPERATURE", "0.5")
    monkeypatch.setenv("AUDITTRACE_DECISION_STATE_MAX_CHARS", "77")
    s = _s()
    assert (s.decision_timeout_ms, s.decision_temperature) == (1234, 0.5)
    assert s.decision_state_max_chars == 77


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
        _enabled(decision_model_digest=digest)


def test_digest_not_required_when_url_unset() -> None:
    assert _s(decision_model_digest="not-a-digest").decision_url == ""


def test_alias_required_when_url_set() -> None:
    with pytest.raises(ValidationError, match="decision_model_alias"):
        _enabled(decision_model_alias="")


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
    """A digest the record does not vouch for has no denylist, so it may not decide."""
    with pytest.raises(ValidationError, match="weights"):
        _enabled(decision_model_digest=UNTABLED_DIGEST)


def test_untabled_digest_is_harmless_while_disabled() -> None:
    assert _s(decision_model_digest=UNTABLED_DIGEST).decision_url == ""


# ------------------------------------------------- WU-459-2 chunk 04: the eight fields


def test_state_max_chars_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="decision_state_max_chars"):
        _s(decision_state_max_chars=0)
    with pytest.raises(ValidationError, match="decision_state_max_chars"):
        _s(decision_state_max_chars=-5)
    assert _s(decision_state_max_chars=1).decision_state_max_chars == 1


def test_max_inflight_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="decision_shadow_max_inflight"):
        _s(decision_shadow_max_inflight=0)
    assert _s(decision_shadow_max_inflight=1).decision_shadow_max_inflight == 1


def test_breaker_threshold_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="decision_breaker_threshold"):
        _s(decision_breaker_threshold=0)
    assert _s(decision_breaker_threshold=1).decision_breaker_threshold == 1


def test_queue_max_negative_rejected() -> None:
    with pytest.raises(ValidationError, match="decision_shadow_queue_max"):
        _s(decision_shadow_queue_max=-1)


def test_queue_max_zero_accepted() -> None:
    assert _s(decision_shadow_queue_max=0).decision_shadow_queue_max == 0


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_breaker_cooldown_must_be_positive_finite(value: float) -> None:
    with pytest.raises(ValidationError, match="decision_breaker_cooldown_s"):
        _s(decision_breaker_cooldown_s=value)


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_identity_ttl_must_be_positive_finite(value: float) -> None:
    with pytest.raises(ValidationError, match="decision_identity_ttl_s"):
        _s(decision_identity_ttl_s=value)


# ------------------------------------------- WU-459-2 chunk 04: the record at startup


def test_record_required_when_url_set() -> None:
    with pytest.raises(ValidationError, match="decision_artefact_record is required"):
        _enabled(decision_artefact_record="")


def test_record_not_loaded_when_url_unset() -> None:
    s = _s(decision_artefact_record="nope.json", decision_artefact_record_sha256="x")
    assert s.decision_url == ""


def test_record_sha_mismatch_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="sha256_mismatch"):
        _enabled(decision_artefact_record_sha256="00" * 32)


def test_record_missing_rejected_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="missing"):
        _enabled(decision_artefact_record=str(tmp_path / "absent.json"))


def test_digest_not_in_record_rejected() -> None:
    with pytest.raises(ValidationError, match="weights"):
        _enabled(decision_model_digest="cd" * 32)


def test_unknown_family_rejected_at_startup(tmp_path: Path) -> None:
    def mutate(d: dict[str, object]) -> None:
        d["template"]["family"] = "foo"  # type: ignore[index]

    path, sha = _record_copy(tmp_path, mutate)
    with pytest.raises(ValidationError, match="family_unknown"):
        _enabled(decision_artefact_record=path, decision_artefact_record_sha256=sha)


def test_absolute_record_path_is_accepted(tmp_path: Path) -> None:
    path, sha = _record_copy(tmp_path, lambda d: None)
    s = _enabled(decision_artefact_record=path, decision_artefact_record_sha256=sha)
    assert s.decision_artefact_record == path
