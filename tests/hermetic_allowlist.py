"""Allowlist for the hermetic cluster-tool guard's IN-PROCESS word check in
``tests/conftest.py`` (SPEC 2026-10-11-SPEC-test-parity-scripted-
enforcement.md, R3, redesigned by ADDENDUM A2 through A7 after two
independent-review REJECTs on the earlier argv-parsing approach).

A test may reach a REAL ``kubectl``, or a REAL ``helm`` invocation that
isn't one of the positionally-safe chart-only subcommands, only if it
opts in with BOTH:

1. the ``@pytest.mark.allow_cluster_tool(tool="kubectl", reason="...")``
   marker on the test, AND
2. a matching entry in :data:`ALLOWED_CLUSTER_TOOL_TESTS` below, keyed by
   the test's pytest nodeid, naming the SAME tool.

Both must agree or the guard fails closed (a marker with no allowlist
entry, or an allowlist entry whose tool doesn't match the marker's, is
itself a guard failure — see ``tests/conftest.py::_enforce_hermetic``).
This opt-in waives ONLY the in-process word check — it has no effect on
the env-equality check (which has no opt-in at all), and none on the
process-layer kubectl/helm shims in ``scripts/ci/run-tests.sh``: a test
that is allowlisted here but genuinely reaches a real cluster tool still
fails the full run via the shim's reject log. That is intended — a test
that needs a real cluster belongs in the integration gate, not this
suite.

Starts EMPTY for ``kubectl``, and stays empty. No test in this suite
legitimately needs a real, live cluster: the two tests that looked like
they did
(``tests/test_deploy_runner.py::test_cli_returns_eight_on_first_party_mismatch``
and ``::test_noop_path_d3_recheck_clears_converged_and_exits_eight``) were
hermeticity LEAKS — the mesh gate's default ``mesh.MeshGate`` went
unstubbed and shelled to a REAL kubectl, which only "passed" because the
laptop happens to run a live k3s cluster. They were fixed by STUBBING the
mesh gate, never by listing them here.

If a genuine future need arises, add the marker + the entry together, and
explain in the ``reason`` why stubbing the seam is not possible for that
test.
"""

from __future__ import annotations

#: nodeid -> the exact tool name (``"kubectl"`` or ``"helm"``) that test's
#: ``@pytest.mark.allow_cluster_tool(tool=...)`` marker is permitted to name.
ALLOWED_CLUSTER_TOOL_TESTS: dict[str, str] = {}
