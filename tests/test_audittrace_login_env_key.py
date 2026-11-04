"""Offline tests for `scripts/audittrace-login --env-key VAR` / `--unset-env`.

LC-9 ADDENDUM-1 S4. The RT-5 live run needs several identities at once, so the
login script can mirror a token into a NAMED variable of the Bruno `.env`:

(a) ``--env-key VAR`` writes VAR only; the ``accessToken`` / ``AUDITTRACE_TOKEN``
    lines stay byte-equal;
(b) it logs the path and VAR, never the token;
(c) ``--env-key VAR --unset-env`` removes the line (no network, no token read);
refresh = the same call pointed at that identity's ``AUDITTRACE_TOKENS_DIR``.

FAKE tokens only, in ``tmp_path``. No network: the crafted tokens are far from
expiry so the refresh step is a no-op.

Falsifiability (see the neuter table in the PR evidence): writing
``accessToken`` from the named path turns the "VAR only" test RED; echoing the
token turns the "never logs the token" test RED; a prefix match on unset, a
swallowed grep error and a missing cleanup trap each turn a test below RED.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests._hermetic_env import hermetic_env

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "audittrace-login"
FAKE_A = "FAKE.identity-a-jwt.neverPrintA-0123456789"  # noqa: S105 (test fixture)
FAKE_B = "FAKE.identity-b-jwt.neverPrintB-9876543210"  # noqa: S105 (test fixture)
OPERATOR_LINES = "accessToken=FAKE.operator-tok\nAUDITTRACE_TOKEN=FAKE.operator-tok\n"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(c) for c in ("bash", "jq", "curl", "sha256sum")),
    reason="requires bash + jq + curl + sha256sum on PATH",
)


def _write_tokens(tokens_dir: Path, token: str) -> None:
    tokens_dir.mkdir(parents=True, exist_ok=True)
    far_future = int(time.time()) + 100_000
    (tokens_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": token,
                "refresh_token": "unused-because-not-near-expiry",
                "access_expires_at": far_future,
                "refresh_expires_at": far_future,
                "token_type": "Bearer",
                "realm_issuer": "https://audittrace.local:30952/realms/audittrace",
                "client_id": "audittrace-opencode",
            }
        )
    )


def _run(
    tokens_dir: Path, env_file: Path, *args: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    env = hermetic_env(
        {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tokens_dir.parent),
            "AUDITTRACE_TOKENS_DIR": str(tokens_dir),
            "AUDITTRACE_BRUNO_ENV": str(env_file),
            **extra_env,
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[Path, Path]:
    tokens_dir = tmp_path / "cfg-a"
    env_file = tmp_path / "bruno" / ".env"
    env_file.parent.mkdir(parents=True)
    _write_tokens(tokens_dir, FAKE_A)
    return tokens_dir, env_file


def test_env_key_writes_var_only_default_lines_byte_equal(
    ctx: tuple[Path, Path],
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text("baseUrl=https://example.invalid\n" + OPERATOR_LINES)

    proc = _run(tokens_dir, env_file, "--sync-env", "--env-key", "tokenUserA")

    assert proc.returncode == 0, proc.stderr
    lines = env_file.read_text().splitlines()
    assert f"tokenUserA={FAKE_A}" in lines
    # the default pair is byte-equal and no new accessToken line appeared
    assert "accessToken=FAKE.operator-tok" in lines
    assert "AUDITTRACE_TOKEN=FAKE.operator-tok" in lines
    assert sum(1 for ln in lines if ln.startswith("accessToken=")) == 1
    assert sum(1 for ln in lines if ln.startswith("AUDITTRACE_TOKEN=")) == 1
    assert FAKE_A not in "\n".join(
        ln for ln in lines if not ln.startswith("tokenUserA=")
    )
    assert "baseUrl=https://example.invalid" in lines
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_env_key_logs_path_and_var_never_the_token(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx

    proc = _run(tokens_dir, env_file, "--sync-env", "--env-key", "tokenUserA")

    assert proc.returncode == 0, proc.stderr
    assert str(env_file) in proc.stderr
    assert "tokenUserA" in proc.stderr
    assert FAKE_A not in proc.stdout, "raw token present on stdout"
    assert FAKE_A not in proc.stderr, "raw token present on stderr"


def test_env_key_is_idempotent_and_updates_in_place(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx
    _run(tokens_dir, env_file, "--sync-env", "--env-key", "tokenUserA")
    _write_tokens(tokens_dir, FAKE_B)  # refreshed token for the same identity slot
    _run(tokens_dir, env_file, "--sync-env", "--env-key", "tokenUserA")

    lines = env_file.read_text().splitlines()
    assert [ln for ln in lines if ln.startswith("tokenUserA=")] == [
        f"tokenUserA={FAKE_B}"
    ]


def test_two_identities_refresh_through_their_own_tokens_dir(
    ctx: tuple[Path, Path], tmp_path: Path
) -> None:
    tokens_dir_a, env_file = ctx
    tokens_dir_b = tmp_path / "cfg-b"
    _write_tokens(tokens_dir_b, FAKE_B)

    _run(tokens_dir_a, env_file, "--sync-env", "--env-key", "tokenUserA")
    proc = _run(tokens_dir_b, env_file, "--sync-env", "--env-key", "tokenUserB")

    assert proc.returncode == 0, proc.stderr
    lines = env_file.read_text().splitlines()
    assert f"tokenUserA={FAKE_A}" in lines
    assert f"tokenUserB={FAKE_B}" in lines


def test_unset_env_removes_only_the_named_line(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(
        OPERATOR_LINES
        + f"tokenUserA={FAKE_A}\ntokenUserB={FAKE_B}\ntokenUserAB={FAKE_B}\n"
    )

    proc = _run(tokens_dir, env_file, "--env-key", "tokenUserA", "--unset-env")

    assert proc.returncode == 0, proc.stderr
    content = env_file.read_text()
    assert not any(ln.startswith("tokenUserA=") for ln in content.splitlines())
    assert f"tokenUserB={FAKE_B}" in content
    # a sibling whose name merely starts with the key survives (match is on `=`)
    assert f"tokenUserAB={FAKE_B}" in content
    assert OPERATOR_LINES in content
    assert FAKE_A not in proc.stdout + proc.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_unset_env_needs_no_tokens_file_and_tolerates_missing_env(
    tmp_path: Path,
) -> None:
    tokens_dir = tmp_path / "empty-cfg"  # no tokens.json: must not be read
    env_file = tmp_path / "bruno" / ".env"  # does not exist

    proc = _run(tokens_dir, env_file, "--env-key", "tokenUserA", "--unset-env")

    assert proc.returncode == 0, proc.stderr
    assert not env_file.exists()


def test_unset_env_without_env_key_is_refused(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(OPERATOR_LINES)

    proc = _run(tokens_dir, env_file, "--unset-env")

    assert proc.returncode == 64
    assert env_file.read_text() == OPERATOR_LINES


@pytest.mark.parametrize(
    "bad", ["accessToken", "AUDITTRACE_TOKEN", "a b", "1x", "x=y", "a.*"]
)
def test_env_key_refuses_default_pair_and_invalid_names(
    ctx: tuple[Path, Path], bad: str
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(OPERATOR_LINES)

    proc = _run(tokens_dir, env_file, "--sync-env", "--env-key", bad)

    assert proc.returncode == 64
    assert env_file.read_text() == OPERATOR_LINES


def test_env_key_requires_a_value(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx

    proc = _run(tokens_dir, env_file, "--env-key")

    assert proc.returncode == 64
    assert "requires a variable name" in proc.stderr


def test_env_key_honours_sync_opt_out(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx

    proc = _run(
        tokens_dir,
        env_file,
        "--ensure",
        "--env-key",
        "tokenUserA",
        AUDITTRACE_SYNC_ENV="0",
    )

    assert proc.returncode == 0, proc.stderr
    assert not env_file.exists()


def test_help_documents_the_new_flags(ctx: tuple[Path, Path]) -> None:
    tokens_dir, env_file = ctx

    proc = _run(tokens_dir, env_file, "--help")

    assert proc.returncode == 0
    assert "--env-key VAR" in proc.stdout
    assert "--unset-env" in proc.stdout


def _failing_grep_path(tmp_path: Path) -> str:
    """A PATH whose first entry holds a `grep` that prints a partial line and exits 2."""
    shim = tmp_path / "shim"
    shim.mkdir()
    grep = shim / "grep"
    grep.write_text("#!/bin/sh\necho partial-line\nexit 2\n")
    grep.chmod(0o755)
    return f"{shim}:/usr/bin:/bin:/usr/local/bin"


def test_unset_env_fails_closed_on_a_grep_io_error(
    ctx: tuple[Path, Path], tmp_path: Path
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(OPERATOR_LINES + f"tokenUserA={FAKE_A}\n")
    before = env_file.read_bytes()

    proc = _run(
        tokens_dir,
        env_file,
        "--env-key",
        "tokenUserA",
        "--unset-env",
        PATH=_failing_grep_path(tmp_path),
    )

    assert proc.returncode != 0
    assert env_file.read_bytes() == before, "env file replaced after a grep error"
    assert [p.name for p in env_file.parent.iterdir()] == [".env"], "temp file left"
    assert "grep rc=2" in proc.stderr


def test_env_key_write_fails_closed_on_a_grep_io_error(
    ctx: tuple[Path, Path], tmp_path: Path
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(OPERATOR_LINES)
    before = env_file.read_bytes()

    proc = _run(
        tokens_dir,
        env_file,
        "--sync-env",
        "--env-key",
        "tokenUserA",
        PATH=_failing_grep_path(tmp_path),
    )

    assert proc.returncode != 0
    assert env_file.read_bytes() == before, "env file replaced after a grep error"
    assert [p.name for p in env_file.parent.iterdir()] == [".env"], "temp file left"
    assert FAKE_A not in proc.stdout + proc.stderr


def test_default_pair_sync_fails_closed_on_a_grep_io_error(
    ctx: tuple[Path, Path], tmp_path: Path
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(OPERATOR_LINES)
    before = env_file.read_bytes()

    proc = _run(tokens_dir, env_file, "--sync-env", PATH=_failing_grep_path(tmp_path))

    assert proc.returncode != 0
    assert env_file.read_bytes() == before
    assert [p.name for p in env_file.parent.iterdir()] == [".env"]


def test_unset_env_removing_the_only_line_leaves_an_empty_file(
    ctx: tuple[Path, Path],
) -> None:
    tokens_dir, env_file = ctx
    env_file.write_text(f"tokenUserA={FAKE_A}\n")

    proc = _run(tokens_dir, env_file, "--env-key", "tokenUserA", "--unset-env")

    assert proc.returncode == 0, proc.stderr
    assert env_file.read_text() == ""
