"""Neuter proof (spec 2026-09-10-SPEC-deploy-runner-target-overlay-aware).

`scripts/deploy/runner.py` used to read chart values from base
``values.yaml`` ONLY (``CHART_VALUES_FILE``; ``_read_chart_values()``'s
default). Base has ``console.enabled: false``. The runner had NO
``-f``/``--values`` CLI flag, so it never told ``_read_chart_values()`` —
or ``helm upgrade`` itself — about ``charts/audittrace/values-laptop.yaml``,
where ``console.enabled: true`` actually lives on the laptop target. So on
the real laptop deploy: :func:`console_image_set_args` and
:func:`console_image_digests` both read the base ``console.enabled=false``
and returned ``[]``/``{}`` — the apply-side console `--set` fix (#333) and
the convergence fix (#336) were both INERT on the actual target (origin
finding
``finding-deploy-runner-not-overlay-aware-console-inert-20260910.md``,
verified live at the v1.26.0 Part C.4 redeploy: helm revision stayed at
265, no new upgrade ran).

This module proves, with the REAL committed chart files (no synthetic
stand-ins for the laptop overlay itself — the whole point is the runner's
view of the ACTUAL target config):

1. ``_read_chart_values()`` reports the EFFECTIVE (deep-merged) view when
   given ``values_files=(values-laptop.yaml,)`` — ``console.enabled`` flips
   from the base's ``False`` to the overlay's ``True``.
2. That effective view flows, unmocked, through
   :func:`console_image_set_args` (the runner's apply-side console `--set`
   emission) and through the production ``_helm_apply_cmd``/``_is_converged``
   default paths (``chart_values=None``) — the ACTUAL apply/convergence
   code, not a hand-rolled equivalent.
3. Deep-merge precedence: an overlay key overrides the matching base key;
   sibling keys in the SAME nested block, set only in one file, both
   survive (mirrors Helm's own ``-f`` merge semantics).
4. No ``--values`` given ⇒ byte-identical to the pre-fix behaviour (base
   only), including the helm argv carrying no stray ``-f``.

**Falsifiability.** Every "with overlay" test in this module is paired with
an equivalent "without overlay" assertion using the SAME production code
path and the SAME real chart files. Neutering the fix — e.g. making
``_read_chart_values()`` ignore ``values_files`` (base only), or dropping
the ``-f`` wiring from ``_helm_apply_cmd`` — collapses the "with overlay"
case to the "without overlay" case: the console `--set` args vanish, the
``-f`` token vanishes, and the convergence check goes blind to console
drift again. Every such collapse is asserted against directly, so the
regression goes RED without any code change to the test itself.

Anchors: ``feedback_vacuous_neuter_test_antipattern``, ``feedback_no_more_drifts``,
``feedback_laptop_rollout_pin_local_repository``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.deploy import registry, runner
from scripts.deploy.runner import DeployConfig, DeployRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
CHART_VALUES_FILE = CHART_DIR / "values.yaml"
LAPTOP_VALUES_FILE = CHART_DIR / "values-laptop.yaml"

# The real, committed digests this repo pins today (values.yaml, WU-6 Part
# C re-pin, 2026-09-10) — asserted against directly so a regression that
# silently stops reading them is caught, not just "some string present".
_REAL_LIBRECHAT_DIGEST = (
    "sha256:edd23f45e60810e3d4ab94e7fe2429d802867a0a2ab284ecbad09596b0490527"
)
_REAL_BFF_DIGEST = (
    "sha256:e15af5a03581d521492b01ad1d7a2ac521b55dc9aa2773bcfa3344b6db2e54b7"
)

# Mirrors the Makefile helm-lint / tests/test_deploy_runner_console_image_pins.py
# secret placeholders so the real `helm template` render below never blocks
# on a `required` chart guard.
_LINT_SECRETS: list[str] = []
for _kv in (
    "secrets.minio.secretKey=preflight",
    "secrets.minio.kmsKey=preflight",
    "secrets.chromadb.token=preflight",
    "secrets.keycloak.adminPassword=preflight",
    "secrets.postgres.appPassword=preflight",
    "secrets.postgres.password=preflight",
    "secrets.redis.password=preflight",
    "secrets.summariser.password=preflight",
):
    _LINT_SECRETS.extend(["--set", _kv])


# ── helpers (self-contained, mirrors tests/test_deploy_runner.py) ───────────


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("settle_interval", 0.0)
    kw.setdefault("target_version", "v1.26.0")
    return DeployConfig(**kw)


def _hub_ref(digest="sha256:x"):
    return registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "1.26.0", digest, "hub"
    )


class _Dispatcher:
    """Routes runner._run calls to canned CompletedProcess results by token match."""

    def __init__(self, rules, default=None):
        self.rules = rules
        self.default = default or _proc(0, "")
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, env=None):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return self.default


def _console_dispatch_rules(
    *,
    librechat_digest,
    bff_digest,
    memory_server_digest="sha256:x",
    helm_status="deployed",
):
    """Order matters: `component=librechat-bff` MUST be matched before the
    plainer `component=librechat` needle (a substring of the former)."""
    return [
        (
            "component=memory-server",
            _proc(0, f"docker.io/lfds/audittrace-memory-server@{memory_server_digest}"),
        ),
        ("component=librechat-bff", _proc(0, f"repo/bff@{bff_digest}")),
        ("component=librechat", _proc(0, f"repo/librechat@{librechat_digest}")),
        (
            "helm status",
            _proc(0, json.dumps({"version": 266, "info": {"status": helm_status}})),
        ),
        ("helm upgrade", _proc(0, "deployed")),
    ]


# ── (1) _read_chart_values() effective view — the core acceptance test ─────


def test_base_only_console_disabled_sanity():
    """Sanity precondition: base values.yaml alone has console.enabled=False
    — if this ever flips, the rest of this module's "overlay flips it to
    True" assertions would be vacuous."""
    base_only = runner._read_chart_values(CHART_VALUES_FILE)
    assert base_only["console"]["enabled"] is False


