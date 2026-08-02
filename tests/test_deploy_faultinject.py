"""Unit tests for the WS6 fault-injection certification runner — #384 WS6.

Hermetic by construction: every side-effecting seam (the kubectl/iptables/journald
subprocess ``_run``, the clock ``_monotonic``, the sleep, the signal handler) is
faked or monkeypatched, so NO destructive op ever runs. The reviewer's mandate
(spec §6) is proven here in both directions:

* **non-vacuous** — an injection that does not produce its expected signal FAILS
  the case (``test_certify_case_fails_when_fault_did_not_land``);
* **no false HEALED** — a case whose observed outcome differs from its declared
  expectation FAILS (``test_certify_case_no_false_healed_when_outcome_wrong``),
  and the negative control resolves to UNSAFE;
* **teardown-always** — an exception mid-injection and a budget timeout both still
  call restore (``test_certify_case_inject_raises_still_restores``,
  ``test_certify_case_budget_exceeded_self_restores``);
* **safety gate is real** — each of {missing flag, missing env, wrong node count,
  wrong context} independently blocks the live path with ``inject_fn`` never
  called (``test_guard_*`` + ``test_main_live_refused_never_injects``);
* **falsifiable coupling to mesh.py** — every non-negative case's expected signal
  is in the healer's routing set for its tier (``test_registry_integrity_*``).
"""

from __future__ import annotations

import argparse
import json
import signal as _signal
import subprocess
import threading

import pytest

from scripts.deploy import faultinject as fi
from scripts.deploy.mesh import (
    HEALED,
    HEALTHY,
    HOST_RESETTLE_WHITELIST,
    RBAC_RESTART_SIGNALS,
    UNSAFE,
    Diagnosis,
    Finding,
)

# ── fakes ─────────────────────────────────────────────────────────────────────

# The real _journal_cursor captured BEFORE any autouse patching, so the dedicated
# cursor tests can exercise the true implementation while every certify_case test
# gets the hermetic stub instead of shelling out to journalctl.
_REAL_JOURNAL_CURSOR = fi._journal_cursor


@pytest.fixture(autouse=True)
def _hermetic_journal_cursor(monkeypatch):
    """Keep certify_case hermetic: stub the pre-inject cursor capture.

    _journal_cursor now runs journalctl for EVERY case; without this every
    certify_case test would shell out. Returns a fixed fake cursor so the
    step-6 read receives a real (non-blank) value by default.
    """
    monkeypatch.setattr(fi, "_journal_cursor", lambda unit: "curs-pre-inject")


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _diag(*signals):
    return Diagnosis(findings=[Finding(s, "detail") for s in signals])


def _healthy():
    return Diagnosis(findings=[])


class _FakeResult:
    """A stand-in for MeshGateResult carrying only what certify_case reads."""

    def __init__(self, outcome, reason="fake reason"):
        self.outcome = outcome
        self.reason = reason


class _FakeGate:
    """Duck-typed GateLike: yields diagnoses in sequence, a fixed evaluate result.

    diagnose_mesh() walks the provided sequence and sticks on the last element, so
    step-0 (pre-state) and step-3 (post-inject) reads are independently scripted.
    """

    def __init__(self, diagnoses, result):
        self._diag = list(diagnoses)
        self._i = 0
        self._result = result
        self.diag_calls = 0
        self.eval_calls = 0

    def diagnose_mesh(self):
        self.diag_calls += 1
        d = self._diag[self._i]
        if self._i < len(self._diag) - 1:
            self._i += 1
        return d

    def evaluate(self):
        self.eval_calls += 1
        return self._result


def _case(
    *,
    cid="FX",
    tier=fi.TIER_RBAC,
    expected_signals=("istiod-not-ready",),
    expected_outcome=HEALED,
    inject_fn=None,
    teardown_fn=None,
    is_negative_control=False,
    skip_reason=None,
):
    return fi.FaultCase(
        id=cid,
        label=f"{cid} label",
        tier=tier,
        expected_signals=expected_signals,
        expected_outcome=expected_outcome,
        inject_fn=inject_fn or (lambda: None),
        teardown_fn=teardown_fn or (lambda: None),
        is_negative_control=is_negative_control,
        skip_reason=skip_reason,
    )


