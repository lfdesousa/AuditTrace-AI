"""Offline tests for `scripts/audittrace-login --sync-env`.

The login script additively mirrors a freshly-obtained access_token into the
gitignored Bruno collection `.env` (mode 600) so `bru run` can pick it up as
`{{accessToken}}` without any agent reading the guarded `tokens.json`. These
tests exercise that path with a crafted, far-from-expiry token so no network
call happens (the refresh step is a no-op), asserting:

* the token lands in the target .env as ``accessToken`` and ``AUDITTRACE_TOKEN``;
* the file is mode 600;
* the raw token is NEVER printed to stdout/stderr (TOKEN-GUARD preserved);
* the write is idempotent (re-running does not duplicate the line);
* unrelated pre-existing lines survive (in-place upsert, not clobber);
* ``AUDITTRACE_SYNC_ENV=0`` disables the sync.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "audittrace-login"
FAKE_TOKEN = "FAKE.dummy-jwt-value.doNotLeakMe-0123456789"  # noqa: S105 (test fixture)

pytestmark = pytest.mark.skipif(
    not all(shutil.which(c) for c in ("bash", "jq", "curl", "sha256sum")),
    reason="requires bash + jq + curl + sha256sum on PATH",
)


def _write_tokens(tokens_dir: Path) -> None:
    tokens_dir.mkdir(parents=True, exist_ok=True)
    far_future = (
        int(time.time()) + 100_000
    )  # well beyond REFRESH_THRESHOLD → no network
    (tokens_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": FAKE_TOKEN,
                "refresh_token": "unused-because-not-near-expiry",
                "access_expires_at": far_future,
                "refresh_expires_at": far_future,
                "token_type": "Bearer",
                "realm_issuer": "https://audittrace.local:30952/realms/audittrace",
                "client_id": "audittrace-opencode",
            }
        )
    )


def _run_sync(
    tokens_dir: Path, env_file: Path, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tokens_dir.parent),
        "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
        "AUDITTRACE_BRUNO_ENV": str(env_file),
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "--sync-env"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_sync_env_writes_both_keys_mode_600(tmp_path: Path) -> None:
    tokens_dir = tmp_path / "cfg"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    _write_tokens(tokens_dir)

    proc = _run_sync(tokens_dir, env_file)

    assert proc.returncode == 0, proc.stderr
    content = env_file.read_text()
    assert f"accessToken={FAKE_TOKEN}" in content
    assert f"AUDITTRACE_TOKEN={FAKE_TOKEN}" in content
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600, f"expected 600, got {oct(mode)}"


def test_sync_env_never_prints_raw_token(tmp_path: Path) -> None:
    tokens_dir = tmp_path / "cfg"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    _write_tokens(tokens_dir)

    proc = _run_sync(tokens_dir, env_file)

    assert FAKE_TOKEN not in proc.stdout, "raw token leaked to stdout"
    assert FAKE_TOKEN not in proc.stderr, "raw token leaked to stderr"


def test_sync_env_is_idempotent(tmp_path: Path) -> None:
    tokens_dir = tmp_path / "cfg"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    _write_tokens(tokens_dir)

    _run_sync(tokens_dir, env_file)
    _run_sync(tokens_dir, env_file)

    lines = env_file.read_text().splitlines()
    assert sum(1 for ln in lines if ln.startswith("accessToken=")) == 1
    assert sum(1 for ln in lines if ln.startswith("AUDITTRACE_TOKEN=")) == 1


def test_sync_env_preserves_unrelated_lines(tmp_path: Path) -> None:
    tokens_dir = tmp_path / "cfg"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("baseUrl=https://example.invalid\naccessToken=STALE\n")
    _write_tokens(tokens_dir)

    _run_sync(tokens_dir, env_file)

    content = env_file.read_text()
    assert "baseUrl=https://example.invalid" in content, "operator override clobbered"
    assert "accessToken=STALE" not in content, "stale token not replaced"
    assert f"accessToken={FAKE_TOKEN}" in content


def test_sync_env_disabled_writes_nothing(tmp_path: Path) -> None:
    tokens_dir = tmp_path / "cfg"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    _write_tokens(tokens_dir)

    proc = _run_sync(tokens_dir, env_file, AUDITTRACE_SYNC_ENV="0")

    assert proc.returncode == 0, proc.stderr
    assert not env_file.exists(), "sync should be a no-op when AUDITTRACE_SYNC_ENV=0"