def test_read_chart_values_with_laptop_overlay_reports_console_enabled():
    """The spec's own Rule-1 acceptance test, verbatim: with the REAL
    ``values-laptop.yaml`` as an overlay, ``_read_chart_values()`` reports
    ``console.enabled=True`` — the effective view a laptop deploy actually
    runs with, not the base-only view that was silently used before this
    fix.

    Falsifiable: ignore ``values_files`` in ``_read_chart_values`` (the
    pre-fix behaviour) and this assertion flips to ``False`` — identical to
    :func:`test_base_only_console_disabled_sanity` above.
    """
    effective = runner._read_chart_values(
        CHART_VALUES_FILE, values_files=(LAPTOP_VALUES_FILE,)
    )
    assert effective["console"]["enabled"] is True


def test_read_chart_values_overlay_default_base_path():
    """Same as above but exercising the DEFAULT ``path=`` (the production
    call shape: only ``values_files`` supplied)."""
    effective = runner._read_chart_values(values_files=(LAPTOP_VALUES_FILE,))
    assert effective["console"]["enabled"] is True


# ── (2) deep-merge precedence, using the real base + real overlay ──────────


def test_deep_merge_overlay_wins_but_sibling_base_keys_survive():
    """The overlay sets `console.librechat.host` (a key ABSENT from base);
    base sets `console.librechat.image.*` (a sibling key ABSENT from the
    overlay). Both must survive the merge — proves this is a recursive
    key-by-key merge, not a block-level replace that would silently drop
    the console image pins the moment ANY overlay touches `console.librechat`."""
    effective = runner._read_chart_values(values_files=(LAPTOP_VALUES_FILE,))
    librechat = effective["console"]["librechat"]
    # from the overlay
    assert librechat["host"] == "librechat.audittrace.local"
    # from base — would be silently lost under a shallow/replace merge
    assert librechat["image"]["digest"] == _REAL_LIBRECHAT_DIGEST