# ── pure falsifiability helpers ────────────────────────────────────────────────


def test_signal_landed_true_when_expected_present():
    assert fi.signal_landed(_diag("istiod-not-ready"), ("istiod-not-ready",)) is True


def test_signal_landed_false_when_absent():
    assert fi.signal_landed(_healthy(), ("istiod-not-ready",)) is False


def test_signal_landed_false_on_empty_expected():
    # a case must DECLARE what landing looks like — empty expected never lands.
    assert fi.signal_landed(_diag("istiod-not-ready"), ()) is False


def test_journal_shows_unit_fired_true_on_marker():
    text = "Aug 02 ... [mesh-resettle] EXECUTING; bounded op: systemctl restart k3s"
    assert fi.journal_shows_unit_fired(text) is True


def test_journal_shows_unit_fired_false_on_empty():
    assert fi.journal_shows_unit_fired("") is False


def test_journal_shows_unit_fired_false_on_unrelated():
    assert fi.journal_shows_unit_fired("some other log line") is False


# ── registry integrity: the falsifiable coupling to mesh.py ────────────────────


def test_registry_integrity_signals_couple_to_routing_sets():
    routing = RBAC_RESTART_SIGNALS | HOST_RESETTLE_WHITELIST
    for case in fi.REGISTRY.values():
        if case.is_negative_control:
            # the negative control is exempt from routing membership; it is
            # defined by its UNSAFE expectation instead.
            assert case.expected_outcome == UNSAFE
            continue
        for sig in case.expected_signals:
            assert sig in routing, f"{case.id} signal {sig} not in any routing set"


def test_registry_integrity_tier_matches_routing_set():
    for case in fi.REGISTRY.values():
        if case.is_negative_control:
            continue
        if case.tier == fi.TIER_RBAC:
            assert all(s in RBAC_RESTART_SIGNALS for s in case.expected_signals)
        elif case.tier == fi.TIER_HOST_ROOT:
            assert all(s in HOST_RESETTLE_WHITELIST for s in case.expected_signals)


def test_registry_core_crown_jewel_and_negative_control():
    assert fi.CORE_CASE_IDS == ("F1", "F2", "N1")
    # F2 is the #307 crown jewel: exactly the host-root signal.
    assert fi.REGISTRY["F2"].expected_signals == ("istiod-api-unreachable",)
    assert "istiod-api-unreachable" in HOST_RESETTLE_WHITELIST
    assert fi.REGISTRY["F2"].expected_outcome == HEALED
    # N1 is the negative control -> UNSAFE.
    assert fi.REGISTRY["N1"].is_negative_control is True
    assert fi.REGISTRY["N1"].expected_outcome == UNSAFE
    # F3 is a DECLARED skip, never a silent drop.
    assert fi.REGISTRY["F3"].skip_reason is not None


def test_cases_for_all_returns_core_in_order():
    assert [c.id for c in fi.cases_for("all")] == ["F1", "F2", "N1"]


def test_cases_for_single():
    assert [c.id for c in fi.cases_for("F2")] == ["F2"]


def test_cases_for_unknown_raises():
    with pytest.raises(KeyError):
        fi.cases_for("nope")


# ── certify_case: the 7-step DoD flow ──────────────────────────────────────────


def test_certify_case_happy_path_heals_and_restores():
    injected, restored = [], []
    case = _case(
        inject_fn=lambda: injected.append(1),
        teardown_fn=lambda: restored.append(1),
    )
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.passed
    assert res.injected is True
    assert res.restored is True
    assert res.observed_outcome == HEALED
    assert injected == [1]
    assert restored == [1]
    assert gate.eval_calls == 1


def test_certify_case_skip_returns_immediately_without_touching_gate():
    gate = _FakeGate([_healthy()], _FakeResult(HEALED))
    res = fi.certify_case(fi.REGISTRY["F3"], gate, budget_s=180)
    assert res.skipped
    assert res.verdict == fi.VERDICT_SKIP
    assert gate.diag_calls == 0  # never diagnosed, never injected


