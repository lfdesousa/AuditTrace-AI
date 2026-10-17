"""Unit tests for the shared front-door resolver (SPEC #401).

Proves the single-source-of-truth invariant: explicit > env > fallback
precedence, fresh env reads, and that memory.py consumes it correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scripts.deploy import frontdoor, memory
from scripts.deploy.frontdoor import DEFAULT_FRONT_DOOR, resolve_front_door


def _origin(url: str) -> str:
    """Scheme+host of a URL, EXACT-matched (never a substring ``in`` check) so a
    request URL's path/query can't let an arbitrary-position match slip through.
    Clears CodeQL py/incomplete-url-substring-sanitization."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ── resolve_front_door: precedence ────────────────────────────────────────────


def test_explicit_arg_wins_over_env_and_fallback(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env.host")
    assert resolve_front_door("https://explicit.host") == "https://explicit.host"


def test_env_wins_over_fallback_when_no_explicit_arg(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env.host")
    assert resolve_front_door() == "https://env.host"
    assert resolve_front_door(None) == "https://env.host"


def test_fallback_when_no_explicit_arg_and_no_env(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    assert resolve_front_door() == DEFAULT_FRONT_DOOR
    assert resolve_front_door(None) == DEFAULT_FRONT_DOOR


def test_empty_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "")
    assert resolve_front_door() == DEFAULT_FRONT_DOOR


def test_whitespace_only_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "   ")
    assert resolve_front_door() == DEFAULT_FRONT_DOOR


def test_explicit_empty_string_is_not_set_and_falls_through(monkeypatch):
    """An explicit empty string is falsy in Python, so it falls through
    to env/fallback — consistent with the spec's `if explicit:` check.
    """
    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    assert resolve_front_door("") == DEFAULT_FRONT_DOOR


# ── fresh env read (environment-dynamic) ─────────────────────────────────────

# SPEC §4(d): env is read FRESH at call time, NOT cached at import.
# Neuter the env branch → this test goes RED with a fallback-hit.


def test_env_read_is_fresh_at_call_time(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    # At import time, env is unset → fallback
    result_before = resolve_front_door()
    assert result_before == DEFAULT_FRONT_DOOR
    # Set env AFTER import — must be picked up
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://fresh.host")
    assert resolve_front_door() == "https://fresh.host"
    # The fallback path is still callable
    assert resolve_front_door(None) == "https://fresh.host"


# ── memory.py integration: env drives the target ─────────────────────────────

# SPEC §4: AUDITTRACE_FRONT_DOOR=https://audittrace.local (no explicit arg)
# makes log_deploy_record target the laptop host, NOT allaboutdata.eu.
# This is the exact scenario that failed live.


def _install_memory_http(monkeypatch, responses):
    """Install a fake http_request in memory module and return a call spy."""

    class _Spy:
        def __init__(self):
            self.calls = []

        def __call__(
            self, method, url, headers=None, body=None, timeout=30, context=None
        ):
            parsed = urlparse(url)
            self.calls.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "url": url,
                }
            )
            status, payload = responses.get(parsed.path, (404, b'{"detail":"nf"}'))
            return status, {}, payload

    spy = _Spy()
    monkeypatch.setattr(memory, "_http_request", spy)
    return spy


def _clear_env_token(monkeypatch):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)


def test_log_deploy_record_targets_env_front_door_not_hardcoded_fallback(
    monkeypatch, tmp_path
):
    """AUDITTRACE_FRONT_DOOR=https://audittrace.local makes log_deploy_record
    target the laptop host, NOT the hardcoded allaboutdata.eu fallback.

    This is the EXACT live failure: the fleet's OWN verdict-logging tried to
    reach audittrace.allaboutdata.eu from the laptop and failed — lost data.
    The fix: memory.py now uses resolve_front_door which honours the env var.
    """
    _clear_env_token(monkeypatch)
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://audittrace.local")

    fake = _install_memory_http(
        monkeypatch,
        {
            "/memory/upload": (200, json.dumps({"key": "episodic/rec.md"}).encode()),
            "/memory/index": (200, json.dumps({"indexed": 1}).encode()),
        },
    )
    # No explicit front_door arg — should resolve via env → audittrace.local
    memory.log_deploy_record("test record", token="fake.jwt", filename="rec.md")
    upload_call = next(c for c in fake.calls if c["path"] == "/memory/upload")
    assert _origin(upload_call["url"]) == "https://audittrace.local"
    assert _origin(upload_call["url"]) != "https://audittrace.allaboutdata.eu"


def test_log_deploy_record_explicit_front_door_overrides_env(monkeypatch, tmp_path):
    """Explicit front_door= arg still wins over env var (backward compat)."""
    _clear_env_token(monkeypatch)
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env.host")

    fake = _install_memory_http(
        monkeypatch,
        {
            "/memory/upload": (200, json.dumps({"key": "episodic/x.md"}).encode()),
            "/memory/index": (200, b"{}"),
        },
    )
    memory.log_deploy_record(
        "t", token="fake.jwt", filename="x.md", front_door="https://explicit.host"
    )
    upload_call = next(c for c in fake.calls if c["path"] == "/memory/upload")
    assert _origin(upload_call["url"]) == "https://explicit.host"