def test_deep_merge_precedence_synthetic(tmp_path):
    """Synthetic base/overlay pair isolates the merge algorithm itself from
    the real chart's current shape (which moves over time)."""
    base = tmp_path / "base.yaml"
    overlay = tmp_path / "overlay.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "top": {"a": 1, "b": {"nested": "base-value", "keep": "base-only"}},
                "base_only_top": True,
            }
        )
    )
    overlay.write_text(
        yaml.safe_dump({"top": {"a": 2, "b": {"nested": "overlay-value"}}})
    )
    merged = runner._read_chart_values(base, values_files=(overlay,))
    assert merged["top"]["a"] == 2  # overlay wins on a direct scalar conflict
    assert merged["top"]["b"]["nested"] == "overlay-value"  # overlay wins, nested
    assert merged["top"]["b"]["keep"] == "base-only"  # base-only sibling survives
    assert merged["base_only_top"] is True  # base-only top-level key survives


def test_deep_merge_ordered_multiple_overlays_later_wins(tmp_path):
    base = tmp_path / "base.yaml"
    overlay1 = tmp_path / "o1.yaml"
    overlay2 = tmp_path / "o2.yaml"
    base.write_text(yaml.safe_dump({"x": "base"}))
    overlay1.write_text(yaml.safe_dump({"x": "first"}))
    overlay2.write_text(yaml.safe_dump({"x": "second"}))
    merged = runner._read_chart_values(base, values_files=(overlay1, overlay2))
    assert merged["x"] == "second"
    # reversed order -> reversed winner, proving order (not just "an
    # overlay wins") drives the result.
    merged_reversed = runner._read_chart_values(base, values_files=(overlay2, overlay1))
    assert merged_reversed["x"] == "first"


# ── (3) no --values given -> byte-identical to pre-fix (backward compat) ───


def test_read_chart_values_no_overlays_matches_single_file_parse():
    assert runner._read_chart_values(CHART_VALUES_FILE) == runner._read_chart_values(
        CHART_VALUES_FILE, values_files=()
    )


def test_helm_apply_cmd_no_overlay_no_stray_dash_f():
    cfg = DeployConfig(target_version="v1.26.0")
    ref = _hub_ref("sha256:x")
    cmd = runner._helm_apply_cmd(cfg, ref)
    assert "-f" not in cmd


# ── (4) console_image_set_args sees the effective (merged) values ──────────


def test_console_image_set_args_with_laptop_overlay_emits_real_pins():
    """Real end-to-end: base + laptop overlay merged, fed straight into the
    UNMOCKED `console_image_set_args` — the exact function the apply-side
    `--set` argv is built from. Falsifiable: if the overlay never reached
    this function (the pre-fix defect), it would see `console.enabled=False`
    and return `[]`."""
    effective = runner._read_chart_values(values_files=(LAPTOP_VALUES_FILE,))
    args = runner.console_image_set_args(effective)
    assert args, "expected non-empty --set argv with console enabled via overlay"
    joined = " ".join(args)
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in joined
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in joined


def test_console_image_set_args_without_overlay_stays_empty():
    """The CONTROL case: base values.yaml alone (no `-f`) -> console
    disabled -> no console `--set` args. Paired with the overlay test above
    so the contrast — not just one isolated assertion — is what proves the
    fix."""
    base_only = runner._read_chart_values(CHART_VALUES_FILE)
    assert runner.console_image_set_args(base_only) == []


# ── (5) the -f argv itself, in order, positioned before --set ──────────────


def test_helm_apply_cmd_carries_dash_f_for_each_overlay_in_order():
    cfg = DeployConfig(
        target_version="v1.26.0",
        values_files=(LAPTOP_VALUES_FILE, Path("/tmp/second-overlay.yaml")),
    )
    ref = _hub_ref("sha256:x")
    cmd = runner._helm_apply_cmd(cfg, ref, chart_values={"console": {"enabled": False}})
    idx_chart = cmd.index(str(CHART_DIR))
    idx_f1 = cmd.index("-f")
    assert idx_f1 > idx_chart, "-f must come after the chart directory"
    assert cmd[idx_f1 + 1] == str(LAPTOP_VALUES_FILE)
    assert cmd[idx_f1 + 2] == "-f"
    assert cmd[idx_f1 + 3] == "/tmp/second-overlay.yaml"
    idx_set = cmd.index("--set")
    assert idx_f1 < idx_set, "-f must come before --set"
    idx_reset = cmd.index("--reset-then-reuse-values")
    assert idx_f1 < idx_reset, "-f must come before --reset-then-reuse-values"


