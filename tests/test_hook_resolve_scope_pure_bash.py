"""LC10A-HOOK-AWK: ``chat_aud_resolve_scope`` and ``session_e2e_resolve_scope``
resolve a realm client-scope id from kcadm CSV in pure bash (the hook image has
no awk). The REAL functions are lifted from the rendered hook script and run
under a fake ``acl_e2e_kc``; the semantics pinned are the ones the awk one-liner
had: the FIRST row whose 2nd CSV field equals the name EXACTLY yields field 1; a
kcadm failure reaches ``read-failed``; an absent name reaches ``id-unresolvable``.

Neuters (RED, then GREEN on restore): compare by prefix (``== "${name}"*``);
drop the ``|| acl_e2e_fail read-failed`` arm; drop the ``break`` (last match
wins instead of first).
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.test_chart_drift_guards_acl_write import CONFIGMAP, _preamble_text
from tests.test_hook_script_image_commands import _rendered_hook_script

FUNCTIONS = ["chat_aud_resolve_scope", "session_e2e_resolve_scope"]


def _function(script: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.S | re.M)
    assert m is not None, f"{name} not found in the rendered hook script"
    return m.group(0)


def _run(fn: str, *, csv: str, kcadm_rc: int = 0, preset: str = "") -> tuple[int, str]:
    script = _rendered_hook_script()
    prog = (
        "set -euo pipefail\n"
        + _preamble_text(CONFIGMAP)
        + 'REALM="audittrace"\ndeclare -A SCOPE_ID\n'
        + 'acl_e2e_kc() { printf "%s" "${FAKE_CSV}"; return "${FAKE_RC}"; }\n'
        + _function(script, fn)
        + (f'SCOPE_ID["audittrace:query"]="{preset}"\n' if preset else "")
        + f'{fn} "audittrace:query"\n'
        + 'echo "RESOLVED=${SCOPE_ID[audittrace:query]}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", prog],
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE_CSV": csv, "FAKE_RC": str(kcadm_rc)},
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize("fn", FUNCTIONS)
class TestResolveScopeSemantics:
    def test_the_matching_row_yields_field_one(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-a,profile\nid-q,audittrace:query\nid-z,other\n")
        assert rc == 0, out
        assert "RESOLVED=id-q" in out

    def test_the_first_of_two_matching_rows_wins(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-1,audittrace:query\nid-2,audittrace:query\n")
        assert rc == 0, out
        assert "RESOLVED=id-1" in out

    def test_a_name_that_is_a_prefix_of_another_does_not_match(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-p,audittrace:query:extra\nid-q,audittrace:query\n")
        assert rc == 0, out
        assert "RESOLVED=id-q" in out

    def test_a_longer_name_alone_is_not_a_match(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-p,audittrace:query:extra\nid-r,xaudittrace:query\n")
        assert rc != 0
        assert "code=id-unresolvable" in out
        assert "RESOLVED" not in out

    def test_the_name_in_field_one_is_not_a_match(self, fn: str) -> None:
        rc, out = _run(fn, csv="audittrace:query,id-x\n")
        assert rc != 0
        assert "code=id-unresolvable" in out

    def test_an_absent_name_reaches_id_unresolvable(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-a,profile\nid-b,email\n")
        assert rc != 0
        assert "code=id-unresolvable" in out
        assert "RESOLVED" not in out

    def test_an_empty_listing_reaches_id_unresolvable(self, fn: str) -> None:
        rc, out = _run(fn, csv="")
        assert rc != 0
        assert "code=id-unresolvable" in out

    def test_a_kcadm_failure_reaches_read_failed_even_with_output(
        self, fn: str
    ) -> None:
        rc, out = _run(fn, csv="id-q,audittrace:query\n", kcadm_rc=1)
        assert rc != 0
        assert "code=read-failed" in out
        assert "RESOLVED" not in out

    def test_a_cached_id_short_circuits_without_calling_kcadm(self, fn: str) -> None:
        rc, out = _run(fn, csv="", kcadm_rc=1, preset="cached-id")
        assert rc == 0, out
        assert "RESOLVED=cached-id" in out

    def test_a_trailing_row_without_newline_is_read(self, fn: str) -> None:
        rc, out = _run(fn, csv="id-a,profile\nid-q,audittrace:query")
        assert rc == 0, out
        assert "RESOLVED=id-q" in out
