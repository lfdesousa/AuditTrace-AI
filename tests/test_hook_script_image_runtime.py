"""LC10A-HOOK-AWK, the PRIMARY guard: run the real hook script with a PATH that
holds ONLY the commands the pinned Keycloak image provides.

``tests/test_hook_script_image_commands.py`` reads the script statically; that
scan cannot see every way bash can run a command. This test executes the whole
script (the ConfigMap site, via the existing ``RunFullHarness``) with ``PATH``
set to a scratch directory of symlinks to exactly the measured image commands
(``IMAGE_EXTERNALS``, plus ``env``: harness plumbing for the fake cluster tool's
shebang, measured present in the image). A command the hook really executes that
the image lacks then fails with 127 / "command not found", whatever syntax
reached it. It covers every path the harness exercises (both realm states).

Neuter (RED, then GREEN on restore): ``test_a_missing_command_on_an_exercised_
path_is_caught`` injects ``command awk`` into ``chat_aud_resolve_scope`` and
must see ``awk: command not found``; and ``command awk`` placed in the real
resolver of the chart goes RED the same way.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests._hermetic_env import hermetic_env
from tests.test_acl_e2e_failure_diagnostics import (
    RunFullHarness,
    _listening_socket,
    _run_full_state,
)
from tests.test_chart_drift_guards_acl_write import CONFIGMAP
from tests.test_hook_script_image_commands import IMAGE_EXTERNALS

KCADM = "/opt/keycloak/bin/kcadm.sh"  # replaced by the harness's fake kcadm
PLUMBING = {"env"}  # the fake cluster tool's `#!/usr/bin/env bash`; in the image


class ImageOnlyHarness(RunFullHarness):
    """``RunFullHarness`` whose child ``PATH`` is the image command set only."""

    def __init__(self, tmp: Path, site: Path) -> None:
        super().__init__(tmp, site)
        self.image_bin = tmp / "image-bin"
        self.image_bin.mkdir()
        for name in sorted((IMAGE_EXTERNALS - {KCADM}) | PLUMBING):
            host = shutil.which(name)
            assert host is not None, f"{name} is not on the host PATH"
            (self.image_bin / name).symlink_to(host)

    def _env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        base = {**os.environ, "PATH": str(self.image_bin)}
        return hermetic_env(
            base,
            FAKE_STATE=str(self.state_path),
            FAKE_LOG=str(self.log),
            FAKE_MARKER_LOG=str(self.marker_log),
            # bash localises "command not found": pin it so the assertion on
            # that text cannot pass vacuously on a non-English host.
            LC_ALL="C",
            **(extra_env or {}),
        )


class CanaryHarness(ImageOnlyHarness):
    """Injects ``command awk`` into the chat resolver, on an exercised path."""

    def build_script(self, *, tcp_port: int) -> str:
        text = super().build_script(tcp_port=tcp_port)
        marker = 'done <<< "${csv}"'
        at = text.index(marker, text.index("chat_aud_resolve_scope() {"))
        injected = 'done <<< "$(printf %s "${csv}" | command awk 1)"'
        return text[:at] + injected + text[at + len(marker) :]


def _states() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("all-absent", _run_full_state(scopes_present=False, e2e_present=False)),
        ("all-present", _run_full_state(scopes_present=True, e2e_present=True)),
    ]


@pytest.mark.parametrize(
    ("label", "state"), _states(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_the_hook_runs_to_the_end_with_only_the_image_commands(
    tmp_path: Path, label: str, state: dict[str, Any]
) -> None:
    srv, port = _listening_socket()
    try:
        h = ImageOnlyHarness(tmp_path, CONFIGMAP)
        proc = h.run(state=state, tcp_port=port)
    finally:
        srv.close()
    out = proc.stdout + proc.stderr
    assert "command not found" not in out, (label, out[-2000:])
    assert proc.returncode != 127, (label, out[-2000:])
    assert "memory-scopes provisioning complete" in proc.stdout, (label, out[-2000:])


def test_a_missing_command_on_an_exercised_path_is_caught(tmp_path: Path) -> None:
    """Non-vacuity of the PATH restriction: the host HAS awk, the image PATH does
    not, so a ``command awk`` in the resolver (which the all-absent state runs)
    must surface as ``awk: command not found``."""
    srv, port = _listening_socket()
    try:
        h = CanaryHarness(tmp_path, CONFIGMAP)
        proc = h.run(
            state=_run_full_state(scopes_present=False, e2e_present=False),
            tcp_port=port,
        )
    finally:
        srv.close()
    out = proc.stdout + proc.stderr
    assert "awk: command not found" in out, out[-2000:]
    assert "memory-scopes provisioning complete" not in proc.stdout


def test_the_image_path_really_lacks_awk(tmp_path: Path) -> None:
    h = ImageOnlyHarness(tmp_path, CONFIGMAP)
    assert sorted(p.name for p in h.image_bin.iterdir()) == sorted(
        n for n in (IMAGE_EXTERNALS - {KCADM}) | PLUMBING
    )
    assert shutil.which("awk", path=str(h.image_bin)) is None