# ── (6) THE core neuter proof: production default path, with vs without ───


def test_helm_apply_cmd_default_path_console_sets_appear_only_with_overlay():
    """The critical falsifiable pair, through the REAL production default
    path (``chart_values=None`` — never test-injected): with the laptop
    overlay wired into ``cfg.values_files``, the SAME ``_helm_apply_cmd``
    call that used to be permanently blind now emits the console `--set`
    args; without it, the argv is exactly what it was before this fix.

    Falsifiable: neuter ``_read_chart_values`` to ignore ``values_files``
    (or drop it from ``_helm_apply_cmd``'s default read) and the "with
    overlay" argv collapses to the "without overlay" argv — this test goes
    RED because the `console.` assertions below stop matching.
    """
    ref = _hub_ref("sha256:x")

    with_overlay = runner._helm_apply_cmd(
        DeployConfig(target_version="v1.26.0", values_files=(LAPTOP_VALUES_FILE,)), ref
    )
    without_overlay = runner._helm_apply_cmd(
        DeployConfig(target_version="v1.26.0"), ref
    )

    with_joined = " ".join(with_overlay)
    without_joined = " ".join(without_overlay)

    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in with_joined
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in with_joined
    assert "-f " + str(LAPTOP_VALUES_FILE) in with_joined

    assert "console." not in without_joined
    assert "-f" not in without_overlay


# ── (7) convergence: overlay-aware vs blind, same live cluster state ───────


def test_is_converged_detects_console_drift_with_overlay(tmp_path, monkeypatch):
    """With the laptop overlay wired in, a live console pod running a STALE
    digest (memory-server itself already converged) is correctly detected
    as NOT converged — exactly the fix the v1.26.0 Part C.4 finding demands.
    """
    disp = _Dispatcher(
        rules=_console_dispatch_rules(
            librechat_digest="sha256:STALE-ON-CLUSTER", bff_digest=_REAL_BFF_DIGEST
        )
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path, values_files=(LAPTOP_VALUES_FILE,)))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged()
    assert check.converged is False
    assert "console mismatch: librechat" in check.basis


def test_is_converged_blind_to_same_console_drift_without_overlay(
    tmp_path, monkeypatch
):
    """The CONTROL case reproducing the live defect hermetically: the
    IDENTICAL stale-console cluster state as the test above, but with no
    `--values` overlay wired in (``cfg.values_files`` defaults to ``()``) —
    convergence reads base ``console.enabled=False``, never even looks at
    the console pods, and wrongly reports converged. This is the exact
    v1.26.0 Part C.4 symptom (helm revision stayed at 265) reproduced
    without a cluster."""
    memory_server_doc = {
        "kind": "Deployment",
        "metadata": {"name": "audittrace-memory-server"},
        "spec": {
            "template": {"spec": {"containers": [{"name": "memory-server", "env": []}]}}
        },
    }
    disp = _Dispatcher(
        rules=[
            *_console_dispatch_rules(
                librechat_digest="sha256:STALE-ON-CLUSTER", bff_digest=_REAL_BFF_DIGEST
            ),
            # config-drift check (spec 2026-09-10-SPEC-deploy-runner-
            # convergence-config-drift): memory-server only, since base
            # console.enabled=False skips the console workloads here too —
            # matching intended + live specs so this stays the TRUE no-op
            # control case the test name promises.
            ("helm template", _proc(0, json.dumps(memory_server_doc))),
            (
                "get deployment audittrace-memory-server -n",
                _proc(0, json.dumps(memory_server_doc)),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))  # no values_files -> base only
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged()
    assert check.converged is True
    assert "console" not in check.basis
    assert not any("librechat" in " ".join(c) for c in disp.calls)


def test_chart_apply_end_to_end_reconciles_console_drift_with_overlay(
    tmp_path, monkeypatch
):
    """Full `phase_chart_apply` (not just `_is_converged`): with the overlay
    wired in, the stale console digest makes `helm upgrade` actually RUN —
    the P2 no-op this whole spec exists to close."""
    disp = _Dispatcher(
        rules=_console_dispatch_rules(
            librechat_digest="sha256:STALE-ON-CLUSTER", bff_digest=_REAL_BFF_DIGEST
        )
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path, values_files=(LAPTOP_VALUES_FILE,)))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status != "noop"
    upgrade = next(c for c in disp.calls if "helm upgrade" in " ".join(c))
    assert "-f" in upgrade and str(LAPTOP_VALUES_FILE) in upgrade
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in " ".join(
        upgrade
    )


