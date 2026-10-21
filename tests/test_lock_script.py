"""``scripts/lock.sh`` + the ``make lock*`` targets (DEP-LOCK-AT G1/G2, C10).

The real network compile is live evidence (AC-3), not a unit test. Here the
REAL ``lock.sh`` runs in a scratch root against a FAKE ``uv`` so every branch
of its own logic is exercised offline: tool resolution (CI-PARITY §2: .venv/bin
first, then PATH, then FAIL CLOSED), the exact compiler flags, the fresh-recompile
fixed-point diff (comment-stripped, body-sensitive), and the SSL_CERT_FILE
unset.

Neuters: delete the fail-closed ``exit 1`` -> test_no_uv_anywhere_fails_closed
RED; drop ``env -u SSL_CERT_FILE`` -> test_compile_unsets_ssl_cert_file RED;
make ``check`` ignore the body diff -> test_check_fails_on_a_changed_body RED.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._hermetic_env import hermetic_env

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lock.sh"
LOCKS = (
    "requirements.txt",
    "requirements-dev.txt",
    "bff/requirements.txt",
    "images/llm-stub/requirements.txt",
    "requirements-gate.txt",
)
BODY = "fastapi==0.146.0 \\\n    --hash=sha256:aa\n"


def _exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _scratch(tmp_path: Path, body: str = BODY) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "lock.sh")
    (root / ".python-version").write_text("3.12\n")
    for rel in LOCKS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# header recorded -o " + rel + "\n" + body)
    return root


FAKE_UV = """#!/usr/bin/env bash
# fake uv: records argv + SSL_CERT_FILE, then writes $FAKE_BODY to the -o target
echo "$*" >> "$FAKE_LOG"
echo "SSL=${SSL_CERT_FILE-<unset>}" >> "$FAKE_LOG"
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  case "$prev:$a" in -c:*.constraints)
    echo "CONSTRAINTS-BEGIN" >> "$FAKE_LOG"; cat "$a" >> "$FAKE_LOG"; echo "CONSTRAINTS-END" >> "$FAKE_LOG"
    if [ -n "${FAKE_FAIL_ON_CONSTRAINTS-}" ]; then echo "No solution found" >&2; exit 1; fi
    ;;
  esac
  prev="$a"