def test_certify_case_refuses_when_pre_state_not_healthy():
    injected = []
    case = _case(inject_fn=lambda: injected.append(1))
    gate = _FakeGate([_diag("istiod-not-ready")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "refused" in res.reason
    assert "pre-state not HEALTHY" in res.reason
    assert res.injected is False
    assert injected == []  # NEVER inject onto a broken mesh


def test_certify_case_fails_when_fault_did_not_land():
    # non-vacuous: diagnose is HEALTHY after "inject" -> the fault never landed.
    restored = []
    case = _case(teardown_fn=lambda: restored.append(1))
    gate = _FakeGate([_healthy(), _healthy()], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "did not land" in res.reason
    assert res.injected is True
    assert res.restored is True  # still restored
    assert restored == [1]


def test_certify_case_no_false_healed_when_outcome_wrong():
    # expected HEALED but the gate returns UNSAFE -> FAIL, never a false pass.
    case = _case(expected_outcome=HEALED)
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(UNSAFE))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "!= expected" in res.reason
    assert res.observed_outcome == UNSAFE


def test_certify_case_negative_control_unsafe_passes():
    # N1-shaped: expected UNSAFE, gate returns UNSAFE -> PASS (correct fail-closed).
    case = _case(
        cid="N1",
        tier=fi.TIER_NEGATIVE,
        expected_outcome=UNSAFE,
        is_negative_control=True,
    )
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(UNSAFE))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.passed
    assert res.observed_outcome == UNSAFE


def test_certify_case_negative_control_fails_on_false_healed():
    # the reviewer's scenario: a "healer" reports success while the fault persists.
    # A negative control that resolves to HEALED must FAIL the certification.
    case = _case(
        cid="N1",
        tier=fi.TIER_NEGATIVE,
        expected_outcome=UNSAFE,
        is_negative_control=True,
    )
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "!= expected" in res.reason


def test_certify_case_budget_exceeded_self_restores(monkeypatch):
    seq = iter([0.0, 1000.0])
    monkeypatch.setattr(fi, "_monotonic", lambda: next(seq))
    restored = []
    case = _case(teardown_fn=lambda: restored.append(1))
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "exceeded budget" in res.reason
    assert res.restored is True  # the runner restores itself on timeout
    assert restored == [1]


def test_certify_case_inject_raises_still_restores():
    restored = []

    def _boom():
        raise RuntimeError("break failed")

    case = _case(inject_fn=_boom, teardown_fn=lambda: restored.append(1))
    gate = _FakeGate([_healthy()], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "unexpected error" in res.reason
    assert res.restored is True  # teardown-always even when inject explodes
    assert restored == [1]


def test_certify_case_teardown_error_is_recorded():
    def _bad_restore():
        raise RuntimeError("restore blew up")

    case = _case(teardown_fn=_bad_restore)
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    # the heal itself passed; the restore attempt failed and is surfaced honestly.
    assert res.passed
    assert res.restored is False
    assert res.teardown_error == "restore blew up"


def test_certify_case_host_root_requires_journal(monkeypatch):
    # falsifiable: the step-6 read must be scoped by the PRE-INJECT cursor, so
    # capture what after_cursor certify_case threads through.
    seen = {}

    def _fake_read(unit, *, after_cursor):
        seen["after_cursor"] = after_cursor
        return "[mesh-resettle] EXECUTING"

    monkeypatch.setattr(fi, "_read_journal", _fake_read)
    case = _case(
        cid="F2",
        tier=fi.TIER_HOST_ROOT,
        expected_signals=("istiod-api-unreachable",),
        expected_outcome=HEALED,
    )
    gate = _FakeGate([_healthy(), _diag("istiod-api-unreachable")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.passed
    assert res.journal_confirmed is True
    # the cursor captured before inject (the hermetic stub) reaches the read.
    assert seen["after_cursor"] == "curs-pre-inject"


def test_certify_case_signal_mid_run_restores_exactly_once():
    # A SIGINT during injection fires the handler (which restores + re-raises)
    # AND the ExitStack callback on unwind. The idempotency guard means restore
    # runs EXACTLY once, and the abort still leaves the mesh restored.
    calls = []

    def _inject():
        _signal.raise_signal(_signal.SIGINT)

    case = _case(inject_fn=_inject, teardown_fn=lambda: calls.append(1))
    gate = _FakeGate([_healthy()], _FakeResult(HEALED))
    with pytest.raises(KeyboardInterrupt):
        fi.certify_case(case, gate, budget_s=180)
    assert calls == [1]  # two restore triggers, one actual restore


def test_certify_case_host_root_fails_without_journal(monkeypatch):
    # heal reports HEALED but NO journald record -> not auditable -> FAIL.
    monkeypatch.setattr(fi, "_read_journal", lambda unit, *, after_cursor: "")
    case = _case(
        cid="F2",
        tier=fi.TIER_HOST_ROOT,
        expected_signals=("istiod-api-unreachable",),
        expected_outcome=HEALED,
    )
    gate = _FakeGate([_healthy(), _diag("istiod-api-unreachable")], _FakeResult(HEALED))
    res = fi.certify_case(case, gate, budget_s=180)
    assert res.verdict == fi.VERDICT_FAIL
    assert "no journald record" in res.reason
    assert res.journal_confirmed is False


# ── certify: the batch + report ────────────────────────────────────────────────


def test_certify_batch_all_pass_is_ok():
    good = _case(cid="A")
    neg = _case(
        cid="B",
        tier=fi.TIER_NEGATIVE,
        expected_outcome=UNSAFE,
        is_negative_control=True,
    )
    gates = {
        "A": _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED)),
    }

    # run them one at a time so each gets a fresh gate
    r1 = fi.certify_case(good, gates["A"], budget_s=180)
    r2 = fi.certify_case(
        neg,
        _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(UNSAFE)),
        budget_s=180,
    )
    report = fi.CertificationReport(results=[r1, r2], budget_s=180)
    assert report.ok is True
    assert report.as_dict()["counts"] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "skipped": 0,
    }


