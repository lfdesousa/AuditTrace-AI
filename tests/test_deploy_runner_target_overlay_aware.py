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

# The real, committed digests this repo pins today (values.yaml, Phase-E
# re-pin, 2026-09-18, spec 2026-09-18-SPEC-e2e-deploy-and-mongo-audit.md,
# superseding the WU-6 Part C 2026-09-10 pair) — asserted against directly
# so a regression that silently stops reading them is caught, not just
# "some string present".
_REAL_LIBRECHAT_DIGEST = (
    "sha256:4db01e94ee7b321f37475261b5d66c73f5aa88144bfbbd3eff7e6922b24f905a"
)
# BFF-BUMP-1.29.1 re-pin (2026-10-08).
_REAL_BFF_DIGEST = (
    "sha256:b6e907c06d4a603ce524284fefc37be9c008a38a4f9680a82356abfa66fe7212"
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


def test_console_image_set_args_without_overlay_still_emits_real_pins():
    """BFF-BUMP-1.29.1 D2 supersedes the old CONTROL here: base
    values.yaml alone (no `-f`, `console.enabled: false` on disk) still
    emits the SAME six `--set` args as the laptop-overlay case above —
    the console's `--set` pins are now UNCONDITIONAL (presence on the
    cluster is decided by the template's own `console.enabled` guard, not
    by whether the runner asserts the pins). Falsifiable: restore the
    removed `console.enabled` gate in `console_image_set_args` and this
    goes RED (back to `[]`)."""
    base_only = runner._read_chart_values(CHART_VALUES_FILE)
    args = runner.console_image_set_args(base_only)
    joined = " ".join(args)
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in joined
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in joined


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


def test_helm_apply_cmd_default_path_overlay_moves_only_the_dash_f_token():
    """Through the REAL production default path (``chart_values=None`` —
    never test-injected): with the laptop overlay wired into
    ``cfg.values_files``, the SAME ``_helm_apply_cmd`` call carries the
    ``-f`` token AND the console `--set` pins; without it, the console
    `--set` pins are STILL present (D2, unconditional) but the ``-f``
    token is gone. The overlay-sensitive falsifiable surface is `-f`
    alone now — see
    ``test_read_chart_values_with_laptop_overlay_reports_console_enabled``
    for the ``console.enabled`` flip itself.

    Falsifiable: neuter ``_read_chart_values`` to ignore ``values_files``
    (or drop it from ``_helm_apply_cmd``'s default read) and the ``-f``
    token disappears from the "with overlay" argv too — this test goes RED
    on the ``-f`` assertion.
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

    # D2: the console pins are unconditional, present EITHER way now.
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in without_joined
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in without_joined
    assert "-f" not in without_overlay


# ── (7) convergence: manifest-derived, overlay NEVER matters (Rule B1) ─────
# BFF-BUMP-1.29.1 Rule B1 supersedes this section's old with/without-overlay
# split. `_is_converged()` used to read console presence off the FILE-side
# `console.enabled` flag — blind without `-f values-laptop.yaml` (the exact
# live defect: rev 272 no-op'd a stale BFF override straight past the
# re-pinned chart). This file's former
# `test_is_converged_blind_to_same_console_drift_without_overlay` pinned
# that blindness as CORRECT behaviour — DELETED per Rule B1; the single
# test below is its inverse, covering both the overlay and no-overlay case
# with the SAME (now overlay-independent) outcome.


def _console_drift_manifest_yaml(librechat_digest, bff_digest, memory_server_digest):
    def _doc(name, component, container, image):
        return {
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app.kubernetes.io/component": component}},
                "template": {
                    "spec": {"containers": [{"name": container, "image": image}]}
                },
            },
        }

    docs = [
        _doc(
            "audittrace-memory-server",
            "memory-server",
            "memory-server",
            f"docker.io/lfds/audittrace-memory-server:1.26.0@{memory_server_digest}",
        ),
        _doc(
            "audittrace-librechat",
            "librechat",
            "librechat",
            f"docker.io/lfds/audittrace-librechat:768de61@{librechat_digest}",
        ),
        _doc(
            "audittrace-librechat-bff",
            "librechat-bff",
            "bff",
            f"docker.io/lfds/audittrace-librechat-bff:1.29.1@{bff_digest}",
        ),
    ]
    return "\n---\n".join(json.dumps(d) for d in docs)


def _pods_json_proc(container, digest):
    return _proc(
        0,
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"labels": {}},
                        "status": {
                            "containerStatuses": [
                                {"name": container, "imageID": f"repo@{digest}"}
                            ]
                        },
                    }
                ]
            }
        ),
    )


def test_is_converged_detects_console_drift_regardless_of_overlay(
    tmp_path, monkeypatch
):
    """A stale STORED BFF/librechat override already visible in the LIVE
    manifest (the rev-272 shape) is detected as NOT converged whether or
    not `-f values-laptop.yaml` is passed — presence is derived from
    `helm get manifest`, never the file-side `console.enabled` flag.

    Falsifiable / neuter: re-gate the console rows on file-side
    `console.enabled` (restore the old mechanism) and the ``values_files=()``
    iteration goes RED — `check.converged` flips to True, reproducing the
    rev-272 defect exactly as it ran live.
    """
    manifest_yaml = _console_drift_manifest_yaml(
        "sha256:STALE-ON-CLUSTER", _REAL_BFF_DIGEST, "sha256:x"
    )
    for values_files in ((LAPTOP_VALUES_FILE,), ()):
        disp = _Dispatcher(
            rules=[
                ("helm get manifest", _proc(0, manifest_yaml)),
                (
                    '"memory-server")].imageID',
                    _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x"),
                ),
                ("component=librechat-bff", _pods_json_proc("bff", _REAL_BFF_DIGEST)),
                (
                    "component=librechat",
                    _pods_json_proc("librechat", "sha256:STALE-ON-CLUSTER"),
                ),
                (
                    "component=memory-server",
                    _pods_json_proc("memory-server", "sha256:x"),
                ),
            ]
        )
        monkeypatch.setattr(runner, "_run", disp)
        r = DeployRunner(_cfg(tmp_path, values_files=values_files))
        r.image_ref = _hub_ref("sha256:x")
        check = r._is_converged()
        assert check.converged is False, (
            f"expected NOT converged for values_files={values_files!r}"
        )
        assert "first-party mismatch: librechat" in check.basis


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


def test_chart_apply_end_to_end_without_overlay_still_emits_six_console_sets(
    tmp_path, monkeypatch
):
    """B-1's third leg (review finding): file-side `console.enabled=false`
    (base values.yaml, NO `-f` overlay) must STILL carry the six console
    `--set` pins in the REAL `helm upgrade` argv once `phase_chart_apply`
    actually applies (D2, unconditional) — not merely in a dry-run plan
    line. Only `test_chart_apply_end_to_end_reconciles_console_drift_with_
    overlay` above exercised this end to end, and only WITH the overlay —
    this is its missing without-overlay counterpart.
    """
    disp = _Dispatcher(rules=[("helm upgrade", _proc(0, "deployed"))])
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(
        _cfg(tmp_path)
    )  # no values_files -> base only, console.enabled=false
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.records[0].status != "noop"
    upgrade = next(c for c in disp.calls if "helm upgrade" in " ".join(c))
    joined = " ".join(upgrade)
    assert "-f" not in upgrade
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in joined
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in joined


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


def test_cli_dry_run_without_values_flag_still_shows_console_sets_but_no_dash_f(
    tmp_path, monkeypatch, capsys
):
    """BFF-BUMP-1.29.1 D2 supersedes the old "no console `--set`" CONTROL:
    the exact same CLI invocation minus `-f` still shows the console
    `--set` pins (unconditional now) — only the `-f` token is gone."""
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
    assert f"console.librechat.image.digest={_REAL_LIBRECHAT_DIGEST}" in out
    assert f"console.bff.image.digest={_REAL_BFF_DIGEST}" in out
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
