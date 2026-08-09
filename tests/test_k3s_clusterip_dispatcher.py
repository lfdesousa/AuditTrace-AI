"""Unit tests for the SPEC #443 v2 SAFE NetworkManager dispatcher
(``scripts/network/k3s_clusterip_dispatcher.py``).

Three tiers, cheapest/purest first:

1. :func:`decide` — the pure decision table, exhaustively branch-tested. This
   is where the mandatory **"guard GREEN -> dispatcher no-ops" neuter test**
   lives, paired with its non-vacuous companion (guard RED -> restart), plus
   every skip branch (unhandled action / debounced / lock held).
2. ``is_debounced`` / ``is_lock_held`` / the state-file I/O helpers — pure
   boundary-condition tests and real-filesystem round-trips via ``tmp_path``.
3. :func:`run_dispatch` — the full orchestration, with ``run_guard_fn`` /
   ``restart_fn`` INJECTED (never real subprocess) and ``now_ms`` INJECTED
   (never the real clock). This is where the **debounce/lock integration
   test** lives: two rapid events against the same ``tmp_path`` state dir,
   asserting the repair action fires at most once (no restart storm).

No real subprocess, no real systemctl, no real cluster, anywhere in this file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.network.k3s_clusterip_dispatcher as mod

# ── tier 1: decide() — the pure decision table ──────────────────────────────


def test_decide_unhandled_action_always_skips() -> None:
    # action_recognised=False must win regardless of the other flags.
    for debounced in (True, False):
        for lock_held in (True, False):
            for guard_healthy in (True, False):
                assert (
                    mod.decide(
                        action_recognised=False,
                        debounced=debounced,
                        lock_held=lock_held,
                        guard_healthy=guard_healthy,
                    )
                    == mod.ACTION_SKIP_IGNORED_EVENT
                )


def test_decide_debounced_skips_before_lock_or_guard_checked() -> None:
    assert (
        mod.decide(
            action_recognised=True, debounced=True, lock_held=True, guard_healthy=False
        )
        == mod.ACTION_SKIP_DEBOUNCED
    )


def test_decide_lock_held_skips_when_not_debounced() -> None:
    assert (
        mod.decide(
            action_recognised=True, debounced=False, lock_held=True, guard_healthy=False
        )
        == mod.ACTION_SKIP_LOCK_HELD
    )


def test_decide_green_guard_never_restarts() -> None:
    """THE mandatory "guard GREEN -> dispatcher no-ops" neuter test.

    With every skip-condition cleared and the guard reporting healthy, the
    decision table MUST return ACTION_NOOP_HEALTHY — it is structurally
    incapable of returning ACTION_RESTART_K3S here.
    """
    decision = mod.decide(
        action_recognised=True, debounced=False, lock_held=False, guard_healthy=True
    )
    assert decision == mod.ACTION_NOOP_HEALTHY
    assert decision != mod.ACTION_RESTART_K3S


def test_decide_red_guard_restarts() -> None:
    """The non-vacuous companion: flip ONLY guard_healthy and the decision
    moves to the one remaining branch — proves test_decide_green_guard_never_
    restarts is not vacuously true because decide() never restarts at all."""
    decision = mod.decide(
        action_recognised=True, debounced=False, lock_held=False, guard_healthy=False
    )
    assert decision == mod.ACTION_RESTART_K3S


# ── tier 2a: is_debounced / is_lock_held boundary conditions ───────────────


def test_is_debounced_none_last_event_is_never_debounced() -> None:
    assert mod.is_debounced(None, now_ms=10_000, debounce_seconds=30) is False


def test_is_debounced_true_just_inside_window() -> None:
    # 29999ms < 30000ms window
    assert mod.is_debounced(0, now_ms=29_999, debounce_seconds=30) is True


def test_is_debounced_false_exactly_at_window_edge() -> None:
    # 30000ms is NOT < 30000ms — boundary is exclusive, matches "just expired".
    assert mod.is_debounced(0, now_ms=30_000, debounce_seconds=30) is False


def test_is_lock_held_none_acquired_is_never_held() -> None:
    assert mod.is_lock_held(None, now_ms=10_000, lock_timeout_seconds=180) is False


def test_is_lock_held_true_within_timeout() -> None:
    assert mod.is_lock_held(0, now_ms=1_000, lock_timeout_seconds=180) is True


def test_is_lock_held_false_once_stale() -> None:
    assert mod.is_lock_held(0, now_ms=180_000, lock_timeout_seconds=180) is False


# ── tier 2b: state/lock file I/O round trips ────────────────────────────────


def test_last_event_round_trip(tmp_path: Path) -> None:
    assert mod.read_last_event_ms(tmp_path) is None
    mod.record_last_event(tmp_path, 12345)
    assert mod.read_last_event_ms(tmp_path) == 12345


def test_last_event_missing_file_reads_none(tmp_path: Path) -> None:
    assert mod.read_last_event_ms(tmp_path / "does-not-exist") is None


def test_last_event_corrupt_file_reads_none(tmp_path: Path) -> None:
    (tmp_path / mod.STATE_FILENAME).write_text("not json{{{", encoding="utf-8")
    assert mod.read_last_event_ms(tmp_path) is None


def test_lock_round_trip(tmp_path: Path) -> None:
    assert mod.read_lock_acquired_ms(tmp_path) is None
    mod.acquire_lock(tmp_path, 5000, pid=999)
    assert mod.read_lock_acquired_ms(tmp_path) == 5000
    mod.release_lock(tmp_path)
    assert mod.read_lock_acquired_ms(tmp_path) is None


def test_release_lock_missing_file_is_a_noop(tmp_path: Path) -> None:
    mod.release_lock(tmp_path)  # must not raise


def test_lock_corrupt_file_reads_none_fail_open_on_lock_only(tmp_path: Path) -> None:
    (tmp_path / mod.LOCK_FILENAME).write_text("not json{{{", encoding="utf-8")
    assert mod.read_lock_acquired_ms(tmp_path) is None


# ── external-effect seams (default_run_guard / default_restart_k3s) ────────


def test_default_restart_k3s_dry_run_never_calls_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        called["n"] += 1
        raise AssertionError("subprocess.run must not be called under dry_run")

    monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
    rc, detail = mod.default_restart_k3s(dry_run=True)
    assert rc == 0
    assert "DRY-RUN" in detail
    assert called["n"] == 0


def test_default_restart_k3s_real_run_invokes_systemctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc, _detail = mod.default_restart_k3s(dry_run=False)
    assert rc == 0
    assert seen["cmd"] == ["systemctl", "restart", "k3s"]


def test_default_run_guard_uses_exit_code_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        # stdout deliberately contradicts the exit code — proves the
        # dispatcher trusts ONLY the exit code, never the printed text.
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="RESULT: healthy (lies)", stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    healthy, stdout = mod.default_run_guard(Path("/fake/verify.py"), insecure=True)
    assert healthy is False
    assert "lies" in stdout


def test_default_run_guard_omits_insecure_flag_when_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.default_run_guard(Path("/fake/verify.py"), insecure=False)
    assert "--insecure" not in seen["cmd"]


# ── tier 3: run_dispatch() orchestration ────────────────────────────────────


def _guard(healthy: bool, detail: str = "") -> Any:
    return lambda: (healthy, detail)


def _counting_restart(
    rc: int = 0, detail: str = "restarted"
) -> tuple[Any, dict[str, int]]:
    calls = {"n": 0}

    def _fn() -> tuple[int, str]:
        calls["n"] += 1
        return rc, detail

    return _fn, calls


def test_run_dispatch_ignores_unhandled_action_no_side_effects(tmp_path: Path) -> None:
    guard_calls = {"n": 0}

    def guard_fn() -> tuple[bool, str]:
        guard_calls["n"] += 1
        return True, ""

    outcome = mod.run_dispatch(
        interface="wlan0",
        action="dhcp4-change",
        now_ms=1_000,
        state_dir=tmp_path,
        run_guard_fn=guard_fn,
        restart_fn=lambda: (0, ""),
    )
    assert outcome.decision == mod.ACTION_SKIP_IGNORED_EVENT
    assert guard_calls["n"] == 0
    assert not (tmp_path / mod.STATE_FILENAME).exists()
    assert not (tmp_path / mod.LOCK_FILENAME).exists()


def test_run_dispatch_green_guard_never_restarts(tmp_path: Path) -> None:
    """Integration-level restatement of the "GREEN -> no-op" neuter test:
    a healthy guard reaches run_dispatch() end-to-end and restart_fn is
    PROVABLY never invoked."""
    restart_fn, restart_calls = _counting_restart()

    outcome = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=1_000,
        state_dir=tmp_path,
        run_guard_fn=_guard(True, "all green"),
        restart_fn=restart_fn,
    )
    assert outcome.decision == mod.ACTION_NOOP_HEALTHY
    assert restart_calls["n"] == 0
    # state recorded (debounce window starts), lock released
    assert mod.read_last_event_ms(tmp_path) == 1_000
    assert mod.read_lock_acquired_ms(tmp_path) is None


def test_run_dispatch_red_guard_restarts_and_reverifies(tmp_path: Path) -> None:
    guard_results = iter([(False, "broken"), (True, "healed")])
    restart_fn, restart_calls = _counting_restart()

    outcome = mod.run_dispatch(
        interface="eth0",
        action="connectivity-change",
        now_ms=2_000,
        state_dir=tmp_path,
        run_guard_fn=lambda: next(guard_results),
        restart_fn=restart_fn,
    )
    assert outcome.decision == mod.ACTION_RESTART_K3S
    assert outcome.healed is True
    assert restart_calls["n"] == 1


def test_run_dispatch_red_guard_restart_does_not_heal_reports_false(
    tmp_path: Path,
) -> None:
    guard_results = iter([(False, "broken"), (False, "still broken")])
    restart_fn, restart_calls = _counting_restart()

    outcome = mod.run_dispatch(
        interface="eth0",
        action="down",
        now_ms=3_000,
        state_dir=tmp_path,
        run_guard_fn=lambda: next(guard_results),
        restart_fn=restart_fn,
    )
    assert outcome.decision == mod.ACTION_RESTART_K3S
    assert outcome.healed is False
    assert restart_calls["n"] == 1


def test_run_dispatch_debounce_prevents_restart_storm(tmp_path: Path) -> None:
    """The mandatory debounce test: two rapid events, both against an
    UNHEALTHY guard, must produce at most ONE restart — not two."""
    restart_fn, restart_calls = _counting_restart()
    guard_calls = {"n": 0}

    def guard_fn() -> tuple[bool, str]:
        guard_calls["n"] += 1
        return False, "still broken"

    first = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=1_000,
        state_dir=tmp_path,
        run_guard_fn=guard_fn,
        restart_fn=restart_fn,
        debounce_seconds=30,
    )
    # second event 5s later — well inside the 30s debounce window
    second = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=6_000,
        state_dir=tmp_path,
        run_guard_fn=guard_fn,
        restart_fn=restart_fn,
        debounce_seconds=30,
    )
    assert first.decision == mod.ACTION_RESTART_K3S
    assert second.decision == mod.ACTION_SKIP_DEBOUNCED
    assert restart_calls["n"] == 1, "debounce must cap restarts at one per window"
    assert guard_calls["n"] == 2, "first event's pre+post guard calls only"


def test_run_dispatch_after_debounce_window_processes_again(tmp_path: Path) -> None:
    restart_fn, restart_calls = _counting_restart()

    mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=1_000,
        state_dir=tmp_path,
        run_guard_fn=_guard(False, "broken"),
        restart_fn=restart_fn,
        debounce_seconds=10,
    )
    # 11s later — outside the 10s debounce window
    outcome = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=12_000,
        state_dir=tmp_path,
        run_guard_fn=_guard(False, "broken again"),
        restart_fn=restart_fn,
        debounce_seconds=10,
    )
    assert outcome.decision == mod.ACTION_RESTART_K3S
    assert restart_calls["n"] == 2


def test_run_dispatch_lock_held_skips_without_running_guard(tmp_path: Path) -> None:
    """A heal already in flight (lock held) must skip BEFORE the guard runs
    at all — the cheap check happens first."""
    mod.acquire_lock(tmp_path, now_ms=1_000, pid=424242)
    guard_calls = {"n": 0}

    def guard_fn() -> tuple[bool, str]:
        guard_calls["n"] += 1
        return True, ""

    outcome = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=1_500,  # 500ms after lock acquired, well within 180s timeout
        state_dir=tmp_path,
        run_guard_fn=guard_fn,
        restart_fn=lambda: (0, ""),
        lock_timeout_seconds=180,
    )
    assert outcome.decision == mod.ACTION_SKIP_LOCK_HELD
    assert guard_calls["n"] == 0
    # the pre-existing lock is untouched by a call that never acquired it
    assert mod.read_lock_acquired_ms(tmp_path) == 1_000


def test_run_dispatch_stale_lock_is_treated_as_not_held(tmp_path: Path) -> None:
    mod.acquire_lock(tmp_path, now_ms=0, pid=111)
    outcome = mod.run_dispatch(
        interface="wlan0",
        action="up",
        now_ms=200_000,  # 200s later, past the 180s default timeout
        state_dir=tmp_path,
        run_guard_fn=_guard(True, "green"),
        restart_fn=lambda: (0, ""),
        lock_timeout_seconds=180,
    )
    assert outcome.decision == mod.ACTION_NOOP_HEALTHY


def test_run_dispatch_lock_released_even_if_guard_raises(tmp_path: Path) -> None:
    def raising_guard() -> tuple[bool, str]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        mod.run_dispatch(
            interface="wlan0",
            action="up",
            now_ms=1_000,
            state_dir=tmp_path,
            run_guard_fn=raising_guard,
            restart_fn=lambda: (0, ""),
        )
    assert mod.read_lock_acquired_ms(tmp_path) is None, (
        "lock must not leak on exception"
    )


# ── CLI: env-resolution helpers ──────────────────────────────────────────────


def test_resolve_state_dir_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mod.STATE_DIR_ENV_VAR, raising=False)
    assert mod._resolve_state_dir() == mod.DEFAULT_STATE_DIR


def test_resolve_state_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, "/tmp/custom-state")
    assert mod._resolve_state_dir() == Path("/tmp/custom-state")


def test_resolve_int_env_default_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
    assert mod._resolve_int_env("SOME_UNSET_VAR", 42) == 42


def test_resolve_int_env_default_on_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_VAR", "not-an-int")
    assert mod._resolve_int_env("SOME_VAR", 42) == 42


def test_resolve_int_env_parses_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_VAR", "99")
    assert mod._resolve_int_env("SOME_VAR", 42) == 99


def test_resolve_dry_run_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mod.DRY_RUN_ENV_VAR, raising=False)
    assert mod._resolve_dry_run() is False


def test_resolve_dry_run_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mod.DRY_RUN_ENV_VAR, "1")
    assert mod._resolve_dry_run() is True


# ── CLI: main() ──────────────────────────────────────────────────────────────


def test_main_unhandled_action_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, str(tmp_path))
    rc = mod.main(["wlan0", "hostname"])
    assert rc == 0
    out = capsys.readouterr().out
    assert mod.ACTION_SKIP_IGNORED_EVENT in out


def test_main_green_guard_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(
        mod, "default_run_guard", lambda verify_script, *, insecure: (True, "ok")
    )

    def fail_restart(*, dry_run: bool) -> tuple[int, str]:
        raise AssertionError("must not restart on a healthy guard")

    monkeypatch.setattr(mod, "default_restart_k3s", fail_restart)
    rc = mod.main(["wlan0", "up"])
    assert rc == 0
    assert mod.ACTION_NOOP_HEALTHY in capsys.readouterr().out


def test_main_red_guard_restart_fails_to_heal_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(
        mod, "default_run_guard", lambda verify_script, *, insecure: (False, "broken")
    )
    monkeypatch.setattr(mod, "default_restart_k3s", lambda *, dry_run: (0, "restarted"))
    rc = mod.main(["eth0", "connectivity-change"])
    assert rc == 1
    out = capsys.readouterr().out
    assert mod.ACTION_RESTART_K3S in out


def test_main_red_guard_restart_heals_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, str(tmp_path))
    results = iter([(False, "broken"), (True, "healed")])
    monkeypatch.setattr(
        mod, "default_run_guard", lambda verify_script, *, insecure: next(results)
    )
    monkeypatch.setattr(mod, "default_restart_k3s", lambda *, dry_run: (0, "restarted"))
    rc = mod.main(["eth0", "connectivity-change"])
    assert rc == 0


def test_main_no_args_defaults_to_empty_strings_and_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mod.STATE_DIR_ENV_VAR, str(tmp_path))
    rc = mod.main([])
    assert rc == 0


# ── the module's own dataclass round trip ───────────────────────────────────


def test_dispatch_outcome_as_dict() -> None:
    outcome = mod.DispatchOutcome("restart-k3s", "did the thing", healed=True)
    assert outcome.as_dict() == {
        "decision": "restart-k3s",
        "detail": "did the thing",
        "healed": True,
    }


def test_json_read_write_round_trip_helper(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "f.json"
    mod._write_json(path, {"a": 1})
    assert mod._read_json(path) == {"a": 1}
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


def test_read_json_returns_none_for_non_dict_payload(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert mod._read_json(path) is None