def test_certify_runs_multiple_cases_via_certify_fn():
    cases = [_case(cid="A"), _case(cid="B")]
    # a single gate reused: each case does healthy->degraded, evaluate HEALED.
    # _FakeGate sticks on the last diagnosis, so after the first case it stays
    # degraded; give each case its own gate by wrapping.
    results = []
    for c in cases:
        g = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
        results.append(fi.certify_case(c, g, budget_s=180))
    report = fi.CertificationReport(results=results, budget_s=180)
    assert report.ok is True
    assert len(report.ran) == 2


def test_certify_fn_end_to_end():
    # exercise certify() itself (not just certify_case) with a single case.
    case = _case(cid="A")
    gate = _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))
    report = fi.certify([case], gate, budget_s=180)
    assert report.ok is True


def test_report_ok_false_on_any_fail():
    passed = fi.CaseResult("A", "a", fi.TIER_RBAC, HEALED, verdict=fi.VERDICT_PASS)
    failed = fi.CaseResult("B", "b", fi.TIER_RBAC, HEALED, verdict=fi.VERDICT_FAIL)
    report = fi.CertificationReport(results=[passed, failed], budget_s=180)
    assert report.ok is False


def test_report_ok_false_when_all_skipped():
    skipped = fi.CaseResult(
        "F3", "f3", fi.TIER_HOST_ROOT, HEALED, verdict=fi.VERDICT_SKIP
    )
    report = fi.CertificationReport(results=[skipped], budget_s=180)
    assert report.ok is False  # a report of only skips is not a certification
    assert report.ran == []


def test_report_human_summary_contains_verdicts():
    r = fi.CaseResult(
        "F1",
        "istiod",
        fi.TIER_RBAC,
        HEALED,
        verdict=fi.VERDICT_PASS,
        observed_outcome=HEALED,
        reason="ok",
    )
    summary = fi.CertificationReport(results=[r], budget_s=180).human_summary()
    assert "PASS" in summary
    assert "[F1]" in summary


# ── record serialisation ───────────────────────────────────────────────────────


def test_faultcase_as_dict():
    d = fi.REGISTRY["F1"].as_dict()
    assert d["id"] == "F1"
    assert d["tier"] == fi.TIER_RBAC
    assert d["expected_outcome"] == HEALED


def test_caseresult_as_dict_and_props():
    r = fi.CaseResult("F1", "l", fi.TIER_RBAC, HEALED, verdict=fi.VERDICT_PASS)
    assert r.passed is True
    assert r.skipped is False
    d = r.as_dict()
    assert d["verdict"] == fi.VERDICT_PASS
    assert d["case_id"] == "F1"


