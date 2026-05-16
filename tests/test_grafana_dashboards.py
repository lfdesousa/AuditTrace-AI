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
}


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