def test_recall_deploy_lessons_targets_env_front_door(monkeypatch):
    """AUDITTRACE_FRONT_DOOR env var also drives recall_deploy_lessons."""
    _clear_env_token(monkeypatch)
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env-serve.test")

    fake = _install_memory_http(
        monkeypatch,
        {"/memory/semantic": (200, json.dumps({"items": []}).encode())},
    )
    memory.recall_deploy_lessons("q", token="fake.jwt")
    call = fake.calls[0]
    assert _origin(call["url"]) == "https://env-serve.test"


def test_default_front_door_constant_is_backward_compat():
    """DEFAULT_FRONT_DOOR is still exported (for any external readers)."""
    assert frontdoor.DEFAULT_FRONT_DOOR == "https://audittrace.allaboutdata.eu"


def test_memory_default_front_door_constant_is_re_exported():
    """memory.DEFAULT_FRONT_DOOR is re-exported from frontdoor for
    backward compatibility with callers that import from memory."""
    assert memory.DEFAULT_FRONT_DOOR == "https://audittrace.allaboutdata.eu"


def test_no_url_substring_in_frontdoor_source():
    """CodeQL-safety guard: the frontdoor module must not gate on a URL
    substring test."""
    src = Path(frontdoor.__file__).read_text()
    assert " in url" not in src
    assert "in front_door" not in src


def test_no_url_substring_in_memory_after_refactor():
    """Verify the memory.py refactor did not introduce substring URL checks."""
    src = Path(memory.__file__).read_text()
    assert " in url" not in src
    assert "in front_door" not in src


def test_verify_reexports_default_front_door():
    """verify.DEFAULT_FRONT_DOOR is still available (re-exported)."""
    from scripts.deploy import verify

    assert verify.DEFAULT_FRONT_DOOR == "https://audittrace.allaboutdata.eu"


def test_verify_default_front_door_is_shared_module_identity():
    """verify.DEFAULT_FRONT_DOOR is the SAME object as frontdoor.DEFAULT_FRONT_DOOR
    (not a copy or a literal). A future re-hardcode in verify.py would break
    the identity check, catching the DRY violation immediately."""
    from scripts.deploy import frontdoor, verify

    assert verify.DEFAULT_FRONT_DOOR is frontdoor.DEFAULT_FRONT_DOOR


def test_verify_config_front_door_none_defaults_to_resolved(monkeypatch):
    """When VerifyConfig is constructed with front_door=None (the new default),
    __post_init__ resolves it via resolve_front_door — so the stored value is
    a real URL string, not None."""
    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    from scripts.deploy.verify import VerifyConfig

    cfg = VerifyConfig(target_version="v1.0.0", front_door=None)
    # __post_init__ resolves None → env var (unset) → DEFAULT_FRONT_DOOR
    assert cfg.front_door == "https://audittrace.allaboutdata.eu"


def test_verify_config_accepts_explicit_front_door():
    """Tests that construct VerifyConfig directly still pass an explicit value."""
    from scripts.deploy.verify import VerifyConfig

    cfg = VerifyConfig(target_version="v1.0.0", front_door="https://test.host")
    assert cfg.front_door == "https://test.host"


def test_config_from_args_resolves_env_front_door(monkeypatch, tmp_path):
    """config_from_args resolves front_door via resolve_front_door when
    --front-door is not passed (i.e., args.front_door is None)."""
    from scripts.deploy import verify

    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env-resolve.test")

    parser = verify.build_parser()
    args = parser.parse_args(["--target-version", "v1.0.0"])
    assert args.front_door is None

    cfg = verify.config_from_args(args)
    assert cfg.front_door == "https://env-resolve.test"


def test_config_from_args_explicit_front_door_overrides_env(monkeypatch):
    """Explicit --front-door flag wins over AUDITTRACE_FRONT_DOOR env."""
    from scripts.deploy import verify

    monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "https://env.test")
    parser = verify.build_parser()
    args = parser.parse_args(
        [
            "--target-version",
            "v1.0.0",
            "--front-door",
            "https://flag.test",
        ]
    )
    cfg = verify.config_from_args(args)
    assert cfg.front_door == "https://flag.test"


def test_config_from_args_fallback_when_no_explicit_and_no_env(monkeypatch, tmp_path):
    """When no --front-door and no env var, config_from_args uses the
    hardcoded fallback."""
    from scripts.deploy import verify

    monkeypatch.delenv("AUDITTRACE_FRONT_DOOR", raising=False)
    parser = verify.build_parser()
    args = parser.parse_args(["--target-version", "v1.0.0"])

    cfg = verify.config_from_args(args)
    assert cfg.front_door == DEFAULT_FRONT_DOOR
