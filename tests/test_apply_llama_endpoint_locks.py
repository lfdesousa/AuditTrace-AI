"""M3 WU-4 review round 1 (F1/F4/F8/F9) — the apply script, its helper
library, and the five tracked systemd reference units (Rule 1, unit).

F4: before this fix round, zero tests referenced ``scripts/systemd/*.
service`` or ``scripts/apply-llama-endpoint-locks.sh``. The reviewer's own
neuter — unlocking BOTH reference units (chat bypassing the locked
launcher entirely, summariser back to ``--host 0.0.0.0`` with no key and
no ``--no-webui``) — left the full 212-test suite GREEN. This file closes
that gap two ways:

1. ``TestSystemdUnitFilesCarryLocks`` / ``TestNeuterEachUnit`` — statically
   parses each of the FIVE tracked unit files (never a running systemd —
   there is none in CI) and proves each per-unit check is falsifiable: a
   tmp-copy neuter of the SAME kind the reviewer applied must turn the
   check RED.
2. ``TestApplyScriptHelpers`` / ``TestApplyScriptEndToEnd`` — exercises
   the apply script's actual mutation logic (F9 preflight, F1 drop-in
   reconciliation + post-restart verification) through a fully sandboxed
   filesystem + a stubbed ``systemctl`` on ``PATH`` — no host root, no
   real systemd, nothing outside ``tmp_path`` is ever touched.

Per STANDING TERMS: one neuter per guard, tmp-copy only, per-guard table
never an aggregate, every RED captured on the actual assertion value. An
autouse fixture proves every tracked file's bytes never moved across the
whole module.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SYSTEMD_SRC_DIR = SCRIPTS_DIR / "systemd"
APPLY_SCRIPT = SCRIPTS_DIR / "apply-llama-endpoint-locks.sh"
HELPERS_LIB = SCRIPTS_DIR / "llama-apply-helpers.lib.sh"

UNIT_FILES = {
    "llama-server": SYSTEMD_SRC_DIR / "llama-server.service",
    "llama-embed-server": SYSTEMD_SRC_DIR / "llama-embed-server.service",
    "llama-summarizer": SYSTEMD_SRC_DIR / "llama-summarizer.service",
    "llama-fleet": SYSTEMD_SRC_DIR / "llama-fleet.service",
    "llama-mistral-server": SYSTEMD_SRC_DIR / "llama-mistral-server.service",
}
ACTIVE_UNITS = ("llama-server", "llama-embed-server", "llama-summarizer")
DORMANT_UNITS = ("llama-fleet", "llama-mistral-server")

# unit name -> expected launcher basename it must delegate to, exactly.
DELEGATING_UNITS = {
    "llama-server": "start-chat-llama.sh",
    "llama-embed-server": "start-embed-llama.sh",
    "llama-fleet": "start-fleet-llama.sh",
    "llama-mistral-server": "start-mistral-llama.sh",
}

TRACKED_FILES = [*UNIT_FILES.values(), APPLY_SCRIPT, HELPERS_LIB]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _tracked_files_never_mutated():
    """Every test in this module operates on tmp copies. This is the
    falsifiability backstop: if any test ever touched a tracked file,
    this fails loudly instead of silently leaving a locked-down unit or
    the apply script itself mutated on disk."""
    before = {p: _sha256(p) for p in TRACKED_FILES}
    yield
    after = {p: _sha256(p) for p in TRACKED_FILES}
    assert after == before, "a test mutated a tracked unit/apply-script file"


# ═══════════════════ unit-file content assertions (F4) ═══════════════════


def _execstart_value(text: str) -> str:
    """Concatenated ``ExecStart=...`` value, joining backslash continuations."""
    out: list[str] = []
    capture = False
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            capture = True
        if capture:
            out.append(line)
            if not line.rstrip().endswith("\\"):
                capture = False
    return "\n".join(out)


def _assert_delegating_unit_locked(text: str, launcher_name: str) -> None:
    execstart = _execstart_value(text)
    assert execstart, "no ExecStart= found"
    expected = f"ExecStart=/home/lfdesousa/work/AuditTrace-AI/scripts/{launcher_name}"
    assert execstart.strip() == expected, (
        "a delegating unit's ExecStart must be EXACTLY the locked launcher "
        f"path, nothing appended or swapped — expected {expected!r}, got "
        f"{execstart.strip()!r}"
    )


def _assert_inline_unit_locked(text: str) -> None:
    execstart = _execstart_value(text)
    assert "--host 127.0.0.1" in execstart, "lock 1 (bind) missing"
    assert "--host 0.0.0.0" not in execstart, "lock 1 (bind) failed: still 0.0.0.0"
    assert "--api-key-file" in execstart, "lock 2 (api-key) missing"
    assert "--api-key " not in execstart, (
        "lock 2 (api-key) failed: literal-value form present on ExecStart"
    )
    assert "--no-webui" in execstart, "lock 3 (no-webui) missing"


class TestSystemdUnitFilesCarryLocks:
    @pytest.mark.parametrize("unit_name", sorted(DELEGATING_UNITS))
    def test_delegating_unit_execstart_is_exactly_the_launcher(self, unit_name):
        text = UNIT_FILES[unit_name].read_text()
        _assert_delegating_unit_locked(text, DELEGATING_UNITS[unit_name])

    def test_summarizer_inline_execstart_carries_all_locks(self):
        text = UNIT_FILES["llama-summarizer"].read_text()
        _assert_inline_unit_locked(text)

    def test_summarizer_binary_matches_live_vulkan_build_not_older_rocm(self):
        """F2: byte-for-byte the live flags AND binary — not the older
        ROCm build under ~/opt/llamacpp (build 8568, March) the file
        pointed at before review round 1."""
        text = UNIT_FILES["llama-summarizer"].read_text()
        execstart = _execstart_value(text)
        assert "build-vulkan-new/bin/llama-server" in execstart
        assert "opt/llamacpp/llama-server" not in execstart

    def test_summarizer_and_chat_set_alias(self):
        """F10: no --alias leaks the raw --model PATH (username + model +
        quantisation) via llama.cpp's permanently-public /v1/models."""
        summarizer = _execstart_value(UNIT_FILES["llama-summarizer"].read_text())
        assert "--alias mistral-7b-summarizer" in summarizer
        chat_launcher = (SCRIPTS_DIR / "start-chat-llama.sh").read_text()
        assert "--alias" in chat_launcher

    @pytest.mark.parametrize("unit_name", sorted(DORMANT_UNITS))
    def test_dormant_units_still_carry_the_locked_launcher(self, unit_name):
        """F8: llama-fleet/llama-mistral-server are `disabled` today but
        must come up locked the moment anyone starts them."""
        text = UNIT_FILES[unit_name].read_text()
        _assert_delegating_unit_locked(text, DELEGATING_UNITS[unit_name])


