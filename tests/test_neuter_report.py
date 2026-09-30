"""Unit tests for ``scripts/neuter/report.py`` -- per_guard_table.md
generation and ``--verify`` (SPEC v3 §13; SF-C, SF-D)."""

from __future__ import annotations

import json

from scripts.neuter.report import generate_report, verify_report, write_report
from scripts.neuter.spec import Edit, GuardTestEntry, NeuterEntry, NeuterSpecFile


def _spec(tmp_path):
    entry = NeuterEntry(
        id="n1",
        file="mod.py",
        edits=[Edit(old="a", new="b")],
        tests=["t.py::test_a"],
        engines=["mock"],
        guard="G1",
        clause="C1",
    )
    return NeuterSpecFile(
        schema=3,
        sha="deadbeef",
        scope_files=["t.py"],
        guard_tests=[GuardTestEntry(id="t.py::test_a", row="G1")],
        neuters=[entry],
        path=tmp_path / "s.json",
    )


def _row(**overrides):
    row = {
        "id": "n1",
        "file": "mod.py",
        "tests_expected": 1,
        "tests_collected": 1,
        "failed": [],
        "failure_types": [],
        "verdict": "RED",
        "error_reason": None,
        "secs": 1.0,
        "worker": 1,
        "restored_clean": True,
        "pg_settings": None,
        "unmapped_red": [],
    }
    row.update(overrides)
    return row


def test_generate_report_basic(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    spec = _spec(tmp_path)
    text = generate_report(evidence, spec)
    assert "n1" in text
    assert "## GREEN (0)" in text
    assert "## ERROR (0)" in text
    assert "generated-from:" in text
    assert "drift_n=0" in text
    assert "error_n=0" in text


def test_generate_report_green_and_error_sections(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    rows = [
        _row(id="n1", verdict="GREEN"),
        _row(id="n1", verdict="ERROR", error_reason="pathcheck"),
    ]
    (evidence / "neuter_results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    spec = _spec(tmp_path)
    text = generate_report(evidence, spec)
    assert "## GREEN (1)" in text
    assert "## ERROR (1)" in text
    assert "pathcheck" in text


def test_generate_report_drift_section(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    row = _row(unmapped_red=["t.py::test_other"])
    (evidence / "neuter_results.jsonl").write_text(json.dumps(row) + "\n")
    spec = _spec(tmp_path)
    text = generate_report(evidence, spec)
    assert "drift_n=1" in text


def test_generate_report_guard_tests_diff(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    spec = _spec(tmp_path)
    reviewer = [
        GuardTestEntry(id="t.py::test_a", row="G1"),
        GuardTestEntry(id="t.py::test_extra", row="G2"),
    ]
    text = generate_report(evidence, spec, reviewer_guard_tests=reviewer)
    assert "test_extra" in text
    assert "diff:" in text


def test_generate_report_arbitration_defect(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    arb = {
        "id": "n1",
        "harness_verdict": "RED",
        "authoritative_verdict": "GREEN",
        "defect_ref": None,
    }
    (evidence / "arbitration.jsonl").write_text(json.dumps(arb) + "\n")
    spec = _spec(tmp_path)
    text = generate_report(evidence, spec)
    assert "defect_n=1" in text.splitlines()[-1]


def test_write_and_verify_report_roundtrip(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    spec = _spec(tmp_path)
    write_report(evidence, spec)
    assert verify_report(evidence, spec) == 0


def test_verify_report_missing_file(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    spec = _spec(tmp_path)
    assert verify_report(evidence, spec) == 7


def test_verify_report_stale_after_new_row(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    spec = _spec(tmp_path)
    write_report(evidence, spec)
    (evidence / "neuter_results.jsonl").write_text(
        json.dumps(_row(id="n2")) + "\n" + json.dumps(_row()) + "\n"
    )
    assert verify_report(evidence, spec) == 7


def test_verify_report_open_arbitration_defect_fails(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    arb = {
        "id": "n1",
        "harness_verdict": "RED",
        "authoritative_verdict": "GREEN",
        "defect_ref": None,
    }
    (evidence / "arbitration.jsonl").write_text(json.dumps(arb) + "\n")
    spec = _spec(tmp_path)
    write_report(evidence, spec)
    assert verify_report(evidence, spec) == 7


def test_verify_report_closed_arbitration_defect_passes(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "neuter_results.jsonl").write_text(json.dumps(_row()) + "\n")
    arb = {
        "id": "n1",
        "harness_verdict": "RED",
        "authoritative_verdict": "GREEN",
        "defect_ref": "issue-123",
    }
    (evidence / "arbitration.jsonl").write_text(json.dumps(arb) + "\n")
    spec = _spec(tmp_path)
    write_report(evidence, spec)
    assert verify_report(evidence, spec) == 0
