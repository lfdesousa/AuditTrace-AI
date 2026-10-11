"""Allowlist for the hermetic cluster-tool guard in ``tests/conftest.py``
(SPEC 2026-10-11-SPEC-test-parity-scripted-enforcement.md, R3).

A test may reach a REAL ``kubectl``, or a REAL ``helm`` invocation whose
subcommand is cluster-touching (anything other than ``template``, ``lint``,
``dependency``, ``version`` or ``show``), only if it opts in with BOTH:

1. the ``@pytest.mark.allow_cluster_tool(tool="kubectl", reason="...")``
   marker on the test, AND
2. a matching entry in :data:`ALLOWED_CLUSTER_TOOL_TESTS` below, keyed by
   the test's pytest nodeid, naming the SAME tool.

Both must agree or the guard fails closed (a marker with no allowlist
entry, or an allowlist entry whose tool doesn't match the marker's, is
itself a guard failure — see ``tests/conftest.py::_enforce_hermetic``).

Starts EMPTY for ``kubectl``. No test in this suite legitimately needs a
real, live cluster: the two tests that looked like they did
(``tests/test_deploy_runner.py::test_cli_returns_eight_on_first_party_mismatch``
and ``::test_noop_path_d3_recheck_clears_converged_and_exits_eight``) were
hermeticity LEAKS — the mesh gate's default ``mesh.MeshGate`` went
unstubbed and shelled to a REAL kubectl, which only "passed" because the
laptop happens to run a live k3s cluster (PR #380, CI run 37878027812).
They were fixed by STUBBING the mesh gate, never by listing them here.

If a genuine future need arises, add the marker + the entry together, and
explain in the ``reason`` why stubbing the seam is not possible for that
test.
"""

from __future__ import annotations

#: nodeid -> the exact tool name (``"kubectl"`` or ``"helm"``) that test's
#: ``@pytest.mark.allow_cluster_tool(tool=...)`` marker is permitted to name.
ALLOWED_CLUSTER_TOOL_TESTS: dict[str, str] = {}
