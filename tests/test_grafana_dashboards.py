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


# PromQL keywords / label VALUES (not metric names) that legitimately show up
# when the identifier regex below tokenises an expr. Case-insensitive.
# Shared by every "no unknown metric names" drift-guard so Wave A and Wave B
# can never silently diverge on what counts as "known" (a prior version had
# two independently-maintained copies of this set; the Wave B copy was
# missing the M1 ``cache=~"hit|miss"`` label value, which failed the guard
# it was supposed to enforce).
_PROMQL_ALLOWED_LOWER = frozenset(
    {
        "histogram_quantile",
        "sum",
        "increase",
        "rate",
        "by",
        "le",
        "collection",
        "source",
        "layer",
        "hit",
        "miss",
        "true",
        "false",
        "cache",
        "memory",
        "http_route",
        "http_response_status_code",
        "http_request_method",
        "http_server_request_duration_seconds_count",
        # Route path segments extracted from regex literals in PromQL
        # (e.g. "/memory/(semantic|episodic|...)"):
        "semantic",
        "episodic",
        "procedural",
        "conversational",
        "get",
        "post",
        "put",
        "delete",
        "normal",
        "area",
        "none",
        "multi",
        "single",
    }
)


def _assert_exprs_reference_known_metrics(
    exprs: list[str], known_metrics: frozenset[str]
) -> None:
    """Assert every PromQL identifier in ``exprs`` is either a known metric
    name (base, with histogram-suffix stripping) or a recognised PromQL
    keyword / label value from :data:`_PROMQL_ALLOWED_LOWER`.

    Shared implementation for the Wave A and Wave B "no unknown metric
    names" drift-guards — see :data:`_PROMQL_ALLOWED_LOWER` docstring for
    why this must NOT be duplicated per-test-class.
    """
    for expr in exprs:
        # Extract metric names from the expression (simplified: look for
        # identifiers that look like metric names: alphanumeric + underscore).
        metrics_in_expr = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expr)
        for metric in metrics_in_expr:
            # Skip Grafana template variables (e.g. $__rate_interval,
            # $__interval, $range, $timeFrom, etc.) — extracted without
            # the '$' by the identifier regex. Also skip double-underscore
            # prefixed names (Grafana internal templates).
            if re.match(r"^\$__?\w+", metric) or metric.startswith("__"):
                continue
            # Skip PromQL window suffixes (1h, 24h, 7d, etc.) and single-char
            # window units extracted from expressions like [1h] → 'h'.
            if metric in ("h", "d", "w", "y", "s", "m") or re.fullmatch(
                r"^\d+[smhdwy]$", metric
            ):
                continue
            # Strip histogram suffixes to get the base metric name.
            base = metric
            for suffix in ("_bucket", "_sum", "_count"):
                if metric.endswith(suffix):
                    base = metric[: -len(suffix)]
                    break
            # The base (e.g. 'audittrace_recall_results') is a valid
            # reference if any suffixed variant is in known_metrics.
            is_known_base = base in known_metrics
            if not is_known_base:
                for variant in (f"{base}_bucket", f"{base}_sum", f"{base}_count"):
                    if variant in known_metrics:
                        is_known_base = True
                        break
            assert (
                base in known_metrics
                or is_known_base
                or base.lower() in _PROMQL_ALLOWED_LOWER
                or (base.startswith("http_") and base.isidentifier())
            ), (
                f"expr {expr!r} references metric/base "
                f"{metric!r} which is NOT in the known-metric set "
                f"({known_metrics}). The dashboard must not "
                "reference unknown metrics."
            )


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
        # WU-C: the panel title must be the public-credibility name
        # ("Recall Hit-Rate"), NOT the internal-speak "(the money panel)".
        title = hit_rate_panels[0].get("title", "")
        assert "money" not in title.lower(), (
            f"{AGENTS_LEARNING_FILE} panel {title!r} still carries "
            "internal-speak '(the money panel)' — must be 'Recall Hit-Rate'."
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


class TestRecallRestCallPanels:
    """Wave A (v2): the Governance & Health row and its table panels
    (Memory Reads by Route — 24h total, Memory Read Status — by code)
    must be present. These panels use the existing HTTP-server metric
    and are already confirmed live on the obs-stack.

    Wave A removed the v1 rate-by-route timeseries (ops/s is invisible
    at low traffic per D6) and replaced it with the 24h totals table.
    The Governance & Health row title was updated from the v1
    'Recall — Memory Read REST Calls' to match the spec's three-row
    naming convention.
    """

    def test_row_panel_present(self) -> None:
        dash = _load(AGENTS_LEARNING_FILE)
        rows = [
            p
            for p in dash.get("panels", [])
            if p.get("type") == "row"
            and "Governance" in p.get("title", "")
            and "Memory Read REST" in p.get("title", "")
        ]
        assert rows, (
            f"{AGENTS_LEARNING_FILE} is missing the 'Governance & Health — "
            "Memory Read REST' row panel (Wave A v2 restructure)."
        )

    def test_table_24h_totals_panel_present(self) -> None:
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "24h total" in p.get("title", "")
            and "memory read" in p.get("title", "").lower()
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} is missing the 'Memory Reads by Route — "
            "24h total' panel (Wave A v2 restructure)."
        )
        assert panels[0].get("type") == "table"
        # Wave A: this panel uses http_server_request_duration_seconds_count
        # (the same HTTP metric) — it must not drift to a non-existent metric.
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "http_server_request_duration_seconds_count" in exprs
        assert "http_request_method" in exprs

    def test_status_by_code_panel_present(self) -> None:
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "response code" in p.get("title", "").lower()
            and "memory read" in p.get("title", "").lower()
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} is missing the 'Memory Read Status — "
            "by response code' panel (Wave A v2 restructure)."
        )
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "http_response_status_code" in exprs