# ─────────────── non-vacuity: neuter each unit, prove RED (F4) ───────────


class TestNeuterEachUnit:
    """One neuter per unit, per-unit table (STANDING TERMS: never an
    aggregate) — reproduces the EXACT class of neuter the reviewer applied
    (bypass the launcher entirely / re-introduce 0.0.0.0+no-webui) and
    proves the corresponding check goes RED."""

    @pytest.mark.parametrize("unit_name", sorted(DELEGATING_UNITS))
    def test_bypassing_the_launcher_fails_the_delegation_check(self, unit_name):
        pristine = UNIT_FILES[unit_name].read_text()
        launcher = DELEGATING_UNITS[unit_name]
        anchor = f"ExecStart=/home/lfdesousa/work/AuditTrace-AI/scripts/{launcher}"
        assert anchor in pristine, "neuter anchor drifted from the real unit file"
        # The reviewer's exact move: bypass the locked launcher, re-declare
        # the old unlocked inline invocation instead.
        neutered = pristine.replace(
            anchor, "ExecStart=/usr/bin/llama-server --host 0.0.0.0 --port 9"
        )
        assert neutered != pristine
        with pytest.raises(AssertionError):
            _assert_delegating_unit_locked(neutered, launcher)

    def test_reintroducing_0_0_0_0_fails_the_inline_check(self):
        pristine = UNIT_FILES["llama-summarizer"].read_text()
        assert "--host 127.0.0.1" in pristine
        neutered = pristine.replace("--host 127.0.0.1", "--host 0.0.0.0")
        assert neutered != pristine
        with pytest.raises(AssertionError):
            _assert_inline_unit_locked(neutered)
        # the OTHER two locks must still read as present — proves the
        # check is targeted, not a single catch-all string compare.
        execstart = _execstart_value(neutered)
        assert "--api-key-file" in execstart
        assert "--no-webui" in execstart

    def test_removing_api_key_file_fails_the_inline_check(self):
        pristine = UNIT_FILES["llama-summarizer"].read_text()
        assert '--api-key-file "$KEY_FILE"' in pristine
        neutered = pristine.replace('--api-key-file "$KEY_FILE" ', "")
        assert neutered != pristine
        with pytest.raises(AssertionError):
            _assert_inline_unit_locked(neutered)
        execstart = _execstart_value(neutered)
        assert "--host 127.0.0.1" in execstart
        assert "--no-webui" in execstart

    def test_removing_no_webui_fails_the_inline_check(self):
        pristine = UNIT_FILES["llama-summarizer"].read_text()
        assert "--no-webui" in pristine
        neutered = pristine.replace("--no-webui \\\n", "")
        assert neutered != pristine
        with pytest.raises(AssertionError):
            _assert_inline_unit_locked(neutered)
        execstart = _execstart_value(neutered)
        assert "--host 127.0.0.1" in execstart
        assert "--api-key-file" in execstart


