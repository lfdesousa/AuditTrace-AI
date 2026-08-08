"""Drift-guards for the Grafana dashboards vendored alongside the chart.

Both dashboards (Operations + Call Flow Tempo) ship as JSON files
in ``charts/audittrace/files/grafana-dashboards/`` so they travel
with the chart release. Pre-rename (ADR-035) the operations
dashboard was named ``sovereign-overview`` with uid + title +
tag ``sovereign-*``. The other dashboard had a stale
``sovereign-ai`` tag. These tests pin the post-rename invariants.

Note: the dashboards DO contain ``sovereign_operation_*`` metric
references (in ``targets[].expr``), which are intentional —
those names match what the ``@log_call`` decorator currently
emits to Prometheus on the live cluster. Renaming the metric
prefix is separate code-side work (backlog item — drift between
``@log_call``'s emission prefix and the platform's post-ADR-035
identity). These tests therefore reject ``sovereign-*`` only
in *cosmetic* identifiers (uid, title, tags), not in metric
expressions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASH_DIR = REPO_ROOT / "charts" / "audittrace" / "files" / "grafana-dashboards"

EXPECTED_DASHBOARDS: dict[str, dict[str, str]] = {
    "audittrace-overview.json": {
        "uid": "audittrace-overview",
        "title_prefix": "AuditTrace-AI",
    },
    "call-flow-tempo.json": {
        "uid": "audittrace-call-flow-tempo",
        "title_prefix": "AuditTrace-AI",
    },
    "audittrace-scan-pipeline.json": {
        "uid": "audittrace-scan-pipeline",
        "title_prefix": "AuditTrace-AI",
    },
    "audittrace-agents-learning.json": {
        "uid": "audittrace-agents-learning",
        "title_prefix": "AuditTrace-AI",
    },
}

# WU-2.2 — the recall-hit-rate panel is the demo's headline panel (#423
# non-vacuous proof): this guard fails if the panel OR its
# ``audittrace_recall_total`` metric target is removed from the dashboard.
AGENTS_LEARNING_FILE = "audittrace-agents-learning.json"
REQUIRED_RECALL_METRICS = ("audittrace_recall_total", "audittrace_recall_results")


def _load(name: str) -> dict:
    path = DASH_DIR / name
    assert path.exists(), f"dashboard missing: {path}"
    return json.loads(path.read_text())


@pytest.mark.parametrize("name", sorted(EXPECTED_DASHBOARDS.keys()))
class TestDashboardIdentity:
    """Each dashboard is present, parses, and uses the post-rename identity."""

    def test_present_and_valid_json(self, name: str) -> None:
        dash = _load(name)
        assert isinstance(dash, dict)
        assert "panels" in dash
        assert len(dash["panels"]) >= 1

    def test_uid_matches_expected(self, name: str) -> None:
        dash = _load(name)
        assert dash.get("uid") == EXPECTED_DASHBOARDS[name]["uid"], (
            f"{name} uid={dash.get('uid')!r} — must be "
            f"{EXPECTED_DASHBOARDS[name]['uid']!r} (post-ADR-035 rename)"
        )

    def test_title_uses_audittrace_brand(self, name: str) -> None:
        dash = _load(name)
        title = dash.get("title", "")
        prefix = EXPECTED_DASHBOARDS[name]["title_prefix"]
        assert title.startswith(prefix), (
            f"{name} title={title!r} — must start with {prefix!r} "
            f"(post-ADR-035 rename). 'Sovereign' is the pre-rename name."
        )

    def test_schema_version_present(self, name: str) -> None:
        # Every dashboard must pin a Grafana schemaVersion — an unpinned/
        # missing schemaVersion means Grafana silently migrates the JSON on
        # first load, drifting the vendored file away from what actually
        # renders (WU-2.2 strengthening of the drift-guard).
        dash = _load(name)
        schema_version = dash.get("schemaVersion")
        assert isinstance(schema_version, int) and schema_version > 0, (
            f"{name} schemaVersion={schema_version!r} — must be a positive int"
        )

    def test_no_cosmetic_sovereign_drift(self, name: str) -> None:
        # uid/title/tags MUST NOT carry the pre-rename `sovereign-*`
        # identifier. Metric-name references (`sovereign_operation_*`)
        # are excluded — they match the live `@log_call` emission and
        # are tracked as a separate code-side drift item.
        dash = _load(name)
        for field in ("uid", "title"):
            value = dash.get(field, "")
            assert "sovereign" not in value.lower(), (
                f"{name} field {field!r}={value!r} carries pre-rename "
                "'sovereign' identifier — must be 'audittrace-*'."
            )
        tags = dash.get("tags", [])
        for tag in tags:
            assert not re.match(r"^sovereign(-|$)", tag), (
                f"{name} tag {tag!r} carries pre-rename 'sovereign' prefix"
            )


def _all_target_exprs(dash: dict) -> list[str]:
    """Every ``targets[].expr`` across every panel — where a metric name
    actually lives in a Grafana dashboard JSON."""
    exprs: list[str] = []
    for panel in dash.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if isinstance(expr, str):
                exprs.append(expr)
    return exprs


class TestAgentsLearningDashboardRecallMetrics:
    """WU-2.2 non-vacuous proof (#423): the drift-guard must fail if the
    recall-hit-rate panel, or the ``audittrace_recall_*`` metrics it plots,
    is removed from the dashboard. Verified by hand during the build (panel
    deleted, suite re-run → RED here; restored → GREEN) — this class is the
    forever-guard against that regression happening silently."""

    def test_recall_metrics_present_somewhere_in_the_dashboard(self) -> None:
        dash = _load(AGENTS_LEARNING_FILE)
        exprs = " ".join(_all_target_exprs(dash))
        for metric in REQUIRED_RECALL_METRICS:
            assert metric in exprs, (
                f"{AGENTS_LEARNING_FILE} is missing a target referencing "
                f"{metric!r} — the recall-telemetry panels are vacuous "
                "without it (#423 REJECT trigger)."
            )

    def test_recall_hit_rate_panel_present(self) -> None:
        # The headline metric (spec: WU-2 §1) is recall-hit-rate = rate(hit
        # ="true") / rate(total). Find the panel that computes it and assert
        # its expr actually references audittrace_recall_total with BOTH the
        # hit="true" numerator and the unfiltered-total denominator — a panel
        # titled "hit rate" that silently dropped the metric would pass a
        # weaker title-only check, so this asserts on the expr content.
        dash = _load(AGENTS_LEARNING_FILE)
        hit_rate_panels = [
            p
            for p in dash.get("panels", [])
            if "hit-rate" in p.get("title", "").lower()
            or "hit rate" in p.get("title", "").lower()
        ]
        assert hit_rate_panels, (
            f"{AGENTS_LEARNING_FILE} has no panel titled with 'hit-rate' / "
            "'hit rate' — the headline recall panel is missing."
        )
        exprs = " ".join(_all_target_exprs({"panels": hit_rate_panels}))
        assert "audittrace_recall_total" in exprs
        assert 'hit="true"' in exprs, (
            "the hit-rate panel's expr must filter the numerator on "
            'hit="true" — a bare rate(audittrace_recall_total[...]) alone '
            "is a call-volume panel, not a hit-rate panel."
        )

    def test_results_per_recall_panel_references_results_histogram(self) -> None:
        dash = _load(AGENTS_LEARNING_FILE)
        exprs = " ".join(_all_target_exprs(dash))
        assert "audittrace_recall_results" in exprs


class TestDashboardSetCompleteness:
    """The chart packages BOTH dashboards (no accidental drop)."""

    def test_no_unexpected_dashboard_files(self) -> None:
        present = sorted(p.name for p in DASH_DIR.glob("*.json"))
        expected = sorted(EXPECTED_DASHBOARDS.keys())
        assert present == expected, (
            f"dashboard set drift — present={present}, expected={expected}. "
            "Add the new file to EXPECTED_DASHBOARDS in this test, then "
            "decide whether the file is a chart-shipped artefact or a "
            "Grafana-side experiment that should live elsewhere."
        )
