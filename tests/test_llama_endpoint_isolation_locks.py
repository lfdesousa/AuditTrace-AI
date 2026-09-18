"""M3 WU-4 — llama endpoint isolation locks (Rule 1, unit).

Asserts the EFFECTIVE launch invocation of every enumerated llama-server
endpoint on this host — chat (scripts/start-chat-llama.sh), embed
(scripts/start-embed-llama.sh), and summariser
(scripts/start-summarizer-llama.sh) — carries all four locks:

  1. ``--host 127.0.0.1``           (never 0.0.0.0)
  2. ``--api-key-file <path>``      (never a literal key on argv)
  3. ``--no-webui``
  4. consumer wiring — covered separately (test_chat_proxy.py's
     ``TestLlamaApiKeyHeaderWiring``, test_adr047_server_side_embedding.py,
     test_session_summarizer.py's ``TestEnsureClientAuthHeader``)

"Effective invocation" means: run the REAL script (with a stub
``llama-server`` standing in for the binary) and capture the argv it
actually execs — not a ``grep`` of the script source.

Per STANDING TERMS (one neuter per lock, cmp-restored between each, per-
lock table never an aggregate): ``TestNeuterEachLock`` mutates a
byte-identical TMP COPY of the tracked ``scripts/llama-lock-flags.lib.sh``
— never the real file on disk, so an interrupted test run can never leave
a live launcher script neutered — proves the specific lock's assertion
goes RED while the other two stay GREEN, then discards the mutated copy.
An autouse fixture asserts the tracked file's bytes never moved across
the whole module.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LOCK_LIB = SCRIPTS_DIR / "llama-lock-flags.lib.sh"

ENDPOINTS = {
    "chat": SCRIPTS_DIR / "start-chat-llama.sh",
    "embed": SCRIPTS_DIR / "start-embed-llama.sh",
    "summarizer": SCRIPTS_DIR / "start-summarizer-llama.sh",
}

_LOCK_LIB_ANCHOR = 'LLAMA_LOCK_FLAGS=(--host 127.0.0.1 --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)'


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _tracked_files_never_mutated():
    """Every test in this module operates on tmp copies. This is the
    falsifiability backstop: if any test ever touched a tracked file,
    this fails loudly instead of silently leaving a locked-down launcher
    script neutered on disk."""
    before = {p: _sha256(p) for p in [LOCK_LIB, *ENDPOINTS.values()]}
    yield
    after = {p: _sha256(p) for p in before}
    assert after == before, "a test mutated a tracked launcher script in place"


def _make_stub_llama_bin(tmp_path: Path) -> Path:
    stub = tmp_path / "llama-server"
    stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_FILE"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _make_key_file(tmp_path: Path, name: str = "llama-api-key.txt") -> Path:
    key_file = tmp_path / name
    key_file.write_text("test-key-do-not-use\n")
    key_file.chmod(0o600)
    return key_file


def _run_launcher(
    script: Path,
    tmp_path: Path,
    *,
    key_file: Path | None = None,
    scripts_dir: Path | None = None,
) -> tuple[int, list[str], str]:
    """Run *script* with a stub ``llama-server`` and return
    ``(returncode, captured_argv, stderr)``. ``captured_argv`` is empty
    when the launcher exited before reaching ``exec`` (e.g. missing key
    file — the fail-closed path)."""
    model = tmp_path / "model.gguf"
    model.touch()
    capture = tmp_path / "capture.txt"
    if key_file is None:
        key_file = _make_key_file(tmp_path)
    env = {
        **os.environ,
        "LLAMA_BIN": str(_make_stub_llama_bin(tmp_path)),
        "MODEL_PATH": str(model),
        "LLAMA_API_KEY_FILE": str(key_file),
        "CAPTURE_FILE": str(capture),
    }
    # Point PATH/relative sourcing at an alternate scripts/ tree for the
    # neuter tests (never the real one).
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    argv = capture.read_text().splitlines() if capture.exists() else []
    return result.returncode, argv, result.stderr


def _assert_lock_host(argv: list[str]) -> None:
    assert "--host" in argv, "lock 1 (bind) missing: no --host flag at all"
    idx = argv.index("--host")
    assert argv[idx + 1] == "127.0.0.1", (
        f"lock 1 (bind) failed: --host value is {argv[idx + 1]!r}, not 127.0.0.1"
    )


def _assert_lock_api_key(argv: list[str], *, key_file: Path) -> None:
    assert "--api-key-file" in argv, "lock 2 (api-key) missing: no --api-key-file flag"
    idx = argv.index("--api-key-file")
    assert argv[idx + 1] == str(key_file), (
        "lock 2 (api-key) failed: --api-key-file does not point at the "
        "configured key file"
    )
    # The secret's VALUE must never be a literal token on argv — only the
    # file PATH may appear (see scripts/llama-lock-flags.lib.sh header).
    assert "--api-key" not in argv, (
        "lock 2 (api-key) failed: --api-key (literal-value form) present on "
        "argv — ps could read the secret"
    )
    key_value = key_file.read_text().strip()
    assert key_value not in argv, (
        "lock 2 (api-key) failed: the key VALUE itself appears on argv"
    )


def _assert_lock_no_webui(argv: list[str]) -> None:
    assert "--no-webui" in argv, "lock 3 (no-webui) missing"


def _assert_all_locks(argv: list[str], *, key_file: Path) -> None:
    _assert_lock_host(argv)
    _assert_lock_api_key(argv, key_file=key_file)
    _assert_lock_no_webui(argv)


# ─────────────────── Rule 1 — effective invocation, happy path ───────────


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
class TestEffectiveInvocationCarriesAllLocks:
    def test_all_four_locks_present_on_effective_argv(self, name, tmp_path):
        script = ENDPOINTS[name]
        key_file = _make_key_file(tmp_path)
        rc, argv, stderr = _run_launcher(script, tmp_path, key_file=key_file)
        assert rc == 0, f"launcher failed: {stderr}"
        assert argv, "no argv captured — the stub was never exec'd"
        _assert_all_locks(argv, key_file=key_file)

    def test_fails_closed_when_key_file_missing(self, name, tmp_path):
        """A launcher with no provisioned key file must refuse to start
        rather than fall back to an unauthenticated/no-op mode."""
        script = ENDPOINTS[name]
        missing_key = tmp_path / "does-not-exist.txt"
        rc, argv, stderr = _run_launcher(script, tmp_path, key_file=missing_key)
        assert rc != 0
        assert argv == [], "launcher must not exec the model at all without a key"
        assert "llama API key file not found" in stderr


# ───────────────────── Rule 1 — per-lock neuter table ─────────────────────
#
# One neuter per lock, applied identically across every endpoint (since all
# three source the SAME scripts/llama-lock-flags.lib.sh — the whole point
# of the single-source-of-truth pattern is that a defect there breaks every
# endpoint at once, not that each endpoint carries its own copy to drift).
# Mutations touch a TMP COPY only; the autouse fixture above proves the
# tracked files were never touched by this module.

_NEUTER_MUTATIONS: dict[str, tuple[str, str]] = {
    "host": (
        _LOCK_LIB_ANCHOR,
        'LLAMA_LOCK_FLAGS=(--host 0.0.0.0 --api-key-file "$LLAMA_API_KEY_FILE" --no-webui)',
    ),
    "api_key": (
        _LOCK_LIB_ANCHOR,
        "LLAMA_LOCK_FLAGS=(--host 127.0.0.1 --no-webui)",
    ),
    "no_webui": (
        _LOCK_LIB_ANCHOR,
        'LLAMA_LOCK_FLAGS=(--host 127.0.0.1 --api-key-file "$LLAMA_API_KEY_FILE")',
    ),
}

_LOCK_CHECKS = {
    "host": _assert_lock_host,
    "api_key": lambda argv, key_file: _assert_lock_api_key(argv, key_file=key_file),
    "no_webui": _assert_lock_no_webui,
}


def _copy_scripts_tree(tmp_path: Path) -> Path:
    """Byte-identical copy of scripts/{llama-lock-flags.lib.sh, the three
    launchers} under *tmp_path*, verified via cmp before any mutation."""
    dest = tmp_path / "scripts"
    dest.mkdir(parents=True)
    shutil.copy2(LOCK_LIB, dest / LOCK_LIB.name)
    for path in ENDPOINTS.values():
        shutil.copy2(path, dest / path.name)
    # Prove the copy is byte-identical before we mutate anything in it.
    assert _sha256(dest / LOCK_LIB.name) == _sha256(LOCK_LIB)
    for path in ENDPOINTS.values():
        assert _sha256(dest / path.name) == _sha256(path)
    return dest


@pytest.mark.parametrize("endpoint_name", sorted(ENDPOINTS))
@pytest.mark.parametrize("lock_name", sorted(_NEUTER_MUTATIONS))
class TestNeuterEachLock:
    """Per-lock, per-endpoint table (STANDING TERMS: never an aggregate).

    For every (lock, endpoint) pair: neuter ONLY that lock in a tmp copy
    of the shared lib, run the copied launcher, and assert (a) the
    targeted lock's own check goes RED and (b) the OTHER two locks' checks
    stay GREEN — proving the guard is specific, not a shared failure mode
    that would go red for any old reason.
    """

    def test_neutering_this_lock_fails_only_this_lock(
        self, lock_name, endpoint_name, tmp_path
    ):
        anchor, mutated = _NEUTER_MUTATIONS[lock_name]
        pristine = LOCK_LIB.read_text()
        assert anchor in pristine, "neuter anchor drifted from the real lib file"
        mutated_text = pristine.replace(anchor, mutated)
        assert mutated_text != pristine

        scripts_copy = _copy_scripts_tree(tmp_path / "tree")
        (scripts_copy / LOCK_LIB.name).write_text(mutated_text)

        script = scripts_copy / ENDPOINTS[endpoint_name].name
        key_file = _make_key_file(tmp_path)
        rc, argv, stderr = _run_launcher(script, tmp_path, key_file=key_file)
        assert rc == 0, f"neutered launcher should still start: {stderr}"

        # The targeted lock's check must fail (RED).
        with pytest.raises(AssertionError):
            if lock_name == "api_key":
                _assert_lock_api_key(argv, key_file=key_file)
            else:
                _LOCK_CHECKS[lock_name](argv)

        # The OTHER two locks must still hold (proves the guard is
        # targeted, not a vacuous "something broke" catch-all).
        for other_name, other_check in _LOCK_CHECKS.items():
            if other_name == lock_name:
                continue
            if other_name == "api_key":
                _assert_lock_api_key(argv, key_file=key_file)
            else:
                other_check(argv)
