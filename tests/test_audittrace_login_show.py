"""Tests for the TOKEN-GUARD fix to ``scripts/audittrace-login --show``.

SPEC ``spec-token-guard-kill-show-20260811.md``, layer 1 (the real fix):
``action_show`` used to print the raw ``access_token`` unconditionally
(``scripts/audittrace-login:266-269`` at spec-authoring time) — any agent
invoking ``--show`` got a live JWT in its own transcript, entirely outside
the Read-tool's token deny. This exercises the ACTUAL installed shell
script via subprocess (never a paraphrase of it), the way a caller would
invoke it, against a synthetic ``tokens.json`` so no real credential or
network call is ever involved.

Falsifiability: every "redacted by default" assertion below also checks the
NEGATIVE — that the raw JWT body (``eyJ``, the base64url encoding of
``{"``) never appears in the default ``--show`` output — so a regression
back to the raw-print behaviour fails these tests, not just a happy-path
"exit 0" check.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audittrace-login"

# Not a real JWT (no valid signature) but shaped like one — starts with the
# base64url encoding of a JSON header, ``eyJ...`` — so a test asserting
# "eyJ never appears" is actually exercising the property that matters.
FAKE_ACCESS_TOKEN = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJzdWIiOiJ0ZXN0LXVzZXIiLCJzY29wZSI6ImF1ZGl0dHJhY2U6cXVlcnkifQ."
    "fake-signature-not-real-DO-NOT-TRUST"
)


def _write_tokens_file(tokens_dir: Path, *, expires_in: int = 3600) -> None:
    tokens_dir.mkdir(parents=True, exist_ok=True)
    tokens_file = tokens_dir / "tokens.json"
    payload = {
        "access_token": FAKE_ACCESS_TOKEN,
        "refresh_token": "fake-refresh-token",
        "access_expires_at": int(time.time()) + expires_in,
        "refresh_expires_at": int(time.time()) + expires_in * 2,
        "realm_issuer": "audittrace",
        "client_id": "audittrace-opencode",
    }
    tokens_file.write_text(json.dumps(payload))
    tokens_file.chmod(0o600)


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    tokens_dir = tmp_path / "tokens-dir"
    _write_tokens_file(tokens_dir)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestShowIsRedactedByDefault:
    def test_show_never_prints_the_raw_jwt(self, tmp_path: Path) -> None:
        proc = _run(tmp_path, "--show")

        assert proc.returncode == 0, proc.stderr
        assert "eyJ" not in proc.stdout, (
            f"raw JWT leaked in --show output — TOKEN-GUARD regressed: {proc.stdout!r}"
        )
        assert FAKE_ACCESS_TOKEN not in proc.stdout

    def test_show_output_shape_is_fingerprint_plus_exp(self, tmp_path: Path) -> None:
        proc = _run(tmp_path, "--show")

        assert proc.returncode == 0, proc.stderr
        match = re.search(r"token_fingerprint=([0-9a-f]{8}) exp=(\d+)", proc.stdout)
        assert match is not None, f"unexpected --show output: {proc.stdout!r}"

    def test_show_fingerprint_matches_sha256_of_the_real_token(
        self, tmp_path: Path
    ) -> None:
        proc = _run(tmp_path, "--show")

        match = re.search(r"token_fingerprint=([0-9a-f]{8})", proc.stdout)
        assert match is not None, proc.stdout
        expected = hashlib.sha256(FAKE_ACCESS_TOKEN.encode()).hexdigest()[:8]
        assert match.group(1) == expected

    def test_show_exp_matches_access_expires_at(self, tmp_path: Path) -> None:
        tokens_dir = tmp_path / "tokens-dir"
        _write_tokens_file(tokens_dir, expires_in=7200)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--show"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        stored = json.loads((tokens_dir / "tokens.json").read_text())
        match = re.search(r"exp=(\d+)", proc.stdout)
        assert match is not None, proc.stdout
        assert int(match.group(1)) == stored["access_expires_at"]


class TestShowUnsafeEscapeHatch:
    def test_show_unsafe_prints_the_raw_token(self, tmp_path: Path) -> None:
        proc = _run(tmp_path, "--show-unsafe")

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == FAKE_ACCESS_TOKEN

    def test_env_var_escape_hatch_also_prints_raw_token(self, tmp_path: Path) -> None:
        tokens_dir = tmp_path / "tokens-dir"
        _write_tokens_file(tokens_dir)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
            "AUDITTRACE_SHOW_RAW": "1",
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--show"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == FAKE_ACCESS_TOKEN

    def test_plain_show_without_env_var_stays_redacted(self, tmp_path: Path) -> None:
        # Negative control for the env-var test above: without
        # AUDITTRACE_SHOW_RAW set, plain --show must NOT leak the raw token
        # (i.e. the env var is genuinely opt-in, not a no-op).
        proc = _run(tmp_path, "--show")

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() != FAKE_ACCESS_TOKEN
        assert "eyJ" not in proc.stdout


class TestEnsureAndLogoutUnaffected:
    """The redaction fix must not disturb the other actions."""

    def test_ensure_exits_zero_and_prints_nothing_secret(self, tmp_path: Path) -> None:
        proc = _run(tmp_path, "--ensure")

        assert proc.returncode == 0, proc.stderr
        assert "eyJ" not in proc.stdout
        assert FAKE_ACCESS_TOKEN not in proc.stdout

    def test_logout_deletes_tokens_file(self, tmp_path: Path) -> None:
        tokens_dir = tmp_path / "tokens-dir"
        _write_tokens_file(tokens_dir)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--logout"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert not (tokens_dir / "tokens.json").exists()


class TestHelpTextAdvertisesShowUnsafe:
    def test_help_mentions_show_unsafe_and_env_var(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert "--show-unsafe" in proc.stdout
        assert "AUDITTRACE_SHOW_RAW" in proc.stdout
