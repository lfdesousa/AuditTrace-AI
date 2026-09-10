"""Neuter proof (spec 2026-09-10, deploy-runner-deterministic-console-image-pins).

`scripts/deploy/runner.py` applies the chart with `helm upgrade --install
--reset-then-reuse-values` and used to only ever `--set` the memory-server
image. Console images (`console.librechat.image`, `console.bff.image`) are
chart-`values.yaml`-driven, so a stale stored per-image `--set` override
from a pre-D2-5C deploy (e.g. `console.librechat.image.tag=0f08e22
--set console.librechat.image.digest=sha256:2d9590a5...`) survives
`--reset-then-reuse-values` and shadows the correct new values.yaml default
forever — observed live at the v1.26.0 WU-6 Part C deploy (helm rev 265,
2026-09-10): the fork console UI stayed on the stale digest instead of
rolling to the chart's committed pin.

This module proves BOTH directions with a real `helm template` render (no
mocked helm, no cluster — same discipline as
`tests/test_console_wu3c_testable_deploy.py`):

1. `test_without_runner_console_sets_stale_value_shadows_chart` — the
   CONTROL case: with no console `--set` args (the pre-fix runner
   behaviour), a stale stored override wins over the chart's committed
   default. This is the live defect, reproduced hermetically.
2. `test_runner_console_sets_override_stale_stored_value` — the FIX: the
   runner's own `console_image_set_args(...)` output, appended to the helm
   argv, makes the chart's committed pin win over the exact same stale
   stored override. **Falsifiable**: neuter `_helm_apply_cmd` to drop
   `*console_image_set_args(chart_values)` (or hardcode
   `console_image_set_args` to return `[]`) and this test goes RED — the
   rendered image reverts to the stale tag/digest asserted against in (1).

Plus the `console.enabled=false` path (spec §2): must still render cleanly
with no console Deployment and no spurious `--set`.

Anchors: `feedback_vacuous_neuter_test_antipattern`, `feedback_no_more_drifts`,
`feedback_laptop_rollout_pin_local_repository` (#394, the memory-server
precedent for this exact `--reset-then-reuse-values` gotcha).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.deploy import registry, runner
from scripts.deploy.runner import DeployConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
CHART_VALUES_FILE = CHART_DIR / "values.yaml"

RELEASE = "audittrace"
NAMESPACE = "audittrace"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — this neuter-proof needs a real helm template render",
)

# Mirrors the Makefile helm-lint / test_console_wu3c_testable_deploy.py
# secret placeholders so a `required` chart guard never blocks the render.
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

# Synthetic stale pins, deliberately DISTINCT from whatever the committed
# chart's `console.*.image.{tag,digest}` currently are (which move over
# time — see values.yaml's WU-6 Part C re-pin history). Using a fabricated,
# obviously-not-a-real-tag value (rather than hardcoding the specific
# pre-/post-repin pair from the spec's narrative, e.g. `0f08e22` /
# `c879b74`) keeps this test correct regardless of which side of a chart
# re-pin `main` happens to be on at review time — the shape of the defect
# (a stale stored per-image override shadows the chart default forever) is
# what's under test, not one specific historical value pair.
_STALE_LIBRECHAT_TAG = "STALE-PRE-D2-5C-OVERRIDE"
_STALE_LIBRECHAT_DIGEST = "sha256:" + "1" * 64
_STALE_BFF_TAG = "STALE-WU3B-OVERRIDE"
_STALE_BFF_DIGEST = "sha256:" + "2" * 64


def _real_chart_values() -> dict[str, Any]:
    values = runner._read_chart_values(CHART_VALUES_FILE)
    assert values, "committed charts/audittrace/values.yaml must parse to a mapping"
    return values


def _console_image_pins(values: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The real `console.{librechat,bff}.image` blocks straight off the
    committed values.yaml, regardless of `console.enabled`'s current value
    (this helper is used to build the runner's `--set` args independent of
    that toggle — see `console_image_set_args`'s own enabled-gating, which
    IS exercised separately by the disabled-path test below)."""
    console = values["console"]
    pins = {
        "librechat": console["librechat"]["image"],
        "bff": console["bff"]["image"],
    }
    # Sanity guard against a false pass: if the committed chart ever happened
    # to carry these exact synthetic values, every assertion below would be
    # vacuously satisfied without the fix doing any work.
    assert pins["librechat"]["tag"] != _STALE_LIBRECHAT_TAG
    assert pins["librechat"]["digest"] != _STALE_LIBRECHAT_DIGEST
    assert pins["bff"]["tag"] != _STALE_BFF_TAG
    assert pins["bff"]["digest"] != _STALE_BFF_DIGEST
    return pins