# ── the live (real) fault ops: argv asserted, nothing executed ─────────────────


def test_inject_istiod_degraded_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    fi.inject_istiod_degraded()
    assert seen[0][:3] == ["kubectl", "delete", "pod"]
    assert "app=istiod" in seen[0]


def test_restore_istiod_degraded_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    fi.restore_istiod_degraded()
    assert any("rollout" in c and "restart" in c for c in seen)
    assert any("rollout" in c and "status" in c for c in seen)


def test_inject_clusterip_route_broken_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    fi.inject_clusterip_route_broken()
    assert seen[0][0] == "iptables"
    assert "-I" in seen[0]
    assert fi.KUBE_API_CLUSTERIP in seen[0]
    assert "REJECT" in seen[0]


def test_restore_clusterip_route_is_idempotent(monkeypatch):
    # a non-zero delete (rule already gone) must NOT raise — fail-safe restore.
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "No chain/target"))
    fi.restore_clusterip_route()  # no exception


def test_restore_clusterip_route_suppresses_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("iptables missing")

    monkeypatch.setattr(fi, "_run", _boom)
    fi.restore_clusterip_route()  # suppressed, no raise


def test_inject_unfixable_istiod_scaled_zero_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    fi.inject_unfixable_istiod_scaled_zero()
    assert "scale" in seen[0]
    assert "--replicas=0" in seen[0]


def test_restore_istiod_scaled_zero_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    fi.restore_istiod_scaled_zero()
    assert any("--replicas=1" in c for c in seen)


def test_f3_placeholder_raises():
    with pytest.raises(NotImplementedError):
        fi._f3_not_injectable()


def test_exec_or_raise_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "boom"))
    with pytest.raises(fi.FaultInjectionError):
        fi._exec_or_raise(["kubectl", "get", "x"])


def test_exec_or_raise_raises_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("kubectl not found")

    monkeypatch.setattr(fi, "_run", _boom)
    with pytest.raises(fi.FaultInjectionError):
        fi._exec_or_raise(["kubectl", "get", "x"])


def test_exec_or_raise_ok(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "done"))
    fi._exec_or_raise(["kubectl", "get", "x"])  # no raise


# ── low-level seams (exercised once for coverage honesty) ──────────────────────


def test_run_executes_real_subprocess():
    proc = fi._run(["printf", "hi"])
    assert proc.returncode == 0 and proc.stdout == "hi"


def test_sleep_monotonic_and_now():
    fi._sleep(0.0)
    assert isinstance(fi._monotonic(), float)
    assert "T" in fi._now_iso()


# ── journald cursor + read (timezone-independent host-root proof) ──────────────


def test_journal_cursor_parses_token_from_stderr(monkeypatch):
    # journalctl prints the cursor on stderr as "-- cursor: <token>".
    seen = {}

    def _fake(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, stdout="", stderr="-- cursor: s=abc123;i=42;b=deadbeef\n")

    monkeypatch.setattr(fi, "_run", _fake)
    assert _REAL_JOURNAL_CURSOR("u") == "s=abc123;i=42;b=deadbeef"
    # captured with -n0 --show-cursor so no entries are read, just the cursor.
    assert "-n0" in seen["cmd"] and "--show-cursor" in seen["cmd"]


def test_journal_cursor_blank_when_no_cursor_line(monkeypatch):
    # a run that emits no "-- cursor:" line yields '' (stale/blank cursor path).
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "no cursor here", ""))
    assert _REAL_JOURNAL_CURSOR("u") == ""


def test_journal_cursor_blank_on_nonzero(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "err"))
    assert _REAL_JOURNAL_CURSOR("u") == ""


def test_journal_cursor_blank_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("journalctl missing")

    monkeypatch.setattr(fi, "_run", _boom)
    assert _REAL_JOURNAL_CURSOR("u") == ""


def test_read_journal_uses_after_cursor(monkeypatch):
    seen = {}

    def _fake(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, "log lines")

    monkeypatch.setattr(fi, "_run", _fake)
    assert fi._read_journal("u", after_cursor="curs-x") == "log lines"
    assert "--after-cursor" in seen["cmd"]
    assert "curs-x" in seen["cmd"]