# ═══════════════ apply-script helper library (F1 / F9, unit) ═════════════


def _run_bash(
    command: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", command],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _source_helpers(
    call: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return _run_bash(f'source "{HELPERS_LIB}"; {call}', env=env)


class TestCheckUnitPaths:
    """F9 — the preflight primitive."""

    def test_passes_when_every_referenced_path_exists(self, tmp_path):
        real_file = tmp_path / "launcher.sh"
        real_file.write_text("#!/usr/bin/env bash\ntrue\n")
        unit_file = tmp_path / "fake.service"
        unit_file.write_text(f"[Service]\nExecStart={real_file}\n")
        result = _source_helpers(f'check_unit_paths "{unit_file}"; echo "RC=$?"')
        assert "RC=0" in result.stdout, result.stderr

    def test_fails_and_names_every_missing_path(self, tmp_path):
        missing = tmp_path / "does-not-exist.sh"
        unit_file = tmp_path / "fake.service"
        unit_file.write_text(f"[Service]\nExecStart={missing}\n")
        result = _source_helpers(f'check_unit_paths "{unit_file}"; echo "RC=$?"')
        assert "RC=1" in result.stdout
        assert str(missing) in result.stderr

    def test_multiline_execstart_with_continuations_is_fully_parsed(self, tmp_path):
        bin_path = tmp_path / "bin"
        bin_path.write_text("x")
        model_path = tmp_path / "model.gguf"
        # model_path deliberately NOT created — must be reported missing.
        unit_file = tmp_path / "fake.service"
        unit_file.write_text(
            f"[Service]\nExecStart={bin_path} \\\n  --model {model_path} \\\n  --port 1\n"
        )
        result = _source_helpers(f'check_unit_paths "{unit_file}"; echo "RC=$?"')
        assert "RC=1" in result.stdout
        assert str(model_path) in result.stderr
        assert str(bin_path) not in result.stderr

    def test_comment_lines_are_never_treated_as_required_paths(self, tmp_path):
        """A comment mentioning an absolute path that doesn't exist must
        NOT fail the check — only Exec*/key-file directive lines count."""
        real_bin = tmp_path / "bin"
        real_bin.write_text("x")
        unit_file = tmp_path / "fake.service"
        unit_file.write_text(
            "# see /this/path/does/not/exist.sh for context\n"
            f"[Service]\nExecStart={real_bin}\n"
        )
        result = _source_helpers(f'check_unit_paths "{unit_file}"; echo "RC=$?"')
        assert "RC=0" in result.stdout, result.stderr


class TestReconcileExecstartDropins:
    """F1 — drop-in detection/reconciliation."""

    def _make_dropin_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "llama-server.service.d"
        d.mkdir()
        return d

    def test_dry_run_reports_but_never_renames(self, tmp_path):
        dropin_dir = self._make_dropin_dir(tmp_path)
        conf = dropin_dir / "20-override.conf"
        conf.write_text("[Service]\nExecStart=\nExecStart=/x --host 0.0.0.0\n")
        result = _source_helpers(
            f'reconcile_execstart_dropins llama-server "{tmp_path}" 1'
        )
        assert result.returncode == 0, result.stderr
        assert conf.exists(), "dry-run must not rename the drop-in"
        assert "+ mv" in result.stdout

    def test_real_run_renames_the_overriding_dropin(self, tmp_path):
        dropin_dir = self._make_dropin_dir(tmp_path)
        conf = dropin_dir / "20-override.conf"
        conf.write_text("[Service]\nExecStart=\nExecStart=/x --host 0.0.0.0\n")
        result = _source_helpers(
            f'reconcile_execstart_dropins llama-server "{tmp_path}" 0'
        )
        assert result.returncode == 0, result.stderr
        assert not conf.exists()
        renamed = dropin_dir / "20-override.conf.disabled-by-wu4-locks"
        assert renamed.exists()

    def test_non_overriding_dropin_left_untouched(self, tmp_path):
        """Only a drop-in that RESETS ExecStart= is disabled — proves the
        guard is targeted, not "delete every drop-in in the directory"."""
        dropin_dir = self._make_dropin_dir(tmp_path)
        benign = dropin_dir / "10-nice-to-have.conf"
        benign.write_text("[Service]\nNice=true\n")
        result = _source_helpers(
            f'reconcile_execstart_dropins llama-server "{tmp_path}" 0'
        )
        assert result.returncode == 0, result.stderr
        assert benign.exists(), "a non-ExecStart-resetting drop-in must survive"

    def test_no_dropin_directory_is_a_silent_noop(self, tmp_path):
        result = _source_helpers(
            f'reconcile_execstart_dropins llama-embed-server "{tmp_path}" 0'
        )
        assert result.returncode == 0, result.stderr


def _fake_systemctl(bin_dir: Path, *, execstart_value: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "systemctl"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "show" ]]; then\n'
        f'  echo "ExecStart={{ argv[]={execstart_value} ; }}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    script.chmod(0o755)


