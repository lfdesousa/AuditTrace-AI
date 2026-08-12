"""Unit tests for :mod:`audittrace.services.recall_telemetry`.

RECALL-METRIC-COVERAGE (2026-08-12): the fleet's own recalls hit the same
REST routes a human front-door caller uses, but every ``emit_recall_telemetry``
call site hardcoded ``source="backoffice"`` — the fleet's recalls were
counted, but mislabeled, so the "Agents Learning" dashboard read ~0
fleet activity even though the fleet path was fully instrumented.

This file covers :func:`classify_recall_source_from_request` in isolation
(no FastAPI app, no TestClient — a bare Starlette ``Request`` built from a
raw ASGI scope, mirroring the precedent in ``test_async_persist.py``'s
``_make_request`` helper). The route-level flip (the classifier actually
wired into the 6 REST emit sites) is covered separately in
``tests/test_memory_routes.py::TestRecallTelemetryFleetSourceFlip``.
"""

from __future__ import annotations

import pytest
from fastapi import Request

from audittrace.services.recall_telemetry import classify_recall_source_from_request


def _make_request(headers: dict[str, str]) -> Request:
    """Minimal FastAPI Request stand-in for header-parsing tests (same
    idiom as ``test_async_persist.py::_make_request``)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/memory/semantic",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


class TestClassifyRecallSourceFromRequest:
    """Non-vacuous proof (#423): neuter either branch in
    ``classify_recall_source_from_request`` (drop the ``x-source`` prefix
    check, or drop the ``x-agent-role`` presence check) -> the matching
    test below goes RED."""

    def test_no_headers_is_backoffice(self) -> None:
        assert classify_recall_source_from_request(_make_request({})) == "backoffice"

    def test_x_source_opencode_prefix_is_fleet(self) -> None:
        request = _make_request({"X-Source": "opencode-builder"})
        assert classify_recall_source_from_request(request) == "fleet"

    def test_x_source_opencode_prefix_any_role_is_fleet(self) -> None:
        for role in ("builder", "reviewer", "deployer", "curator", "verifier"):
            request = _make_request({"X-Source": f"opencode-{role}"})
            assert classify_recall_source_from_request(request) == "fleet", role

    def test_x_source_opencode_prefix_case_insensitive(self) -> None:
        request = _make_request({"X-Source": "OPENCODE-BUILDER"})
        assert classify_recall_source_from_request(request) == "fleet"

    def test_x_agent_role_present_is_fleet(self) -> None:
        request = _make_request({"X-Agent-Role": "builder"})
        assert classify_recall_source_from_request(request) == "fleet"

    def test_x_agent_role_present_with_no_x_source_is_still_fleet(self) -> None:
        """The two markers are independent OR conditions per the spec
        Decision — ``X-Agent-Role`` alone (no ``X-Source`` at all) must
        still classify as fleet."""
        request = _make_request({"X-Agent-Role": "reviewer"})
        assert "x-source" not in {h.lower() for h in request.headers.keys()}
        assert classify_recall_source_from_request(request) == "fleet"

    def test_x_agent_role_empty_value_is_backoffice(self) -> None:
        """An empty/whitespace-only header value is NOT "present" for
        classification purposes."""
        request = _make_request({"X-Agent-Role": "   "})
        assert classify_recall_source_from_request(request) == "backoffice"

    def test_x_source_present_but_not_opencode_prefix_is_backoffice(self) -> None:
        """A human front-door client that self-identifies via X-Source
        (e.g. the webui SPA) is NOT the fleet marker — only the
        ``opencode-*`` prefix is."""
        for source in ("audittrace-webui", "curl", "httpx", "librechat"):
            request = _make_request({"X-Source": source})
            assert classify_recall_source_from_request(request) == "backoffice", source

    def test_x_source_bare_opencode_no_dash_is_backoffice(self) -> None:
        """Exact-prefix discipline: "opencode" without the trailing
        "-<role>" does not match the "opencode-" prefix."""
        request = _make_request({"X-Source": "opencode"})
        assert classify_recall_source_from_request(request) == "backoffice"

    def test_x_source_whitespace_is_backoffice(self) -> None:
        request = _make_request({"X-Source": "   "})
        assert classify_recall_source_from_request(request) == "backoffice"

    @pytest.mark.parametrize(
        ("x_source", "x_agent_role", "expected"),
        [
            ("opencode-builder", "", "fleet"),
            ("opencode-builder", "builder", "fleet"),
            ("", "builder", "fleet"),
            ("", "", "backoffice"),
            ("audittrace-webui", "", "backoffice"),
        ],
    )
    def test_combinations(
        self, x_source: str, x_agent_role: str, expected: str
    ) -> None:
        headers: dict[str, str] = {}
        if x_source:
            headers["X-Source"] = x_source
        if x_agent_role:
            headers["X-Agent-Role"] = x_agent_role
        assert classify_recall_source_from_request(_make_request(headers)) == expected