done
printf '# header recorded -o %s\\n%b' "$out" "$FAKE_BODY" > "$out"
"""


def _env(
    bin_dir: Path, tmp_path: Path, body: str = BODY, **extra: str
) -> dict[str, str]:
    # hermetic_env keeps the run-tests.sh CI-shape sentinels (shim dir first on
    # PATH, frozen KUBECONFIG) on this from-scratch child env.
    return hermetic_env(
        {
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "FAKE_LOG": str(tmp_path / "uv.log"),
            "FAKE_BODY": body,
            **extra,
        }
    )


def _run(
    root: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "lock.sh"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def fake_uv(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exec(bin_dir / "uv", FAKE_UV)
    return bin_dir


@pytest.mark.parametrize("sub", ["compile", "check", "freshness", "upgrade"])
def test_no_uv_anywhere_fails_closed(tmp_path: Path, sub: str) -> None:
    """Exactly exit 1 with the resolution message, for EVERY subcommand, and no
    downstream work: without the guard `compile` dies later with 127 and `check`
    would print per-lock failures, so neither can masquerade as the guard."""
    root = _scratch(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(root, _env(empty, tmp_path), sub)
    assert result.returncode == 1
    assert "uv not found" in result.stderr
    assert "cannot be recompiled" not in result.stderr
    assert "lock-check:" not in result.stdout


def test_dot_venv_uv_is_preferred_over_path_uv(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    (root / ".venv" / "bin").mkdir(parents=True)
    _exec(root / ".venv" / "bin" / "uv", FAKE_UV.replace("fake uv:", "VENV uv:"))
    result = _run(root, _env(fake_uv, tmp_path), "compile")
    assert result.returncode == 0, result.stderr
    # the venv uv ran (it shares the log), and the PATH uv never did: one set of
    # five compile lines, not ten.
    log = (tmp_path / "uv.log").read_text()
    assert log.count("pip compile") == 5


def test_path_uv_is_used_when_there_is_no_dot_venv(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    assert _run(root, _env(fake_uv, tmp_path), "compile").returncode == 0


def test_compile_uses_the_exact_flags_and_matrix(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    assert _run(root, _env(fake_uv, tmp_path), "compile").returncode == 0
    lines = [
        ln
        for ln in (tmp_path / "uv.log").read_text().splitlines()
        if "pip compile" in ln
    ]
    assert len(lines) == 5
    for line in lines:
        for flag in (
            "--universal",
            "--generate-hashes",
            "--allow-unsafe",
            "--python-version 3.12",
        ):
            assert flag in line, (flag, line)
    by_out = {ln.split(" -o ")[1].split()[0]: ln for ln in lines}
    assert set(by_out) == set(LOCKS)
    rt = by_out["requirements.txt"]
    assert "pyproject.toml requirements-build.in" in rt
    assert "--no-emit-package audittrace-object-storage" in rt
    dev = by_out["requirements-dev.txt"]
    assert "--extra dev" in dev and "-c requirements.txt" in dev
    assert "--no-emit-package audittrace-object-storage" in dev
    assert "-c requirements.txt" in by_out["bff/requirements.txt"]
    assert "-c requirements.txt" not in by_out["images/llm-stub/requirements.txt"]
    assert "requirements-gate.in" in by_out["requirements-gate.txt"]


def test_compile_unsets_ssl_cert_file(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    env = _env(fake_uv, tmp_path, SSL_CERT_FILE="/some/private/ca.crt")
    assert _run(root, env, "compile").returncode == 0
    log = (tmp_path / "uv.log").read_text()
    assert "SSL=<unset>" in log and "/some/private/ca.crt" not in log


def test_python_version_comes_from_dot_python_version(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    (root / ".python-version").write_text("3.13\n")
    assert _run(root, _env(fake_uv, tmp_path), "compile").returncode == 0
    assert "--python-version 3.13" in (tmp_path / "uv.log").read_text()


def test_an_empty_dot_python_version_fails_closed(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    (root / ".python-version").write_text("\n")
    result = _run(root, _env(fake_uv, tmp_path), "compile")
    assert result.returncode == 1 and ".python-version is empty" in result.stderr


def test_check_passes_when_the_recompile_reproduces_the_body(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    result = _run(root, _env(fake_uv, tmp_path), "check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("OK (fixed point") == 5


def test_check_ignores_comment_only_differences(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    for rel in LOCKS:  # the committed header names a different -o path
        (root / rel).write_text("# totally different header\n" + BODY + "    # via x\n")
    body = BODY + "    # via y\n"
    result = _run(root, _env(fake_uv, tmp_path, body=body), "check")
    assert result.returncode == 0, result.stderr


def test_check_fails_on_a_changed_body(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    tampered = "fastapi==0.146.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
    result = _run(root, _env(fake_uv, tmp_path, body=tampered), "check")
    assert result.returncode == 1
    assert "NOT the fixed point" in result.stderr
    assert "make lock" in result.stderr


def test_check_derives_a_versions_only_constraints_file_from_each_lock(
    tmp_path: Path, fake_uv: Path
) -> None:
    """N1: uv trusts hashes already present in its output file, so the check
    must NOT seed from the lock; it hands uv only `name==version [; marker]`
    constraints (no --hash lines, no continuation backslashes) and compiles
    fresh, so every hash is re-derived from the index."""
    root = _scratch(tmp_path)
    for rel in LOCKS:
        (root / rel).write_text(
            "# h\n"
            "colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:aa\n"
            "fastapi==0.146.0 \\\n    --hash=sha256:bb\n    # via x\n"
        )
    assert _run(root, _env(fake_uv, tmp_path), "check").returncode in (0, 1)
    log = (tmp_path / "uv.log").read_text()
    blocks = log.split("CONSTRAINTS-BEGIN\n")[1:]
    assert len(blocks) == 5
    for block in blocks:
        body = block.split("CONSTRAINTS-END")[0]
        assert body.splitlines() == [
            "colorama==0.4.6 ; sys_platform == 'win32'",
            "fastapi==0.146.0",
        ]


def test_check_never_seeds_the_recompile_from_the_committed_lock(
    tmp_path: Path, fake_uv: Path
) -> None:
    """The output path handed to uv must not exist beforehand (a pre-existing
    output file is trusted by uv, hashes included — the N1 hole)."""
    root = _scratch(tmp_path)
    marker = tmp_path / "preexisting.log"
    _exec(
        fake_uv / "uv",
        FAKE_UV.replace(
            "printf",
            f'[ -e "$out" ] && echo "PREEXISTING $out" >> "{marker}"\nprintf',
        ),
    )
    _run(root, _env(fake_uv, tmp_path), "check")
    assert not marker.exists()


def test_check_reports_a_lock_that_cannot_be_recompiled_under_its_pins(
    tmp_path: Path, fake_uv: Path
) -> None:
    """N2: a direct pin bumped in pyproject without `make lock` contradicts
    its own constraint; uv fails to resolve and the check is RED."""
    root = _scratch(tmp_path)
    result = _run(root, _env(fake_uv, tmp_path, FAKE_FAIL_ON_CONSTRAINTS="1"), "check")
    assert result.returncode == 1
    assert "cannot be recompiled under its own pins" in result.stderr
    assert "No solution found" in result.stderr


def test_check_recompiles_into_a_copy_and_leaves_the_committed_lock_untouched(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    before = (root / "requirements.txt").read_text()
    _run(
        root,
        _env(fake_uv, tmp_path, body="other==1 \\\n    --hash=sha256:bb\n"),
        "check",
    )
    assert (root / "requirements.txt").read_text() == before
    out_args = [
        ln.split(" -o ")[1].split()[0]
        for ln in (tmp_path / "uv.log").read_text().splitlines()
        if "pip compile" in ln
    ]
    assert all(o.startswith(os.environ.get("TMPDIR", "/tmp")) for o in out_args)


def test_freshness_compiles_with_upgrade_and_reports_a_diff(
    tmp_path: Path, fake_uv: Path
) -> None:
    root = _scratch(tmp_path)
    newer = "fastapi==0.147.0 \\\n    --hash=sha256:bb\n"
    result = _run(root, _env(fake_uv, tmp_path, body=newer), "freshness")
    assert result.returncode == 0
    assert "--upgrade" in (tmp_path / "uv.log").read_text()
    assert "### requirements.txt" in result.stdout
    assert "fastapi==0.147.0" in result.stdout


def test_upgrade_all_and_upgrade_one_package(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    assert _run(root, _env(fake_uv, tmp_path), "upgrade").returncode == 0
    log = (tmp_path / "uv.log").read_text()
    assert log.count("--upgrade ") + log.count("--upgrade\n") >= 5
    assert "--upgrade-package" not in log
    (tmp_path / "uv.log").unlink()
    assert _run(root, _env(fake_uv, tmp_path), "upgrade", "fastapi").returncode == 0
    assert (tmp_path / "uv.log").read_text().count("--upgrade-package fastapi") == 5


def test_an_unknown_subcommand_is_a_usage_error(tmp_path: Path, fake_uv: Path) -> None:
    root = _scratch(tmp_path)
    result = _run(root, _env(fake_uv, tmp_path), "bogus")
    assert result.returncode == 2 and "usage:" in result.stderr


def test_make_targets_are_thin_wrappers_around_the_script() -> None:
    out = subprocess.run(
        ["make", "-n", "lock", "lock-check", "lock-freshness", "lock-upgrade", "PKG=x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for sub in ("compile", "check", "freshness", "upgrade x"):
        assert f"bash scripts/lock.sh {sub}" in out


def test_the_script_matrix_names_exactly_the_five_locks() -> None:
    text = SCRIPT.read_text()
    for rel in LOCKS:
        assert f'"{rel}|' in text
