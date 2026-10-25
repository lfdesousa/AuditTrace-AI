"""LC-5 / M3-WU-4 -- ``scripts/setup-vault.sh`` seeds ``kv/audittrace/llm/api-key``
through STDIN (``vault kv put ... api_key=-``), never with the value on argv.

The seed function is sliced out of the script and run in bash against a stub
``vault_exec_stdin`` that records its argv and the stdin it received, so what is
asserted is behaviour: the recorded argv never contains the key, the recorded
stdin is exactly the key (whitespace stripped), and a value outside
``[A-Za-z0-9]{48}`` is refused (it is exported inside a single-quoted shell
template, PM-4).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._hermetic_env import hermetic_env

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-vault.sh"
KEY = "Zx9Qw8Er7Ty6" * 4  # 48 chars, [A-Za-z0-9]


def _function_source(name: str) -> str:
    text = SCRIPT.read_text()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    assert m, f"{name} not found in setup-vault.sh"
    return m.group(0)


def _run(tmp_path: Path, secret_text: str | None, function_src: str | None = None):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    if secret_text is not None:
        (secrets / "llama-api-key.txt").write_text(secret_text)
    argv_log, stdin_log = tmp_path / "argv.log", tmp_path / "stdin.log"
    program = f"""
set -euo pipefail
SECRETS_DIR="{secrets}"
KV_MOUNT=kv
vault_exec_stdin() {{ printf '%s\\n' "$@" > "{argv_log}"; cat > "{stdin_log}"; }}
{function_src or _function_source("seed_kv_stdin")}
seed_kv_stdin llm/api-key api_key llama-api-key.txt
"""
    result = subprocess.run(
        ["bash", "-c", program],
        env=hermetic_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    argv = argv_log.read_text().splitlines() if argv_log.exists() else None
    stdin = stdin_log.read_text() if stdin_log.exists() else None
    return result, argv, stdin


def test_the_value_goes_through_stdin_and_never_on_argv(tmp_path):
    result, argv, stdin = _run(tmp_path, KEY + "\n")
    assert result.returncode == 0, result.stderr
    assert argv == ["kv", "put", "kv/audittrace/llm/api-key", "api_key=-"]
    assert stdin == KEY, "stdin must be the key with the trailing newline stripped"
    assert KEY not in "".join(argv) and KEY not in result.stdout + result.stderr


def test_whitespace_is_stripped_before_the_put(tmp_path):
    result, _argv, stdin = _run(tmp_path, f"  {KEY} \r\n\n")
    assert result.returncode == 0 and stdin == KEY


@pytest.mark.parametrize(
    "bad", ["short", KEY + "x", KEY[:-1] + "!", KEY[:-1] + "'", ""]
)
def test_a_value_outside_the_alphabet_is_refused_and_nothing_is_sent(tmp_path, bad):
    result, argv, stdin = _run(tmp_path, bad + "\n")
    assert result.returncode != 0 and "is not [A-Za-z0-9]{48}" in result.stderr
    assert argv is None and stdin is None, "a refused value must not reach vault"


def test_a_missing_file_is_a_visible_skip_not_a_silent_success(tmp_path):
    result, argv, _ = _run(tmp_path, None)
    assert result.returncode == 0 and "skipped" in result.stdout and argv is None


def test_the_script_calls_the_stdin_seed_for_the_llm_path_and_not_the_argv_seed():
    text = SCRIPT.read_text()
    assert "seed_kv_stdin llm/api-key api_key llama-api-key.txt" in text
    assert not re.search(r"^seed_kv llm/", text, re.M), (
        "the llama key must not go through argv-based seed_kv"
    )


def test_neuter_the_stdin_seed_to_argv_is_caught(tmp_path):
    """Neuter: pass the value on argv instead of stdin -> the stdin and
    no-key-on-argv assertions above fail."""
    src = _function_source("seed_kv_stdin")
    anchor = 'printf \'%s\' "${value}" | vault_exec_stdin kv put "${KV_MOUNT}/audittrace/${path}" "${key}=-" >/dev/null'
    assert anchor in src
    neutered = src.replace(
        anchor,
        'vault_exec_stdin kv put "${KV_MOUNT}/audittrace/${path}" "${key}=${value}" >/dev/null',
    )
    result, argv, stdin = _run(tmp_path, KEY + "\n", neutered)
    assert argv is not None and KEY in "".join(argv), (
        "the neutered seed leaks the value on argv"
    )
    assert stdin == ""