class TestVerifyEffectiveLocks:
    """F1 — one neuter per lock, per-lock table never an aggregate."""

    def test_all_locks_present_passes(self, tmp_path):
        _fake_systemctl(
            tmp_path,
            execstart_value="/x --host 127.0.0.1 --api-key-file /k --no-webui",
        )
        result = _source_helpers(
            "verify_effective_locks llama-server; echo RC=$?",
            env={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        )
        assert "RC=0" in result.stdout, result.stderr

    @staticmethod
    def _missing_bullets(stderr: str) -> str:
        """Just the 'is missing:' bullet block, not the trailing
        'effective value was: ...' dump (which necessarily repeats every
        flag that WAS present, including the ones that are NOT missing)."""
        return stderr.split("effective value was:")[0]

    def test_missing_host_lock_fails_only_that_lock(self, tmp_path):
        _fake_systemctl(
            tmp_path, execstart_value="/x --host 0.0.0.0 --api-key-file /k --no-webui"
        )
        result = _source_helpers(
            "verify_effective_locks llama-server; echo RC=$?",
            env={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        )
        assert "RC=1" in result.stdout
        bullets = self._missing_bullets(result.stderr)
        assert "--host 127.0.0.1" in bullets
        assert "--api-key-file" not in bullets
        assert "--no-webui" not in bullets

    def test_missing_api_key_lock_fails_only_that_lock(self, tmp_path):
        _fake_systemctl(tmp_path, execstart_value="/x --host 127.0.0.1 --no-webui")
        result = _source_helpers(
            "verify_effective_locks llama-server; echo RC=$?",
            env={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        )
        assert "RC=1" in result.stdout
        bullets = self._missing_bullets(result.stderr)
        assert "--api-key-file" in bullets
        assert "--host 127.0.0.1" not in bullets
        assert "--no-webui" not in bullets

    def test_missing_no_webui_lock_fails_only_that_lock(self, tmp_path):
        _fake_systemctl(
            tmp_path,
            execstart_value="/x --host 127.0.0.1 --api-key-file /k",
        )
        result = _source_helpers(
            "verify_effective_locks llama-server; echo RC=$?",
            env={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        )
        assert "RC=1" in result.stdout
        bullets = self._missing_bullets(result.stderr)
        assert "--no-webui" in bullets
        assert "--host 127.0.0.1" not in bullets
        assert "--api-key-file" not in bullets


# ═══════════════ apply-script end-to-end (F1 / F8 / F9, sandboxed) ═══════


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """A fully faked host: a repo checkout with the real apply script +
    helper library + all five (path-substituted) unit files, stub
    launchers/binaries/models, and an empty tmp "systemd directory" the
    script installs into. Nothing outside tmp_path is ever touched — the
    real script is invoked via SYSTEMD_DIR/LLAMA_API_KEY_FILE overrides
    exactly as its own header documents.
    """
    repo_scripts = tmp_path / "repo" / "scripts"
    repo_systemd = repo_scripts / "systemd"
    repo_systemd.mkdir(parents=True)
    shutil.copy2(APPLY_SCRIPT, repo_scripts / APPLY_SCRIPT.name)
    shutil.copy2(HELPERS_LIB, repo_scripts / HELPERS_LIB.name)

    bin_dir = tmp_path / "hostbin"
    bin_dir.mkdir()
    fake_llama_bin = bin_dir / "llama-server"
    fake_llama_bin.write_text("#!/usr/bin/env bash\ntrue\n")
    fake_llama_bin.chmod(0o755)
    fake_model = bin_dir / "model.gguf"
    fake_model.touch()
    fake_gpu_wait = bin_dir / "wait-for-vulkan-gpu.sh"
    fake_gpu_wait.write_text("#!/usr/bin/env bash\ntrue\n")
    fake_gpu_wait.chmod(0o755)
    fake_template = bin_dir / "template.jinja"
    fake_template.touch()

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    key_file = secrets_dir / "llama-api-key.txt"
    key_file.write_text("test-key\n")

    for launcher_name in DELEGATING_UNITS.values():
        p = repo_scripts / launcher_name
        p.write_text(
            '#!/usr/bin/env bash\necho "launched $0" >> "${FAKE_LOG:-/dev/null}"\n'
        )
        p.chmod(0o755)

    replacements = {
        "/home/lfdesousa/work/AuditTrace-AI/scripts": str(repo_scripts),
        "/home/lfdesousa/build/llama.cpp/build-vulkan-new/bin/llama-server": str(
            fake_llama_bin
        ),
        "/usr/local/bin/wait-for-vulkan-gpu.sh": str(fake_gpu_wait),
        "/home/lfdesousa/work/audittrace-private/secrets/llama-api-key.txt": str(
            key_file
        ),
    }
    for unit_name, src in UNIT_FILES.items():
        text = src.read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        text = re.sub(
            r"/home/lfdesousa/models/[A-Za-z0-9_.-]*\.gguf", str(fake_model), text
        )
        (repo_systemd / src.name).write_text(text)

    systemd_dir = tmp_path / "systemd-dir"
    systemd_dir.mkdir()

    return {
        "apply_script": repo_scripts / APPLY_SCRIPT.name,
        "repo_scripts": repo_scripts,
        "systemd_dir": systemd_dir,
        "key_file": key_file,
        "log_file": tmp_path / "log.txt",
    }


def _run_apply(
    sandbox: dict[str, Path],
    *args: str,
    fakebin: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LLAMA_API_KEY_FILE": str(sandbox["key_file"]),
        "SYSTEMD_DIR": str(sandbox["systemd_dir"]),
        "FAKE_LOG": str(sandbox["log_file"]),
    }
    if fakebin is not None:
        env["PATH"] = f"{fakebin}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(sandbox["apply_script"]), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestApplyScriptPreflight:
    def test_missing_launcher_aborts_before_any_mutation(self, sandbox):
        (sandbox["repo_scripts"] / "start-chat-llama.sh").unlink()
        result = _run_apply(sandbox, "--dry-run")
        assert result.returncode == 2
        assert "llama-server references path" in result.stderr
        assert not any(sandbox["systemd_dir"].iterdir()), (
            "preflight failure must leave the systemd dir untouched"
        )

    def test_missing_key_file_target_aborts(self, sandbox):
        sandbox["key_file"].unlink()
        result = _run_apply(sandbox, "--dry-run")
        assert result.returncode == 2
        assert "llama-summarizer references path" in result.stderr


class TestApplyScriptDryRun:
    def test_happy_path_dry_run_touches_nothing(self, sandbox):
        result = _run_apply(sandbox, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert not any(sandbox["systemd_dir"].iterdir()), (
            "dry-run must not install any unit file"
        )
        assert "== 0/5: preflight" in result.stdout
        assert "ok — every unit's launcher/binary/model/key-file path exists" in (
            result.stdout
        )
        assert "== 5/5: (dry-run" in result.stdout

    def test_dry_run_reports_dropin_without_touching_it(self, sandbox):
        dropin_dir = sandbox["systemd_dir"] / "llama-server.service.d"
        dropin_dir.mkdir()
        conf = dropin_dir / "20-permanent.conf"
        conf.write_text("[Service]\nExecStart=\nExecStart=/x --host 0.0.0.0\n")
        result = _run_apply(sandbox, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert conf.exists()
        assert "found ExecStart-overriding drop-in" in result.stdout


class TestApplyScriptRealRun:
    """Non-dry-run path — root check bypassed via the documented testing
    hook since every mutation already targets tmp_path (SYSTEMD_DIR) or a
    stubbed systemctl (PATH), never the real host."""

    def _real_run(self, sandbox, *, healed: bool, extra_env=None):
        fakebin = sandbox["systemd_dir"].parent / "fakebin"
        execstart = (
            "/x --host 127.0.0.1 --api-key-file /k --no-webui"
            if healed
            else "/x --host 0.0.0.0"
        )
        _fake_systemctl(fakebin, execstart_value=execstart)
        env = {"LLAMA_APPLY_ALLOW_NONROOT": "1"}
        if extra_env:
            env.update(extra_env)
        return _run_apply(sandbox, fakebin=fakebin, extra_env=env)

    def test_real_run_installs_all_five_units_and_passes_verification(self, sandbox):
        result = self._real_run(sandbox, healed=True)
        assert result.returncode == 0, result.stderr
        installed = {p.name for p in sandbox["systemd_dir"].glob("*.service")}
        assert installed == {f"{u}.service" for u in UNIT_FILES}
        assert "post-restart verification" in result.stdout
        for unit in ACTIVE_UNITS:
            assert f"ok — {unit} effective ExecStart" in result.stdout

    def test_dormant_units_are_never_started_or_restarted(self, sandbox):
        result = self._real_run(sandbox, healed=True)
        assert result.returncode == 0, result.stderr
        for dormant in DORMANT_UNITS:
            for line in result.stdout.splitlines():
                if line.strip().startswith("+ systemctl"):
                    assert dormant not in line, (
                        f"{dormant} must never be started/restarted (F8): {line}"
                    )

    def test_unhealed_effective_check_fails_the_whole_apply(self, sandbox):
        result = self._real_run(sandbox, healed=False)
        assert result.returncode == 3
        assert "EFFECTIVE ExecStart" in result.stderr

    def test_real_run_reconciles_overriding_dropin(self, sandbox):
        dropin_dir = sandbox["systemd_dir"] / "llama-server.service.d"
        dropin_dir.mkdir()
        conf = dropin_dir / "20-permanent.conf"
        conf.write_text("[Service]\nExecStart=\nExecStart=/x --host 0.0.0.0\n")
        result = self._real_run(sandbox, healed=True)
        assert result.returncode == 0, result.stderr
        assert not conf.exists()
        assert (dropin_dir / "20-permanent.conf.disabled-by-wu4-locks").exists()
