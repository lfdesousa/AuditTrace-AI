"""LC-5 / M3-WU-4 -- ``scripts/apply-llama-endpoint-locks.sh`` end to end, in a
sandbox with NO host effect.

``systemctl``, ``ss``, ``ip``, ``sleep``, ``runuser``, the CoreDNS read, the
llama binaries/models/key, ``/proc`` and ``/etc/systemd/system`` are all stubs
under ``tmp_path`` (tests/_llama_sandbox.py). The stub ``systemctl`` is
STATEFUL: it only sees unit files after ``daemon-reload``, resolves drop-ins
the way systemd does (a drop-in's ``ExecStart=`` wins), and on ``restart``
fabricates the process argv and the listening socket the unit's effective
command would produce, so the script's verification is exercised against
behaviour, not text.

Neuters: the script (or its lib) is copied to tmp, ONE guard is removed, and
the matching scenario must turn RED (the tracked files are hashed before and
after every test).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._llama_sandbox import (
    ACTIVE,
    CNI0_ADDR,
    DORMANT,
    KEY_VALUE,
    SCRIPTS_DIR,
    UNITS,
    ApplySandbox,
    copy_scripts_tree,
    make_apply_sandbox,
    sha_tree,
)

APPLY = SCRIPTS_DIR / "apply-llama-endpoint-locks.sh"
HELPERS = SCRIPTS_DIR / "llama-apply-helpers.lib.sh"
TRACKED = [APPLY, HELPERS, *sorted((SCRIPTS_DIR / "systemd").glob("*.service"))]

ACTIVE_RESTARTS = [f"restart {u}" for u in ACTIVE]


@pytest.fixture(autouse=True)
def _tracked_files_never_mutated():
    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = {p: digest(p) for p in TRACKED}
    yield
    assert {p: digest(p) for p in TRACKED} == before, "a test mutated a tracked file"


@pytest.fixture
def sb(tmp_path: Path) -> ApplySandbox:
    return make_apply_sandbox(tmp_path)


def _lock_state(sb: ApplySandbox) -> dict[str, str]:
    return {u: (sb.state / f"listen-{UNITS[u][1]}").read_text().strip() for u in ACTIVE}


# ═══════════════════════════════ --dry-run ═══════════════════════════════


class TestDryRun:
    def test_changes_nothing_and_plans_exactly_three_restarts(self, sb):
        before = sha_tree(sb.systemd_dir)
        result = sb.apply(APPLY, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert sha_tree(sb.systemd_dir) == before, (
            "--dry-run modified the unit directory"
        )
        assert sb.mutating_calls() == [], (
            "--dry-run must not call systemctl to change anything"
        )
        assert not sb.backup_root.exists(), "--dry-run must not create the backup dir"
        out = result.stdout
        assert len(re.findall(r"^\+ install -m 0644 .*\.service$", out, re.M)) == 5
        restarts = re.findall(r"^\+ systemctl restart (\S+)$", out, re.M)
        assert restarts == list(ACTIVE), (
            "only the three ACTIVE units, smallest model first"
        )
        assert (
            "restart llama-fleet" not in out
            and "restart llama-mistral-server" not in out
        )

    def test_prints_the_unit_binary_version_record(self, sb):
        out = sb.apply(APPLY, "--dry-run").stdout
        for unit in UNITS:
            assert re.search(
                rf"^  {unit} -> \S*llama-server -> version: 10288 \(360e1349f\)$",
                out,
                re.M,
            ), f"no 'unit -> binary -> version' record for {unit}"

    def test_dry_run_with_no_key_file_neither_generates_nor_fails(self, sb):
        sb.key_file.unlink()
        result = sb.apply(APPLY, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert not sb.key_file.exists()
        assert "generate-llama-api-key.sh" in result.stdout

    def test_prints_the_planned_drop_in_rename(self, sb):
        out = sb.apply(APPLY, "--dry-run").stdout
        assert "20-qwen38-mtp-permanent.conf.disabled-by-wu4-locks" in out
        assert (
            sb.systemd_dir / "llama-server.service.d/20-qwen38-mtp-permanent.conf"
        ).exists()


# ════════════════════════════════ --apply ════════════════════════════════


class TestApply:
    def test_happy_path(self, sb):
        key_before = sb.key_file.read_bytes()
        result = sb.apply(APPLY, "--apply")
        assert result.returncode == 0, result.stdout + result.stderr

        # units installed, placeholders substituted, pointing at THIS checkout
        for unit, (launcher, _port) in UNITS.items():
            text = (sb.systemd_dir / f"{unit}.service").read_text()
            assert "@SCRIPTS_DIR@" not in text and "@KEY_FILE@" not in text
            assert f"ExecStart={SCRIPTS_DIR}/{launcher}\n" in text
            assert f'Environment="LLAMA_API_KEY_FILE={sb.key_file}"' in text

        # drop-in: renamed, never deleted
        dropin_dir = sb.systemd_dir / "llama-server.service.d"
        assert not (dropin_dir / "20-qwen38-mtp-permanent.conf").exists()
        assert (
            dropin_dir / "20-qwen38-mtp-permanent.conf.disabled-by-wu4-locks"
        ).exists()

        # reload, then the three active units, one at a time, smallest model first
        assert sb.mutating_calls() == ["daemon-reload", *ACTIVE_RESTARTS]
        # dormant units: definition installed, NEVER started/enabled/stopped
        assert not any(u in c for c in sb.mutating_calls() for u in DORMANT)
        assert not any(
            c.split()[0] in ("enable", "start", "stop", "disable") for c in sb.calls()
        )

        # verification actually saw the locks
        for unit in ACTIVE:
            assert f"ok -- {unit}: effective ExecStart = launcher" in result.stdout
        assert _lock_state(sb) == {u: f"{CNI0_ADDR}:{UNITS[u][1]}" for u in ACTIVE}
        assert (
            f"NodeHosts == cni0: host.audittrace.local -> {CNI0_ADDR} == {CNI0_ADDR}  OK"
            in result.stdout
        )
        for unit in ACTIVE:
            assert re.search(
                rf"^  {unit} -> \S*llama-server -> version: 10288", result.stdout, re.M
            )
        for unit in DORMANT:
            assert f"dormant {unit} unchanged" in result.stdout

        # the existing key was left alone, and nothing printed it
        assert sb.key_file.read_bytes() == key_before
        assert KEY_VALUE not in result.stdout + result.stderr
        assert str(sb.newest_backup()) in result.stdout, (
            "the backup dir must be printed"
        )

    def test_backup_precedes_change_and_covers_all_five_units_and_the_drop_in(self, sb):
        pre = sha_tree(sb.systemd_dir)
        assert sb.apply(APPLY, "--apply").returncode == 0
        backup = sb.newest_backup()
        assert (backup / "MANIFEST").is_file() and (backup / "STATE").is_file()
        for unit in UNITS:
            assert (backup / f"{unit}.pre-wu4" / f"{unit}.service").is_file()
        for unit in UNITS:
            expected = {
                rel: d for rel, d in pre.items() if rel.startswith(f"{unit}.service")
            }
            assert sha_tree(backup / f"{unit}.pre-wu4") == expected, (
                f"{unit}: the backup is not byte-identical to the pre-apply unit + drop-ins"
            )
        assert any("20-qwen38-mtp-permanent.conf" in r for r in sha_tree(backup)), (
            "the ExecStart-resetting drop-in must be in the backup before it is renamed"
        )
        assert backup.stat().st_mode & 0o077 == 0, (
            "the backup dir must be private (0700)"
        )

    def test_second_apply_is_idempotent_and_does_not_restart_again(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        tree = sha_tree(sb.systemd_dir)
        (sb.state / "calls.log").write_text("")
        again = sb.apply(APPLY, "--apply")
        assert again.returncode == 0, again.stdout + again.stderr
        assert sha_tree(sb.systemd_dir) == tree
        assert [c for c in sb.mutating_calls() if c.startswith("restart")] == []
        assert again.stdout.count("already applied and verified -- not restarted") == 3

    def test_generates_a_valid_key_when_absent_and_never_prints_it(self, sb):
        sb.key_file.unlink()
        result = sb.apply(APPLY, "--apply")
        assert result.returncode == 0, result.stdout + result.stderr
        text = sb.key_file.read_text().strip()
        assert re.fullmatch(r"[A-Za-z0-9]{48}", text)
        assert oct(sb.key_file.stat().st_mode & 0o777) == "0o600"
        assert text not in result.stdout + result.stderr

    def test_preflight_refusal_changes_nothing(self, sb):
        (sb.home / "models" / "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf").unlink()
        before = sha_tree(sb.systemd_dir)
        result = sb.apply(APPLY, "--apply")
        assert result.returncode == 2, result.stdout + result.stderr
        assert (
            "llama-summarizer" in result.stderr and "model not found" in result.stderr
        )
        assert sha_tree(sb.systemd_dir) == before
        assert sb.mutating_calls() == [], "a refused preflight must not touch systemd"
        assert not sb.backup_root.exists(), (
            "a refused preflight must not even create a backup dir"
        )

    def test_preflight_refuses_when_cni0_has_no_address(self, sb):
        before = sha_tree(sb.systemd_dir)
        result = sb.apply(APPLY, "--apply", IP_STUB_ADDR="")
        assert result.returncode == 2
        assert sha_tree(sb.systemd_dir) == before and sb.mutating_calls() == []

    def test_preflight_refuses_a_missing_execstartpre_path(self, sb):
        tree = copy_scripts_tree(sb.root / "tree")
        unit = tree / "systemd/llama-server.service"
        unit.write_text(
            unit.read_text().replace(
                "ExecStart=@SCRIPTS_DIR@",
                "ExecStartPre=/nonexistent/wait-for-gpu.sh\nExecStart=@SCRIPTS_DIR@",
            )
        )
        before = sha_tree(sb.systemd_dir)
        result = sb.apply(tree / APPLY.name, "--apply")
        assert result.returncode == 2
        assert "/nonexistent/wait-for-gpu.sh" in result.stderr
        assert sha_tree(sb.systemd_dir) == before and sb.mutating_calls() == []

    def test_a_failed_restart_prints_the_rollback_command_and_exits_3(self, sb):
        result = sb.apply(APPLY, "--apply", SC_OMIT_NOWEBUI="1")
        assert result.returncode == 3
        assert "running argv lacks '--no-webui'" in result.stderr
        assert f"--rollback {sb.newest_backup()}" in result.stderr
        # stopped at the first unit: the 27B chat model is never reached
        assert [
            c for c in sb.mutating_calls() if c.startswith("restart")
        ] == ACTIVE_RESTARTS[:1]

    def test_refuses_to_run_as_non_root_without_the_test_seam(self, sb):
        env = sb.apply_env()
        del env["LLAMA_APPLY_ALLOW_NONROOT"]
        result = subprocess.run(
            ["bash", str(APPLY), "--apply"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1 and "must run as root" in result.stderr
        assert sb.mutating_calls() == []

    def test_usage_error(self, sb):
        result = sb.apply(APPLY)
        assert result.returncode == 1 and "usage:" in result.stderr


# ═══════════════════════════ --verify (read-only) ═══════════════════════════


class TestVerify:
    def test_detects_the_pre_lc5_state_where_the_drop_in_still_wins(self, sb):
        result = sb.apply(APPLY, "--verify")
        assert result.returncode == 3
        assert "effective ExecStart path is" in result.stderr
        assert "running argv lacks '--no-webui'" in result.stderr
        assert "running argv still binds 0.0.0.0" in result.stderr
        assert sb.mutating_calls() == []

    def test_passes_after_apply(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        result = sb.apply(APPLY, "--verify")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_nodehosts_mismatch_fails_loudly(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        result = sb.apply(
            APPLY, "--verify", KUBECTL_NODEHOSTS="10.42.0.9 host.audittrace.local"
        )
        assert result.returncode == 4
        assert "NodeHosts maps host.audittrace.local to '10.42.0.9'" in result.stderr

    def test_nodehosts_absent_entry_fails_loudly(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        result = sb.apply(
            APPLY, "--verify", KUBECTL_NODEHOSTS="10.42.0.1 something-else"
        )
        assert result.returncode == 4 and "'<absent>'" in result.stderr

    def test_unreadable_coredns_is_not_established_not_ok(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        result = sb.apply(APPLY, "--verify", KUBECTL_FAIL="1")
        assert result.returncode == 0
        assert "NodeHosts == cni0: NOT ESTABLISHED" in result.stdout
        assert (
            "OK"
            not in [ln for ln in result.stdout.splitlines() if "NodeHosts" in ln][0]
        )

    def test_a_dormant_unit_that_became_active_fails_the_verify(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        (sb.state / "force-active-llama-fleet").write_text("")
        result = sb.apply(
            APPLY, "--verify", LLAMA_APPLY_BACKUP_DIR=str(sb.newest_backup())
        )
        assert result.returncode == 3
        assert (
            "dormant unit state changed" in result.stderr
            and "llama-fleet" in result.stderr
        )

    def test_a_wildcard_listener_fails_the_verify(self, sb):
        result = sb.apply(APPLY, "--apply", SC_LISTEN_ANY="1")
        assert result.returncode == 3
        assert "expected only 10.42.0.1:11436" in result.stderr

    def test_a_wrong_host_in_the_running_argv_fails_the_verify(self, sb):
        result = sb.apply(APPLY, "--apply", SC_WRONG_HOST="10.42.0.77")
        assert result.returncode == 3 and f"lacks '--host {CNI0_ADDR}'" in result.stderr

    def test_a_missing_api_key_file_flag_fails_the_verify(self, sb):
        result = sb.apply(APPLY, "--apply", SC_OMIT_KEYFILE="1")
        assert result.returncode == 3 and "lacks '--api-key-file" in result.stderr


# ═════════════════════════════════ --rollback ═════════════════════════════════


class TestRollback:
    def test_restores_the_pre_apply_tree_byte_for_byte_and_restarts_the_three(self, sb):
        pre = sha_tree(sb.systemd_dir)
        assert sb.apply(APPLY, "--apply").returncode == 0
        assert sha_tree(sb.systemd_dir) != pre
        backup = sb.newest_backup()
        (sb.state / "calls.log").write_text("")
        result = sb.apply(APPLY, "--rollback", str(backup))
        assert result.returncode == 0, result.stdout + result.stderr
        assert sha_tree(sb.systemd_dir) == pre, (
            "rollback must restore units AND the drop-in exactly"
        )
        assert not list(sb.systemd_dir.rglob("*.disabled-by-wu4-locks"))
        assert sb.mutating_calls() == ["daemon-reload", *ACTIVE_RESTARTS]
        assert all(v.startswith("0.0.0.0:") for v in _lock_state(sb).values())

    def test_refuses_a_backup_whose_manifest_does_not_verify(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        backup = sb.newest_backup()
        victim = backup / "llama-server.pre-wu4/llama-server.service"
        victim.write_text(victim.read_text() + "# tampered\n")
        after_apply = sha_tree(sb.systemd_dir)
        (sb.state / "calls.log").write_text("")
        result = sb.apply(APPLY, "--rollback", str(backup))
        assert result.returncode == 1 and "MANIFEST does not verify" in result.stderr
        assert sha_tree(sb.systemd_dir) == after_apply and sb.mutating_calls() == []

    def test_refuses_without_a_backup_dir(self, sb):
        result = sb.apply(APPLY, "--rollback")
        assert result.returncode == 1 and "needs the backup dir" in result.stderr

    def test_refuses_a_partial_backup(self, sb):
        assert sb.apply(APPLY, "--apply").returncode == 0
        backup = sb.newest_backup()
        shutil.rmtree(backup / "llama-fleet.pre-wu4")
        result = sb.apply(APPLY, "--rollback", str(backup))
        assert result.returncode == 1 and "refusing a partial rollback" in result.stderr


# ═════════════════════════ running as the unit's user ═════════════════════════


class TestRunsAsTheUnitUser:
    def test_user_owned_binaries_and_launchers_go_through_runuser(self, sb):
        result = sb.apply(APPLY, "--dry-run", LLAMA_APPLY_FORCE_RUNUSER="1")
        assert result.returncode == 0, result.stderr
        users = (sb.state / "runuser.log").read_text().split()
        assert set(users) == {"lfdesousa"}
        # five launcher --check + five --version
        assert len(users) >= 10

    def test_without_the_force_seam_a_non_root_run_does_not_use_runuser(self, sb):
        sb.apply(APPLY, "--dry-run")
        assert not (sb.state / "runuser.log").exists()


# ════════════════════════════ helper library units ════════════════════════════


def _bash(
    sb: ApplySandbox, snippet: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{HELPERS}"\n{snippet}'],
        env=sb.apply_env(**env),
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestHelperLibrary:
    def test_unit_directive_joins_continuations_and_skips_comments(self, sb):
        unit = sb.root / "u.service"
        unit.write_text(
            "[Service]\n# ExecStart=/ignored\nExecStart=/a \\\n  --b\nUser=x\n"
        )
        out = _bash(sb, f'unit_directive "{unit}" ExecStart').stdout
        assert out.strip() == "/a  --b" or out.split() == ["/a", "--b"]

    def test_extract_exec_paths_strips_prefixes_and_takes_only_the_executable(self, sb):
        unit = sb.root / "u.service"
        unit.write_text(
            "[Service]\nExecStartPre=-/usr/bin/x --flag\nExecStart=@/opt/y arg\n"
        )
        out = _bash(sb, f'extract_exec_paths "{unit}"').stdout.split()
        assert out == ["/usr/bin/x", "/opt/y"]

    @pytest.mark.parametrize("bad", ["/a b/c", "/a;b", "/a|b", "/a&b", "relative/path"])
    def test_render_unit_refuses_paths_outside_the_conservative_alphabet(self, sb, bad):
        src = sb.root / "t.service"
        src.write_text("ExecStart=@SCRIPTS_DIR@/x\n")
        result = _bash(sb, f'render_unit "{src}" "{sb.root}/o.service" "{bad}" /k/key')
        assert result.returncode != 0 and "refusing to render units" in result.stderr
        result = _bash(sb, f'render_unit "{src}" "{sb.root}/o.service" /ok "{bad}"')
        assert result.returncode != 0

    def test_nodehosts_ip(self, sb):
        text = "10.42.0.1 host.audittrace.local\n192.168.1.113 pcluislinux"
        assert (
            _bash(sb, f'nodehosts_ip host.audittrace.local "{text}"').stdout.strip()
            == "10.42.0.1"
        )
        assert _bash(sb, f'nodehosts_ip nope "{text}"').stdout.strip() == ""

    def test_reconcile_leaves_a_drop_in_without_an_execstart_reset_alone(self, sb):
        d = sb.systemd_dir / "llama-embed-server.service.d"
        d.mkdir()
        conf = d / "10-env.conf"
        conf.write_text("[Service]\nEnvironment=FOO=1\n")
        _bash(
            sb, f'reconcile_execstart_dropins llama-embed-server "{sb.systemd_dir}" 0'
        )
        assert conf.exists() and not list(d.glob("*.disabled-by-wu4-locks"))

    def test_the_key_value_is_compared_without_a_command_argv(self):
        """The one place the key VALUE is compared (verify_unit_locked) must
        use a bash pattern match (a builtin), never grep/awk, whose argv `ps`
        can read."""
        body = HELPERS.read_text()
        fn = body[body.index("verify_unit_locked() {") : body.index("nodehosts_ip() {")]
        assert '"$cmdline" == *"$key_value"*' in fn
        assert not re.search(r"\b(grep|awk|sed)\b[^\n]*key_value", fn)


# ═════════════════════════════════ neuters ═════════════════════════════════
#
# Each: copy the whole scripts/ tree to tmp, remove ONE guard, run the matching
# scenario on the copy, and require the outcome the pristine script prevents.


def _neutered_apply(sb: ApplySandbox, file: str, old: str, new: str) -> Path:
    tree = copy_scripts_tree(sb.root / "tree")
    target = tree / file
    text = target.read_text()
    assert old in text, f"neuter anchor {old!r} drifted from {file}"
    target.write_text(text.replace(old, new, 1))
    return tree / APPLY.name


def test_neuter_drop_in_reconcile_is_caught_by_the_effective_exec_check(sb):
    script = _neutered_apply(
        sb,
        "apply-llama-endpoint-locks.sh",
        'reconcile_execstart_dropins "$unit" "$SYSTEMD_DIR" "$DRY_RUN"',
        "true",
    )
    result = sb.apply(script, "--apply")
    assert result.returncode == 3, "the drop-in kept winning and nothing noticed"
    assert "effective ExecStart path is" in result.stderr


def test_neuter_daemon_reload_is_caught(sb):
    script = _neutered_apply(
        sb, "apply-llama-endpoint-locks.sh", "run systemctl daemon-reload", "true"
    )
    assert sb.apply(script, "--apply").returncode == 3


def test_neuter_rename_to_delete_is_caught(sb):
    script = _neutered_apply(
        sb,
        "llama-apply-helpers.lib.sh",
        'mv "$conf" "$conf.disabled-by-wu4-locks"',
        'rm -f "$conf"',
    )
    assert sb.apply(script, "--apply").returncode == 0
    assert not list(sb.systemd_dir.rglob("*.disabled-by-wu4-locks")), (
        "pristine behaviour keeps the renamed drop-in; the neuter deleted it"
    )


def test_neuter_dormant_units_started_is_caught(sb):
    script = _neutered_apply(
        sb,
        "apply-llama-endpoint-locks.sh",
        "ACTIVE_UNITS=(llama-embed-server llama-summarizer llama-server)",
        "ACTIVE_UNITS=(llama-embed-server llama-summarizer llama-server llama-fleet)",
    )
    sb.apply(script, "--apply")
    assert any("llama-fleet" in c for c in sb.mutating_calls()), (
        "the neuter restarted a dormant unit; the happy-path test asserts none is touched"
    )


def test_neuter_backup_step_is_caught(sb):
    script = _neutered_apply(
        sb,
        "apply-llama-endpoint-locks.sh",
        "\nstep_backup\n",
        "\nBACKUP_DIR=/nonexistent\n",
    )
    sb.apply(script, "--apply")
    assert not sb.backup_root.exists(), (
        "backups are asserted to exist before any change; the neuter made none"
    )


def test_neuter_preflight_is_caught(sb):
    (sb.home / "models" / "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf").unlink()
    script = _neutered_apply(
        sb, "apply-llama-endpoint-locks.sh", "\nstep_preflight\n", "\ntrue\n"
    )
    result = sb.apply(script, "--apply")
    assert result.returncode != 2, "preflight no longer refuses a missing model"


@pytest.mark.parametrize(
    ("label", "old", "new", "env"),
    [
        (
            "no_webui_check",
            '[[ "$cmdline" == *" --no-webui "* ]] || problems+=("running argv lacks \'--no-webui\'")',
            "true",
            {"SC_OMIT_NOWEBUI": "1"},
        ),
        (
            "host_check",
            '[[ "$cmdline" == *" --host $addr "* ]] || problems+=("running argv lacks \'--host $addr\'")',
            "true",
            {"SC_WRONG_HOST": "10.42.0.77"},
        ),
        (
            "api_key_file_check",
            '[[ "$cmdline" == *" --api-key-file $key_file "* ]] || problems+=("running argv lacks \'--api-key-file <the configured key file>\'")',
            "true",
            {"SC_OMIT_KEYFILE": "1"},
        ),
        (
            "listener_check",
            '[[ "$l" == "$addr:$port" ]] || problems+=',
            "true || problems+=",
            {"SC_LISTEN_ANY": "1"},
        ),
    ],
)
def test_neuter_one_verify_check_lets_the_bad_state_through(sb, label, old, new, env):
    pristine = sb.apply(APPLY, "--apply", **env)
    assert pristine.returncode == 3, (
        f"{label}: the pristine script must refuse this state"
    )
    fresh = make_apply_sandbox(sb.root / "second")
    script = _neutered_apply(fresh, "llama-apply-helpers.lib.sh", old, new)
    assert fresh.apply(script, "--apply", **env).returncode == 0, (
        f"{label}: neutering this check should let the bad state through (RED for the guard)"
    )


def test_neuter_nodehosts_check_is_caught(sb):
    script = _neutered_apply(
        sb, "apply-llama-endpoint-locks.sh", "nodehosts_check || rc=$?", "true"
    )
    result = sb.apply(
        script, "--apply", KUBECTL_NODEHOSTS="10.42.0.9 host.audittrace.local"
    )
    assert result.returncode == 0, "pristine exits 4 here; the neuter hid the mismatch"


def test_neuter_key_never_overwritten_is_caught(sb):
    """Without the apply script's own 'already present' guard it asks the
    generator to overwrite; the generator refuses (a second guard), so the
    neutered run FAILS where the pristine happy path succeeds."""
    script = _neutered_apply(
        sb,
        "apply-llama-endpoint-locks.sh",
        'if [[ -f "$KEY_FILE" ]]; then',
        "if false; then",
    )
    before = sb.key_file.read_bytes()
    result = sb.apply(script, "--apply")
    assert result.returncode != 0 or sb.key_file.read_bytes() != before
    assert sb.key_file.read_bytes() == before, "an existing key must never change"