def test_read_journal_blank_cursor_omits_after_cursor(monkeypatch):
    # a blank cursor (capture failed) degrades to a whole-unit read, no
    # --after-cursor flag; still fail-closed via the marker check upstream.
    seen = {}

    def _fake(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, "log lines")

    monkeypatch.setattr(fi, "_run", _fake)
    assert fi._read_journal("u", after_cursor="") == "log lines"
    assert "--after-cursor" not in seen["cmd"]


def test_read_journal_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "err"))
    assert fi._read_journal("u", after_cursor="x") == ""


def test_read_journal_empty_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("journalctl missing")

    monkeypatch.setattr(fi, "_run", _boom)
    assert fi._read_journal("u", after_cursor="x") == ""


# ── the signal-handler restore re-arm ──────────────────────────────────────────


def test_restore_on_signal_fires_restore_and_reraises():
    restored = []
    with fi._restore_on_signal(lambda: restored.append(1)):
        handler = _signal.getsignal(_signal.SIGINT)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(_signal.SIGINT, None)
    assert restored == [1]
    # handlers reinstated on exit (no longer our handler).
    assert _signal.getsignal(_signal.SIGINT) is not handler


def test_restore_on_signal_tolerates_non_main_thread():
    ran = []

    def _in_thread():
        # signal.signal raises ValueError off the main thread -> the CM must
        # still yield (relying on the ExitStack for restore).
        with fi._restore_on_signal(lambda: None):
            ran.append(1)

    t = threading.Thread(target=_in_thread)
    t.start()
    t.join()
    assert ran == [1]


# ── the safety gate ────────────────────────────────────────────────────────────


def _live_args(**over):
    ns = argparse.Namespace()
    setattr(ns, fi.LIVE_FLAG, over.get("flag", True))
    return ns


def test_guard_passes_when_all_conditions_hold(monkeypatch):
    monkeypatch.setattr(fi, "_node_count", lambda: 1)
    monkeypatch.setattr(fi, "_current_context", lambda: "default")
    fi.guard_live_or_refuse(_live_args(), environ={fi.ENV_ARMED: "1"})  # no raise


def test_guard_refuses_missing_flag(monkeypatch):
    called = []
    monkeypatch.setattr(fi, "_node_count", lambda: called.append(1) or 1)
    with pytest.raises(fi.LiveGuardError, match="flag"):
        fi.guard_live_or_refuse(_live_args(flag=False), environ={fi.ENV_ARMED: "1"})
    assert called == []  # short-circuits before touching the cluster


def test_guard_refuses_missing_env():
    with pytest.raises(fi.LiveGuardError, match=fi.ENV_ARMED):
        fi.guard_live_or_refuse(_live_args(), environ={})


def test_guard_refuses_wrong_node_count(monkeypatch):
    monkeypatch.setattr(fi, "_node_count", lambda: 3)
    monkeypatch.setattr(fi, "_current_context", lambda: "default")
    with pytest.raises(fi.LiveGuardError, match="single-node"):
        fi.guard_live_or_refuse(_live_args(), environ={fi.ENV_ARMED: "1"})


def test_guard_refuses_wrong_context(monkeypatch):
    monkeypatch.setattr(fi, "_node_count", lambda: 1)
    monkeypatch.setattr(fi, "_current_context", lambda: "prod-cluster")
    with pytest.raises(fi.LiveGuardError, match="context"):
        fi.guard_live_or_refuse(_live_args(), environ={fi.ENV_ARMED: "1"})


def test_guard_respects_expected_context_override(monkeypatch):
    monkeypatch.setattr(fi, "_node_count", lambda: 1)
    monkeypatch.setattr(fi, "_current_context", lambda: "my-laptop")
    fi.guard_live_or_refuse(
        _live_args(),
        environ={fi.ENV_ARMED: "1", fi.ENV_EXPECTED_CONTEXT: "my-laptop"},
    )  # no raise


def test_node_count_ok(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "node-a"))
    assert fi._node_count() == 1


def test_node_count_multiple(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "a b c"))
    assert fi._node_count() == 3


def test_node_count_minus_one_on_nonzero(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "refused"))
    assert fi._node_count() == -1


