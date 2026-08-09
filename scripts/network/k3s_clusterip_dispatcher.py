#!/usr/bin/env python3
"""The SAFE NetworkManager dispatcher decision engine (SPEC #443 v2 s3).

This module is the **testable core** behind the installable NetworkManager
dispatcher hook ``scripts/network/NN-k3s-clusterip-guard`` (a thin shell
wrapper — NM dispatcher scripts must live at
``/etc/NetworkManager/dispatcher.d/<name>`` with no ``.`` in the filename,
see that file's header comment for why). NetworkManager invokes the
installed hook as root, once per interface event, with
``$1=interface $2=action``; the wrapper execs this module with the same two
positional arguments.

## Why this exists (and why v1's dispatcher was harmful)

The v1 dispatcher "repaired" the mesh by re-asserting a dummy interface and
pinned node-ip — logic that assumed a *specific, wrong* structural fix (see
``verify_clusterip_resilience.py``'s module docstring for the full incident).
That structural fix is GONE. This dispatcher performs **only the one proven,
bounded, known-safe repair action** — ``systemctl restart k3s``, which SPEC
#443 v2 s2 confirms is what actually recovers the mesh after a wifi<->wired
switch — and **only when the guard (``verify_clusterip_resilience.py``)
proves the pod->ClusterIP path is genuinely broken**. No steady-state host
change; the dispatcher does nothing on a switch that didn't break anything.

## Safety design (SPEC #443 v2 s3/s5, each independently falsifiable)

1. **Debounce** — a state file records the timestamp of the last COMPLETED
   dispatch. A new event within ``debounce_seconds`` of that timestamp is
   skipped outright (``ACTION_SKIP_DEBOUNCED``), before the guard is even
   invoked. This is what stops a restart storm when NM fires several events
   for one physical switch (a common real pattern: one interface's `down`
   plus another's `up`, sometimes both firing twice).
2. **Lock** — a separate lock file records "a heal is currently in flight"
   (acquired before running the guard, released in a ``finally``). A second
   event that arrives WHILE the first is still being processed is skipped
   (``ACTION_SKIP_LOCK_HELD``) rather than racing it. A lock older than
   ``lock_timeout_seconds`` is treated as abandoned/stale (a crashed prior
   run must never wedge the dispatcher shut forever).
3. **Only-if-broken** — the restart branch is reached ONLY when the guard's
   exit code says UNHEALTHY. A healthy guard always produces
   ``ACTION_NOOP_HEALTHY`` — this is the "GREEN -> no-op" neuter test's
   entire point (``tests/test_k3s_clusterip_dispatcher.py``): feed the
   decision table a healthy guard result and it MUST be structurally
   incapable of returning ``ACTION_RESTART_K3S``.
4. **Always re-verify, never assume healed** — after a restart, the guard is
   run again; the outcome records whether the restart actually worked
   (``DispatchOutcome.healed``). A restart that didn't fix anything is
   logged loudly (`STILL DEGRADED`), never silently claimed as success.

The ENTIRE restart-or-not policy lives in one pure function, :func:`decide`
— no I/O, no clock, no filesystem — so it is exhaustively unit-tested across
all branches. Everything below it (:func:`run_dispatch`) wires that pure
decision to injected seams (``run_guard_fn``, ``restart_fn``, ``now_ms``),
so the full orchestration is ALSO tested with zero real subprocess calls and
zero real clock reads — only :func:`main` (the CLI entrypoint NM actually
invokes) touches the real world.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess  # noqa: S404 - fixed argv, no shell; systemctl + the guard script
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── the three NM events SPEC #443 v2 s3 names — everything else is ignored so
#    this hook never fires on churn unrelated to which interface currently
#    owns the default route (dhcp4-change, hostname, vpn-*, ...). ──────────
HANDLED_ACTIONS = frozenset({"up", "down", "connectivity-change"})

DEFAULT_DEBOUNCE_SECONDS = 30
DEFAULT_LOCK_TIMEOUT_SECONDS = 180
DEFAULT_GUARD_TIMEOUT = 45
DEFAULT_RESTART_TIMEOUT = 60
DEFAULT_STATE_DIR = Path("/var/lib/audittrace/k3s-clusterip-guard")
STATE_FILENAME = "last-event.json"
LOCK_FILENAME = "heal.lock"

STATE_DIR_ENV_VAR = "CLUSTERIP_GUARD_STATE_DIR"
DEBOUNCE_ENV_VAR = "CLUSTERIP_GUARD_DEBOUNCE_SECONDS"
LOCK_TIMEOUT_ENV_VAR = "CLUSTERIP_GUARD_LOCK_TIMEOUT_SECONDS"
DRY_RUN_ENV_VAR = "CLUSTERIP_GUARD_DRY_RUN"

# ``main()``'s own default for the guard subprocess call — resolved lazily via
# a function (not a module-level constant) so tests can monkeypatch
# ``Path(__file__)`` behaviour freely without import-time side effects.
_VERIFY_SCRIPT_NAME = "verify_clusterip_resilience.py"

# ── decision vocabulary — the fixed set of outcomes `decide()` can return ──
ACTION_SKIP_IGNORED_EVENT = "skip-ignored-event"
ACTION_SKIP_DEBOUNCED = "skip-debounced"
ACTION_SKIP_LOCK_HELD = "skip-lock-held"
ACTION_NOOP_HEALTHY = "noop-healthy"
ACTION_RESTART_K3S = "restart-k3s"


@dataclass
class DispatchOutcome:
    """What one dispatcher invocation decided and (if it restarted) achieved."""

    decision: str
    detail: str
    healed: bool | None = None  # None unless decision == ACTION_RESTART_K3S

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "detail": self.detail, "healed": self.healed}


# ── the pure decision core (SPEC #443 v2 s3/s5 point 4, "safety") ─────────


def decide(
    *, action_recognised: bool, debounced: bool, lock_held: bool, guard_healthy: bool
) -> str:
    """The dispatcher's ENTIRE restart policy, as one pure decision table.

    Falsifiable by construction: flip any ONE input and the decision moves to
    exactly one other branch. In particular ``guard_healthy=True`` can NEVER
    reach ``ACTION_RESTART_K3S`` — the "GREEN -> no-op" neuter test asserts
    exactly that, and the "RED -> restart" companion test asserts the
    opposite is also true (this table is not vacuously always-noop).
    """
    if not action_recognised:
        return ACTION_SKIP_IGNORED_EVENT
    if debounced:
        return ACTION_SKIP_DEBOUNCED
    if lock_held:
        return ACTION_SKIP_LOCK_HELD
    if guard_healthy:
        return ACTION_NOOP_HEALTHY
    return ACTION_RESTART_K3S


def is_debounced(last_event_ms: int | None, now_ms: int, debounce_seconds: int) -> bool:
    """True iff a dispatch COMPLETED within ``debounce_seconds`` of ``now_ms``."""
    if last_event_ms is None:
        return False
    return (now_ms - last_event_ms) < (debounce_seconds * 1000)


def is_lock_held(
    lock_acquired_ms: int | None, now_ms: int, lock_timeout_seconds: int
) -> bool:
    """True iff a heal is in flight AND the lock is not stale/abandoned."""
    if lock_acquired_ms is None:
        return False
    return (now_ms - lock_acquired_ms) < (lock_timeout_seconds * 1000)


# ── state/lock file I/O (isolated so the pure logic above never touches a
#    filesystem — see tests/test_k3s_clusterip_dispatcher.py's split between
#    "pure decide()" tests and "run_dispatch() with tmp_path" tests) ───────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_last_event_ms(state_dir: Path) -> int | None:
    data = _read_json(state_dir / STATE_FILENAME)
    ms = data.get("last_event_ms") if data else None
    return int(ms) if isinstance(ms, int | float) else None


def record_last_event(state_dir: Path, now_ms: int) -> None:
    _write_json(state_dir / STATE_FILENAME, {"last_event_ms": now_ms})


def read_lock_acquired_ms(state_dir: Path) -> int | None:
    """A missing OR unparseable lock file reads as "not held" (fail-open on the
    lock only — never on the restart decision itself, which the guard still
    gates). A permanently corrupt lock file must not wedge healing shut forever.
    """
    data = _read_json(state_dir / LOCK_FILENAME)
    ms = data.get("acquired_ms") if data else None
    return int(ms) if isinstance(ms, int | float) else None


def acquire_lock(state_dir: Path, now_ms: int, pid: int) -> None:
    _write_json(state_dir / LOCK_FILENAME, {"acquired_ms": now_ms, "pid": pid})


def release_lock(state_dir: Path) -> None:
    (state_dir / LOCK_FILENAME).unlink(missing_ok=True)


# ── external-effect seams (the only ones ``main()`` wires to the real world) ──


def default_run_guard(verify_script: Path, *, insecure: bool) -> tuple[bool, str]:
    """Sole seam invoking the neuterable guard as a subprocess.

    The guard's EXIT CODE is the entire contract (0=healthy, 1=unhealthy) —
    this dispatcher never parses the guard's stdout/JSON to decide, only its
    exit code, so a future change to the guard's print format can never
    silently break the dispatcher's safety decision.
    """
    cmd = [sys.executable, str(verify_script)]
    if insecure:
        cmd.append("--insecure")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, check=False, timeout=DEFAULT_GUARD_TIMEOUT
    )
    return proc.returncode == 0, proc.stdout


def default_restart_k3s(*, dry_run: bool) -> tuple[int, str]:
    """Sole seam performing the ONE known-safe repair action — bounded, single attempt.

    Under ``dry_run`` (``CLUSTERIP_GUARD_DRY_RUN=1``), NO subprocess runs at
    all — mirrors ``scripts/deploy/mesh.py``'s ``MESH_RESETTLE_DRY_RUN``
    convention: every decision still logs, nothing actually executes.
    """
    if dry_run:
        return 0, "DRY-RUN: would run `systemctl restart k3s`"
    proc = subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
        ["systemctl", "restart", "k3s"],
        capture_output=True,
        text=True,
        check=False,
        timeout=DEFAULT_RESTART_TIMEOUT,
    )
    return proc.returncode, (proc.stderr or proc.stdout)


RunGuardFn = Callable[[], tuple[bool, str]]
RestartFn = Callable[[], tuple[int, str]]


# ── orchestration (wires the pure decision table to injected seams) ───────


def run_dispatch(
    *,
    interface: str,
    action: str,
    now_ms: int,
    state_dir: Path,
    run_guard_fn: RunGuardFn,
    restart_fn: RestartFn,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    lock_timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> DispatchOutcome:
    """The full, testable decision+action sequence for one NM dispatcher event.

    Every side effect (guard invocation, restart) is INJECTED
    (``run_guard_fn`` / ``restart_fn``), and the clock is INJECTED
    (``now_ms``) — so this function is exercised directly in tests with zero
    monkeypatching of module globals, zero real subprocess, zero real clock.
    Only :func:`main`'s CLI wiring touches env vars / the real clock / the
    real subprocess seams.
    """
    if action not in HANDLED_ACTIONS:
        decision = decide(
            action_recognised=False,
            debounced=False,
            lock_held=False,
            guard_healthy=False,
        )
        logger.info(
            "event: interface=%s action=%s -> %s (unhandled action)",
            interface,
            action,
            decision,
        )
        return DispatchOutcome(
            decision, f"action {action!r} not in {sorted(HANDLED_ACTIONS)}"
        )

    last_event_ms = read_last_event_ms(state_dir)
    debounced = is_debounced(last_event_ms, now_ms, debounce_seconds)
    if debounced and last_event_ms is not None:
        decision = decide(
            action_recognised=True, debounced=True, lock_held=False, guard_healthy=False
        )
        elapsed_ms = now_ms - last_event_ms
        logger.info("event: interface=%s action=%s -> %s", interface, action, decision)
        return DispatchOutcome(
            decision,
            f"last dispatch completed {elapsed_ms}ms ago, "
            f"< {debounce_seconds}s debounce window; skipping",
        )

    lock_acquired_ms = read_lock_acquired_ms(state_dir)
    lock_held = is_lock_held(lock_acquired_ms, now_ms, lock_timeout_seconds)
    if lock_held:
        decision = decide(
            action_recognised=True, debounced=False, lock_held=True, guard_healthy=False
        )
        logger.info("event: interface=%s action=%s -> %s", interface, action, decision)
        return DispatchOutcome(
            decision, "a heal is already in flight (lock held); skipping"
        )

    acquire_lock(state_dir, now_ms, os.getpid())
    try:
        healthy, guard_detail = run_guard_fn()
        if healthy:
            decision = decide(
                action_recognised=True,
                debounced=False,
                lock_held=False,
                guard_healthy=True,
            )
            logger.info(
                "event: interface=%s action=%s -> %s", interface, action, decision
            )
            return DispatchOutcome(
                decision,
                "guard reports the pod->ClusterIP path healthy; no action taken",
            )

        decision = decide(
            action_recognised=True,
            debounced=False,
            lock_held=False,
            guard_healthy=False,
        )
        logger.warning(
            "event: interface=%s action=%s -> %s: guard reported UNHEALTHY:\n%s",
            interface,
            action,
            decision,
            guard_detail.strip(),
        )
        restart_rc, restart_detail = restart_fn()
        post_healthy, post_detail = run_guard_fn()
        if post_healthy:
            logger.info(
                "HEALED — k3s restart (rc=%s) recovered the pod->ClusterIP path",
                restart_rc,
            )
        else:
            logger.error(
                "STILL DEGRADED after k3s restart (rc=%s): %s | post-verify: %s",
                restart_rc,
                restart_detail.strip(),
                post_detail.strip(),
            )
        return DispatchOutcome(
            decision,
            f"restarted k3s (rc={restart_rc}); post-verify healthy={post_healthy}",
            healed=post_healthy,
        )
    finally:
        record_last_event(state_dir, now_ms)
        release_lock(state_dir)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _resolve_state_dir() -> Path:
    raw = os.environ.get(STATE_DIR_ENV_VAR, "").strip()
    return Path(raw) if raw else DEFAULT_STATE_DIR


def _resolve_int_env(var: str, default: int) -> int:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_dry_run() -> bool:
    return os.environ.get(DRY_RUN_ENV_VAR, "0").strip() == "1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "SPEC #443 v2 — NetworkManager dispatcher hook: restarts k3s only "
            "when the guard proves the pod->ClusterIP path is genuinely broken."
        )
    )
    parser.add_argument("interface", nargs="?", default="")
    parser.add_argument("action", nargs="?", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    state_dir = _resolve_state_dir()
    debounce_seconds = _resolve_int_env(DEBOUNCE_ENV_VAR, DEFAULT_DEBOUNCE_SECONDS)
    lock_timeout_seconds = _resolve_int_env(
        LOCK_TIMEOUT_ENV_VAR, DEFAULT_LOCK_TIMEOUT_SECONDS
    )
    dry_run = _resolve_dry_run()
    verify_script = Path(__file__).resolve().parent / _VERIFY_SCRIPT_NAME

    outcome = run_dispatch(
        interface=args.interface,
        action=args.action,
        now_ms=int(time.time() * 1000),
        state_dir=state_dir,
        run_guard_fn=lambda: default_run_guard(verify_script, insecure=True),
        restart_fn=lambda: default_restart_k3s(dry_run=dry_run),
        debounce_seconds=debounce_seconds,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    print(f"[{outcome.decision}] {outcome.detail}")

    if outcome.decision == ACTION_RESTART_K3S and outcome.healed is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