class TestAgentsLearningWaveAPanels:
    """Wave A (v2) — panels 4.2, 4.3, 4.5, 4.6, 4.7 (dashboard-only,
    existing metrics). Wave B (M1/M2) adds:

      - 4.1 Memory Writes — daily (Accumulation row, uses M2 metric)
      - 4.4 Knowledge-Base Growth (Accumulation row, uses M2 metric)
      - 4.NEW Cache-hit share (Effectiveness row, uses M1 cache label)

    Spec §6.7: extended to assert each new panel's PromQL parses and
    references ONLY existing or newly-added metric names:

      - audittrace_recall_total       (counter: source, collection, hit, cache)
      - audittrace_recall_results     (histogram: _bucket / _sum / _count)
      - audittrace_memory_writes_total       (M2 counter: layer)
      - audittrace_memory_chunks_indexed_total (M2 counter: collection)
      - http_server_request_duration_seconds_count  (HTTP counter)
    """

    # Known-existing metric names — any NEW expr that references a name
    # outside this set is a drift indicator (the dashboard should NOT
    # reference metrics that don't exist yet).
    # Wave A + Wave B metrics.
    EXISTING_METRICS = frozenset(
        [
            "audittrace_recall_total",
            "audittrace_recall_results_bucket",
            "audittrace_recall_results_sum",
            "audittrace_recall_results_count",
            "http_server_request_duration_seconds_count",
            # Wave B (M2) — new counters added by this PR.
            "audittrace_memory_writes_total",
            "audittrace_memory_chunks_indexed_total",
        ]
    )

    # Wave A panel titles (from spec §4) — used for lookup.
    WAVE_A_PANEL_TITLES = frozenset(
        [
            "Recall Hit-Rate",
            "Recall Hit-Rate — 24h summary",
            "Results per Recall — p50 / p95 by collection",
            "Recall Calls by Collection — 24h counts (governance view)",
            "Memory Reads by Route — 24h total (GET)",
            "Memory Read Status — by response code (GET)",
        ]
    )

    def test_wave_a_panel_titles_present(self) -> None:
        """Every Wave A panel title from spec §4 must be present in the
        dashboard."""
        dash = _load(AGENTS_LEARNING_FILE)
        panel_titles = {p.get("title", "") for p in dash.get("panels", [])}
        for expected in self.WAVE_A_PANEL_TITLES:
            assert expected in panel_titles, (
                f"{AGENTS_LEARNING_FILE} missing Wave A panel title "
                f"{expected!r} (spec §4.{expected.split()[0].lower()})"
            )

    def test_accumulation_row_populated_wave_b(self) -> None:
        """Wave B: the Accumulation row (4.1 Memory Writes + 4.4
        Knowledge-Base Growth) must have 2 child panels — the M2
        instrumentation now exists. Previously empty in Wave A.

        Grafana only renders a non-collapsed row's (``collapsed: false``)
        children from the FLAT top-level ``dash["panels"]`` array — a
        nested ``row["panels"]`` array is only honoured while the row is
        collapsed. Children are therefore identified by title, matching
        the pre-existing "Governance & Health" row (id 6), whose children
        (the 4.6/4.7 tables) also live as flat top-level entries — not by
        a nested array, which would have silently failed to render (the
        live-render bug class 478a7ea fixed for those same 4.6/4.7
        tables)."""
        dash = _load(AGENTS_LEARNING_FILE)
        panels = dash.get("panels", [])
        acc_rows = [
            p
            for p in panels
            if p.get("type") == "row" and "Accumulation" in p.get("title", "")
        ]
        assert acc_rows, (
            f"{AGENTS_LEARNING_FILE} missing the 'Accumulation' row (Wave A)."
        )
        acc_row = acc_rows[0]
        assert acc_row.get("panels", []) == [], (
            f"{AGENTS_LEARNING_FILE} Accumulation row must not carry a nested "
            "'panels' array while collapsed=false — Grafana ignores it, so any "
            "children living only there would silently fail to render."
        )
        children = [
            p
            for p in panels
            if "Memory Writes" in p.get("title", "")
            or "Knowledge-Base" in p.get("title", "")
        ]
        assert len(children) == 2, (
            f"{AGENTS_LEARNING_FILE} Accumulation row must have 2 child panels "
            "(4.1 Memory Writes + 4.4 Knowledge-Base Growth, Wave B), flat in "
            "the top-level panels array."
        )

    def test_recall_hit_rate_panel_wave_a(self) -> None:
        """4.2: Recall Hit-Rate — rolling ratio with visible gaps.
        PromQL must reference audittrace_recall_total (existing counter),
        use increase() with a window, and NOT interpolate nulls.
        """
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p for p in dash.get("panels", []) if p.get("title") == "Recall Hit-Rate"
        ]
        assert panels, f"{AGENTS_LEARNING_FILE} missing 'Recall Hit-Rate' panel."
        exprs = [
            t.get("expr", "")
            for t in panels[0].get("targets", [])
            if isinstance(t.get("expr"), str)
        ]
        assert exprs, "Recall Hit-Rate panel has no expr targets."
        for expr in exprs:
            assert "audittrace_recall_total" in expr, (
                f"Hit-rate expr {expr!r} must reference audittrace_recall_total"
            )
            assert 'hit="true"' in expr, (
                'Hit-rate expr must filter numerator on hit="true"'
            )
            assert "spanNulls" in str(
                panels[0].get("fieldConfig", {}).get("defaults", {}).get("custom", {})
            ), (
                "Hit-rate timeseries must disable null interpolation "
                "(connected-nulls OFF) so gaps stay visible per D1/D2."
            )

    def test_results_per_recall_panel_wave_a(self) -> None:
        """4.3: Results per Recall — p50/p95 from histogram_quantile.
        PromQL must reference audittrace_recall_results_bucket (existing
        histogram) via histogram_quantile, NOT rate(…_sum)/rate(…_count)
        (which computes an average, not a percentile).
        """
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "p50" in p.get("title", "") or "p95" in p.get("title", "")
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} missing Results per Recall p50/p95 panel."
        )
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "audittrace_recall_results_bucket" in exprs, (
            "Results per Recall must reference audittrace_recall_results_bucket "
            "(histogram_quantile), not the avg ratio (rate_sum / rate_count)."
        )
        assert "histogram_quantile" in exprs, (
            "Results per Recall must use histogram_quantile() for p50/p95."
        )

    def test_recall_calls_by_collection_wave_a(self) -> None:
        """4.5: Recall Calls by Collection — 24h counts (increase[24h]),
        not ops/s. The spec explicitly rejects rate/ops for governance
        panels because near-zero rates render as invisible spikes."""
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "Recall Calls by Collection" in p.get("title", "")
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Recall Calls by Collection' panel."
        )
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "audittrace_recall_total" in exprs
        assert "increase" in exprs, (
            "Recall Calls by Collection must use increase() for 24h counts "
            "(not rate/ops — D6 fix)."
        )
        assert "24h" in exprs, (
            "Recall Calls by Collection must use [24h] window (not $__interval)."
        )
        # Must NOT use "ops" unit — governance wants counts, not rates.
        unit = panels[0].get("fieldConfig", {}).get("defaults", {}).get("unit")
        assert unit != "ops", (
            "Recall Calls by Collection must NOT use 'ops' unit (D6 fix)."
        )

    def test_24h_totals_table_wave_a(self) -> None:
        """4.6: Memory Reads by Route — 24h totals table. Zeros stay
        visible. PromQL must reference the existing HTTP counter."""
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "24h total" in p.get("title", "")
            and "memory read" in p.get("title", "").lower()
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Memory Reads by Route — 24h "
            "total' panel (Wave A v2 restructure)."
        )
        assert panels[0].get("type") == "table"
        # Must be an INSTANT query: a range query renders one row per scrape
        # step (the "Time | 0 | /memory/episodic" list), not the intended
        # single 24h-total row per route. Live-render bug caught 2026-08-09.
        for tgt in panels[0].get("targets", []):
            assert tgt.get("instant") is True, (
                "24h-totals table target must set instant:true (range query "
                "renders a per-scrape time series, not the 24h total per route)."
            )
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "http_server_request_duration_seconds_count" in exprs
        assert "http_request_method" in exprs

    def test_status_by_code_table_wave_a(self) -> None:
        """4.7: Read Status by code — 24h counts with description
        annotation of expected 401 sources. Cross-link note to future
        security view."""
        dash = _load(AGENTS_LEARNING_FILE)
        panels = [
            p
            for p in dash.get("panels", [])
            if "response code" in p.get("title", "").lower()
            and "memory read" in p.get("title", "").lower()
        ]
        assert panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Memory Read Status — by "
            "response code' panel (Wave A v2 restructure)."
        )
        # Must be an INSTANT query (same 24h-total-per-code rendering rule as
        # the by-route table; range query would list a row per scrape).
        for tgt in panels[0].get("targets", []):
            assert tgt.get("instant") is True, (
                "status-by-code table target must set instant:true (range "
                "query renders a per-scrape time series, not 24h totals)."
            )
        # Must reference http_response_status_code in the expr.
        exprs = " ".join(_all_target_exprs({"panels": panels}))
        assert "http_response_status_code" in exprs
        # Description must annotate expected 401 sources (D4 fix).
        desc = panels[0].get("description", "")
        assert "401" in desc, (
            "Status panel description must annotate expected 401 sources (D4 fix)."
        )
        # Description must cross-link to future security view.
        assert "security" in desc.lower() or "future" in desc.lower(), (
            "Status panel description must note this is the boundary where "
            "the learning dashboard ends and the security/audit view begins."
        )

    def test_no_new_metric_names_wave_b(self) -> None:
        """ALL PromQL exprs in the dashboard must reference ONLY the
        known metric set (Wave A + Wave B M1/M2 counters).
        No unknown metric references — that would indicate a
        hallucinated or drifting panel query."""
        dash = _load(AGENTS_LEARNING_FILE)
        _assert_exprs_reference_known_metrics(
            _all_target_exprs(dash), self.EXISTING_METRICS
        )

    def test_panel_descriptions_use_proxy_language(self) -> None:
        """Spec §1 claim discipline: panel descriptions must use 'recalled
        into context', NEVER 'improved the answer' or causal language.
        Every ratio must expose its denominator."""
        dash = _load(AGENTS_LEARNING_FILE)
        for panel in dash.get("panels", []):
            desc = panel.get("description", "")
            # Forbidden causal language.
            assert "improved" not in desc.lower(), (
                f"Panel {panel.get('title', '')!r} description uses forbidden "
                "causal language 'improved' — spec §1 requires 'recalled into "
                "context', never 'improved the answer'."
            )
            assert "caused a better" not in desc.lower(), (
                f"Panel {panel.get('title', '')!r} description uses "
                "forbidden causal language — spec §1."
            )