def test_node_count_minus_one_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("kubectl missing")

    monkeypatch.setattr(fi, "_run", _boom)
    assert fi._node_count() == -1


def test_current_context_ok(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "default\n"))
    assert fi._current_context() == "default"


def test_current_context_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(1, "", "err"))
    assert fi._current_context() == ""


def test_current_context_empty_on_oserror(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("kubectl missing")

    monkeypatch.setattr(fi, "_run", _boom)
    assert fi._current_context() == ""


# ── the CLI ────────────────────────────────────────────────────────────────────


def test_main_dry_run_lists_cases(capsys):
    rc = fi.main(["--case", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "[F1]" in out and "[F2]" in out and "[N1]" in out


def test_main_dry_run_shows_skip(capsys):
    rc = fi.main(["--case", "F3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[SKIP]" in out
    assert "skip:" in out


def test_dry_run_lines_includes_budget_and_arm_hint():
    lines = fi.dry_run_lines(fi.cases_for("all"), 90.0)
    joined = "\n".join(lines)
    assert "90s" in joined
    assert fi.ENV_ARMED in joined


def test_main_live_refused_never_injects(monkeypatch, capsys):
    # missing env AND missing flag -> refuse; assert NO injection op ran.
    ran = []
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: ran.append(cmd) or _proc(0))
    monkeypatch.delenv(fi.ENV_ARMED, raising=False)
    rc = fi.main(["--case", "F1", "--live"])  # no --i-understand flag
    assert rc == 2
    err = capsys.readouterr().err
    assert "LIVE REFUSED" in err
    # nothing destructive executed.
    assert all(
        "delete" not in c and "iptables" not in c and "scale" not in c for c in ran
    )


def test_main_live_success_writes_report(monkeypatch, tmp_path):
    monkeypatch.setattr(fi, "guard_live_or_refuse", lambda args, **kw: None)
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "ok"))

    def _fake_gate(**kwargs):
        return _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(HEALED))

    monkeypatch.setattr(fi.mesh, "MeshGate", _fake_gate)
    monkeypatch.setattr(fi.mesh, "PrivilegedHealer", lambda *a, **k: object())
    rc = fi.main(
        [
            "--case",
            "F1",
            "--live",
            "--i-understand-this-breaks-the-mesh",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    reports = list(tmp_path.glob("ws6-certification-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["ok"] is True
    assert payload["counts"]["passed"] == 1


def test_main_live_returns_one_when_certification_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(fi, "guard_live_or_refuse", lambda args, **kw: None)
    monkeypatch.setattr(fi, "_run", lambda cmd, **kw: _proc(0, "ok"))

    def _fake_gate(**kwargs):
        # F1 expects HEALED; returning UNSAFE makes the certification FAIL.
        return _FakeGate([_healthy(), _diag("istiod-not-ready")], _FakeResult(UNSAFE))

    monkeypatch.setattr(fi.mesh, "MeshGate", _fake_gate)
    monkeypatch.setattr(fi.mesh, "PrivilegedHealer", lambda *a, **k: object())
    rc = fi.main(
        [
            "--case",
            "F1",
            "--live",
            "--i-understand-this-breaks-the-mesh",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_build_parser_defaults():
    args = fi.build_parser().parse_args([])
    assert args.case == "all"
    assert args.live is False
    assert args.budget == fi.DEFAULT_BUDGET_S


def test_mesh_out_dir_default_is_under_deploy():
    d = fi.mesh_out_dir_default()
    assert d.name == "faultinject-runs"


def test_write_report_creates_files(tmp_path):
    r = fi.CaseResult(
        "F1",
        "l",
        fi.TIER_RBAC,
        HEALED,
        verdict=fi.VERDICT_PASS,
        observed_outcome=HEALED,
        reason="ok",
    )
    report = fi.CertificationReport(results=[r], budget_s=180)
    path = fi._write_report(report, tmp_path)
    assert path.exists()
    assert path.with_suffix(".txt").exists()
    assert json.loads(path.read_text())["ok"] is True


def test_healthy_constant_is_a_valid_expectation():
    # sanity: HEALTHY is importable and distinct from HEALED (guards a typo).
    assert HEALTHY != HEALED
