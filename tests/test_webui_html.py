"""Regression guards for ``webui/index.html`` auth-state handling.

Two papercut bugs surfaced during the 2026-05-16 EOD WebUI
walkthrough that both wedged the SPA silently for an end user:

1. **Stale-token "logged in" state.** Page load restored the JWT
   from ``localStorage`` and switched UI to authenticated state
   *without* checking ``exp``. A 12-day-expired token showed
   "logged in" until the first API call returned 401.

2. **State-mismatch URL not stripped.** When the OIDC callback
   fired with ``?code=&state=`` but ``state`` did not match the
   stored value, the SPA logged an error and returned *without
   stripping the URL params*. Next reload re-triggered the same
   mismatch, infinitely.

These tests pin the bootstrap exp-check (``tokenIsValid``) and the
``history.replaceState`` calls on the two error branches so a
later refactor cannot silently revive either wedge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEBUI_HTML = Path(__file__).resolve().parents[1] / "webui" / "index.html"


@pytest.fixture(scope="module")
def webui_source() -> str:
    return WEBUI_HTML.read_text(encoding="utf-8")


def test_token_is_valid_helper_defined(webui_source: str) -> None:
    """``tokenIsValid`` must exist — bootstrap depends on it to
    decide whether a stored ``access_token`` is still usable.
    """
    assert "function tokenIsValid(token)" in webui_source, (
        "tokenIsValid helper missing — bootstrap will fall back "
        "to showing stale-token 'logged in' state."
    )
    # It must actually consult `exp`, not just be a no-op stub.
    helper_block = webui_source.split("function tokenIsValid(token)", 1)[1].split(
        "}", 1
    )[0]
    assert "c.exp" in helper_block, "tokenIsValid must check claims.exp"
    assert "Date.now()" in helper_block, "tokenIsValid must compare to current time"


def test_bootstrap_gates_show_token_on_validity(webui_source: str) -> None:
    """The bootstrap block restoring a stored token must gate the
    ``showToken`` call on ``tokenIsValid`` AND clear storage on
    expiry. Anything weaker re-introduces bug #1.
    """
    # The historical buggy form was a bare ``if (existing) showToken(existing);``.
    bare_pattern = re.compile(r"if\s*\(\s*existing\s*\)\s*showToken\(existing\)\s*;")
    assert not bare_pattern.search(webui_source), (
        "Bootstrap restores token unconditionally — must gate on tokenIsValid(existing)."
    )
    # The current form must AND the existence check with validity.
    gated_pattern = re.compile(
        r"if\s*\(\s*existing\s*&&\s*tokenIsValid\(existing\)\s*\)"
    )
    assert gated_pattern.search(webui_source), (
        "Bootstrap missing `if (existing && tokenIsValid(existing))` guard."
    )
    # And the expired branch must clear storage so we don't loop.
    assert (
        "SS_clear()"
        in webui_source.split("stored token expired", 1)[1].split(";", 3)[0:3].__str__()
    )


def test_state_mismatch_branch_strips_url(webui_source: str) -> None:
    """When ``state`` mismatch is detected the branch must scrub
    ``?code=&state=`` from the URL before returning. Without the
    scrub, a reload retriggers the same mismatch — bug #2.
    """
    # Slice from the mismatch log line down to the next `return;`.
    mismatch_idx = webui_source.find("state mismatch — possible CSRF")
    assert mismatch_idx != -1, "state-mismatch error log no longer present"
    branch = webui_source[mismatch_idx : mismatch_idx + 400]
    assert "history.replaceState" in branch, (
        "state-mismatch branch missing history.replaceState — URL stays dirty, "
        "reload re-triggers the same mismatch infinitely."
    )
    # The replaceState call must precede the return.
    rs_pos = branch.find("history.replaceState")
    ret_pos = branch.find("return;")
    assert 0 < rs_pos < ret_pos, (
        "history.replaceState must execute before the early return"
    )


def test_token_exchange_failure_strips_url(webui_source: str) -> None:
    """A 4xx/5xx from the token endpoint leaves ``?code=&state=``
    in the URL the same way a state mismatch does. Same fix.
    """
    marker = "token endpoint returned"
    idx = webui_source.find(marker)
    assert idx != -1, "token-endpoint error log no longer present"
    branch = webui_source[idx : idx + 400]
    assert "history.replaceState" in branch, (
        "token-error branch missing history.replaceState — dirty URL survives "
        "a reload and the user lands back in the failing exchange path."
    )