class TestAgentsLearningWaveBPanels:
    """Wave B (M1 + M2) — panels 4.1, 4.4, 4.NEW cache-hit share.
    These panels use the NEW M1/M2 instruments introduced by this PR."""

    def test_panel_4_1_memory_writes_present(self) -> None:
        """4.1: Memory Writes — daily must be present inside the
        Accumulation row, using the M2 audittrace_memory_writes_total
        counter."""
        dash = _load(AGENTS_LEARNING_FILE)
        # Panel presence + position is looked up flat in dash["panels"] —
        # NOT via a nested row["panels"] array. Grafana only honours a
        # non-collapsed row's nested "panels" while collapsed; a
        # non-collapsed row's children live as flat top-level entries
        # (see test_accumulation_row_populated_wave_b for the full
        # rationale — this is the live-render-bug class 478a7ea fixed for
        # 4.6/4.7).
        memory_writes_panels = [
            p for p in dash.get("panels", []) if "Memory Writes" in p.get("title", "")
        ]
        assert memory_writes_panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Memory Writes — daily' panel "
            "(4.1, Wave B / M2)."
        )
        panel = memory_writes_panels[0]
        exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
        assert "audittrace_memory_writes_total" in exprs, (
            "4.1 Memory Writes must reference audittrace_memory_writes_total."
        )

    def test_panel_4_4_knowledge_base_growth_present(self) -> None:
        """4.4: Knowledge-Base Growth — cumulative by collection must be
        present inside the Accumulation row, using the M2
        audittrace_memory_chunks_indexed_total counter, stacked by
        collection (fixes D2 flat-1s)."""
        dash = _load(AGENTS_LEARNING_FILE)
        # Flat top-level lookup — see test_panel_4_1_memory_writes_present
        # for why (Grafana non-collapsed-row rendering rule).
        kb_panels = [
            p for p in dash.get("panels", []) if "Knowledge-Base" in p.get("title", "")
        ]
        assert kb_panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Knowledge-Base Growth' panel "
            "(4.4, Wave B / M2, fixes D2)."
        )
        panel = kb_panels[0]
        exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
        assert "audittrace_memory_chunks_indexed_total" in exprs, (
            "4.4 Knowledge-Base Growth must reference "
            "audittrace_memory_chunks_indexed_total."
        )
        # D2 fix: must be cumulative (increase over $__range) not per-event.
        assert "increase" in exprs or "sum(" in exprs, (
            "4.4 Knowledge-Base Growth must use increase()/sum() for cumulative "
            "plotting — the old flat-1s plotted per-event increments (D2)."
        )
        # Stacking: the fieldConfig must have stacking mode set.
        stacking = (
            panel.get("fieldConfig", {})
            .get("defaults", {})
            .get("custom", {})
            .get("stacking", {})
        )
        assert stacking.get("mode") == "normal", (
            "4.4 Knowledge-Base Growth must stack by collection (stacking.mode=normal) "
            "to show WHERE knowledge accumulates."
        )

    def test_panel_4_new_cache_hit_share_present(self) -> None:
        """4.NEW: Cache-hit share must be present in the Effectiveness row,
        using the M1 cache label on audittrace_recall_total."""
        dash = _load(AGENTS_LEARNING_FILE)
        cache_panels = [
            p for p in dash.get("panels", []) if "Cache-hit" in p.get("title", "")
        ]
        assert cache_panels, (
            f"{AGENTS_LEARNING_FILE} missing 'Cache-hit share' panel "
            "(4.NEW, Wave B / M1)."
        )
        panel = cache_panels[0]
        exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
        assert 'cache="hit"' in exprs, (
            '4.NEW Cache-hit share numerator must filter on cache="hit".'
        )
        assert 'cache=~"hit|miss"' in exprs, (
            '4.NEW Cache-hit share denominator must use cache=~"hit|miss" '
            "to include only tool-surface recalls."
        )
        # Must use the recall_total counter (not a different metric).
        assert "audittrace_recall_total" in exprs, (
            "4.NEW must reference audittrace_recall_total (M1 label, not a new metric)."
        )

    def test_no_panel_references_unknown_metrics_wave_b(self) -> None:
        """Every panel's PromQL expr must reference a KNOWN metric (from
        EXISTING_METRICS). No hallucinated metric names.

        Uses the SAME shared guard as
        ``TestAgentsLearningWaveAPanels.test_no_new_metric_names_wave_b`` —
        a previous version of this test re-implemented the check without
        the PromQL-keyword/label-value allow-list, so it flagged legitimate
        tokens like ``sum`` and the M1 ``cache=~"hit|miss"`` label value
        ``miss`` as unknown metrics."""
        dash = _load(AGENTS_LEARNING_FILE)
        _assert_exprs_reference_known_metrics(
            _all_target_exprs(dash), TestAgentsLearningWaveAPanels.EXISTING_METRICS
        )