# ── (8) CLI surface: --values / -f, repeatable, ordered ─────────────────────


def test_build_parser_values_flag_repeatable_and_ordered():
    args = runner.build_parser().parse_args(
        [
            "--target-version",
            "v1.26.0",
            "-f",
            "charts/audittrace/values-laptop.yaml",
            "--values",
            "/tmp/second.yaml",
        ]
    )
    cfg = runner.config_from_args(args)
    assert cfg.values_files == (
        Path("charts/audittrace/values-laptop.yaml"),
        Path("/tmp/second.yaml"),
    )


def test_config_from_args_defaults_values_files_empty():
    args = runner.build_parser().parse_args(["--target-version", "v1.26.0"])
    cfg = runner.config_from_args(args)
    assert cfg.values_files == ()


def test_cli_dry_run_shows_dash_f_and_console_sets_for_laptop_overlay(
    tmp_path, monkeypatch, capsys
):
    """The exact acceptance shape from the spec's `--dry-run` example:
    `--target-version v1.26.0 -f charts/audittrace/values-laptop.yaml`
    prints an argv carrying `-f <overlay>` AND the console `--set` pins."""
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    monkeypatch.setattr(
        registry,
        "resolve",
        lambda v, reg: registry.ImageRef(
            "docker.io/lfds/audittrace-memory-server", "1.26.0", "sha256:abc", "hub"
        ),
    )
    rc = runner.main(
        [
            "--target-version",
            "v1.26.0",
            "-f",
            str(LAPTOP_VALUES_FILE),
            "--dry-run",
            "--out-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"-f {LAPTOP_VALUES_FILE}" in out
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in out
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in out


def test_cli_dry_run_without_values_flag_no_console_sets(tmp_path, monkeypatch, capsys):
    """Backward-compat CONTROL: the exact same CLI invocation minus `-f`
    reproduces today's (pre-overlay) behaviour — no console `--set`, no
    `-f` token."""
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    monkeypatch.setattr(
        registry,
        "resolve",
        lambda v, reg: registry.ImageRef(
            "docker.io/lfds/audittrace-memory-server", "1.26.0", "sha256:abc", "hub"
        ),
    )
    rc = runner.main(
        [
            "--target-version",
            "v1.26.0",
            "--dry-run",
            "--out-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "console." not in out
    assert " -f " not in out


@pytest.mark.skipif(
    __import__("shutil").which("helm") is None,
    reason="helm CLI not on PATH — this render needs a real helm template",
)
def test_helm_template_accepts_dash_f_laptop_overlay_and_renders_console():
    """Belt-and-braces: the actual `-f <overlay>` argv the runner emits is
    valid helm syntax that really does flip the rendered manifest set —
    proving the `-f` wiring isn't just a string in a list nobody consumes."""
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "-n",
        "audittrace",
        "--set",
        "vault.enabled=false",
        "-f",
        str(LAPTOP_VALUES_FILE),
        *_LINT_SECRETS,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    names = {d.get("metadata", {}).get("name", "") for d in docs if d.get("kind")}
    assert any(n.endswith("-librechat") for n in names)
    assert any(n.endswith("-librechat-bff") for n in names)