@pytest.fixture
def stale_values_file(tmp_path) -> Path:
    """Simulates a Helm release value stored BEFORE the console image pins
    lived in `values.yaml` (the WU-6 Part C live defect): an explicit
    per-image `console.<component>.image.tag`/`.digest` override that
    `--reset-then-reuse-values` re-applies on every subsequent upgrade,
    forever shadowing whatever the chart's own default has moved to."""
    path = tmp_path / "stale-release-values.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "console": {
                    "enabled": True,
                    "librechat": {
                        "image": {
                            "tag": _STALE_LIBRECHAT_TAG,
                            "digest": _STALE_LIBRECHAT_DIGEST,
                        }
                    },
                    "bff": {
                        "image": {
                            "tag": _STALE_BFF_TAG,
                            "digest": _STALE_BFF_DIGEST,
                        }
                    },
                }
            }
        )
    )
    return path


def _render_librechat_and_bff_images(
    *, stale_values_file: Path, extra_set: list[str]
) -> dict[str, str]:
    """Full `helm template` render (not `--show-only` — console.enabled must
    be forced on for these scenarios) -> {component: rendered image ref}."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "vault.enabled=false",
        "--set",
        "console.enabled=true",
        "-f",
        str(stale_values_file),
        *_LINT_SECRETS,
        *extra_set,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- args ---\n{cmd}\n--- stderr ---\n{result.stderr}"
        )
    images: dict[str, str] = {}
    for doc in yaml.safe_load_all(result.stdout):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        name = doc.get("metadata", {}).get("name", "")
        containers = (
            doc.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if name.endswith("-librechat"):
            for c in containers:
                if c.get("name") == "librechat":
                    images["librechat"] = c["image"]
        elif name.endswith("-librechat-bff"):
            for c in containers:
                if c.get("name") == "bff":
                    images["bff"] = c["image"]
    if "librechat" not in images or "bff" not in images:
        raise AssertionError(f"expected both console Deployments; got {images!r}")
    return images


# ── (1) the CONTROL case: reproduce the live defect hermetically ───────────


def test_without_runner_console_sets_stale_value_shadows_chart(stale_values_file):
    """Pre-fix behaviour: with NO console `--set` args at all, the stale
    stored override wins over the chart's committed default — exactly the
    WU-6 Part C live symptom this spec closes."""
    images = _render_librechat_and_bff_images(
        stale_values_file=stale_values_file, extra_set=[]
    )
    assert _STALE_LIBRECHAT_TAG in images["librechat"]
    assert _STALE_LIBRECHAT_DIGEST in images["librechat"]
    assert _STALE_BFF_TAG in images["bff"]
    assert _STALE_BFF_DIGEST in images["bff"]

    committed = _console_image_pins(_real_chart_values())
    assert committed["librechat"]["digest"] not in images["librechat"]
    assert committed["bff"]["digest"] not in images["bff"]


# ── (2) the FIX: the runner's --set argv outranks the stale stored value ───


def test_runner_console_sets_override_stale_stored_value(stale_values_file):
    """The runner's own `console_image_set_args(...)` output (derived from
    the committed values.yaml) OUTRANKS the exact same stale stored
    override proven live in (1).

    Falsifiable: drop `*console_image_set_args(chart_values)` from
    `_helm_apply_cmd` (or hardcode `console_image_set_args` to return `[]`)
    and this test goes RED — the rendered image reverts to the stale
    tag/digest.
    """
    real_values = _real_chart_values()
    pins = _console_image_pins(real_values)
    # Build the args exactly the way console_image_set_args does, but force
    # enabled=True regardless of the chart's current on-disk toggle (the
    # toggle itself is exercised separately, below).
    forced_values = dict(real_values)
    forced_console = dict(real_values["console"])
    forced_console["enabled"] = True
    forced_values["console"] = forced_console
    extra_set = runner.console_image_set_args(forced_values)
    assert extra_set, "expected non-empty --set argv when console.enabled=True"

    images = _render_librechat_and_bff_images(
        stale_values_file=stale_values_file, extra_set=extra_set
    )

    assert pins["librechat"]["tag"] in images["librechat"]
    assert pins["librechat"]["digest"] in images["librechat"]
    assert pins["bff"]["tag"] in images["bff"]
    assert pins["bff"]["digest"] in images["bff"]

    # And the stale values are GONE — the whole point of the fix.
    assert _STALE_LIBRECHAT_TAG not in images["librechat"]
    assert _STALE_LIBRECHAT_DIGEST not in images["librechat"]
    assert _STALE_BFF_TAG not in images["bff"]
    assert _STALE_BFF_DIGEST not in images["bff"]


def test_helm_apply_cmd_end_to_end_outranks_stale_override(stale_values_file):
    """Same proof as above, but through the ACTUAL `_helm_apply_cmd` argv
    the runner emits (not a hand-rolled equivalent) — the full production
    seam, minus `--wait`/`--timeout` which don't affect rendering."""
    real_values = _real_chart_values()
    forced_console = dict(real_values["console"])
    forced_console["enabled"] = True
    forced_values = {**real_values, "console": forced_console}

    cfg = DeployConfig(target_version="v1.26.0")
    ref = registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server",
        "1.26.0",
        "sha256:" + "c" * 64,
        "hub",
    )
    argv = runner._helm_apply_cmd(cfg, ref, chart_values=forced_values)

    # Extract the --set pairs and replay them against `helm template`
    # (swap `upgrade --install <release> <chart> -n <ns> --reset-then-reuse-
    # values` for `template <release> <chart> -n <ns>` — the --set argv is
    # byte-identical either way, which is exactly what we're proving).
    extra_set: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--set":
            extra_set += ["--set", argv[i + 1]]
            i += 2
        else:
            i += 1

    images = _render_librechat_and_bff_images(
        stale_values_file=stale_values_file, extra_set=extra_set
    )
    pins = _console_image_pins(real_values)
    assert pins["librechat"]["digest"] in images["librechat"]
    assert pins["bff"]["digest"] in images["bff"]
    assert _STALE_LIBRECHAT_DIGEST not in images["librechat"]
    assert _STALE_BFF_DIGEST not in images["bff"]


# ── console.enabled=false path (spec §2) ────────────────────────────────────


def test_console_disabled_renders_no_console_deployments_and_no_spurious_sets():
    """`console.enabled=false` must still render/deploy cleanly: no console
    Deployment at all, and `console_image_set_args` emits nothing spurious
    for the runner to `--set` in the first place."""
    real_values = _real_chart_values()
    disabled_console = dict(real_values["console"])
    disabled_console["enabled"] = False
    disabled_values = {**real_values, "console": disabled_console}

    assert runner.console_image_set_args(disabled_values) == []

    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "vault.enabled=false",
        "--set",
        "console.enabled=false",
        *_LINT_SECRETS,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    names = {d.get("metadata", {}).get("name", "") for d in docs if d.get("kind")}
    assert not any(n.endswith("-librechat") for n in names)
    assert not any(n.endswith("-librechat-bff") for n in names)
