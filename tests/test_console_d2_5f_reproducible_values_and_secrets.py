"""M3-WU-D2-5F — reproducible laptop values + secret-safety + kill the
last ``localhost:5000`` (spec `console-hardening-f-reproducible-values-
and-secrets`, private repo, amended 2026-09-02 to fold in the last
``localhost:5000`` cleanup surfaced by the D2-5C review).

Three drift classes, each with a falsifiable (neuter -> RED) guard:

* **Values-laptop reproducibility.** ``values-laptop.yaml`` now carries
  ``console.enabled: true`` (folded in from the rev-262/263 CD agent's
  hand ``--set`` override) so ``helm upgrade -f values-laptop.yaml``
  alone reproduces the console deploy — no remembered flags.
* **Zero ``localhost:5000`` anywhere in the rendered chart.** Includes
  the ``helm test`` hook (``templates/tests/test-rls.yaml``), the last
  place a local-registry, mutable-``:latest`` image ref survived. The
  render greps the FULL output (every workload + the hook), not just
  the templates this WU directly edited — a regression anywhere else in
  the chart would still be caught.
* **The ``audittrace-tests`` publish job exists and mirrors its
  siblings** — the same falsifiable pattern
  ``tests/test_publish_workflow_bff_image.py`` (D2-5B) established for
  the BFF image, applied to the tests image (D2-5F).

The BFF exchange-client-secret preserve guard itself (item 2 of the
spec) lives in ``tests/test_console_wu3c_testable_deploy.py::
TestBffSecretPersistence`` — co-located with the sibling D3
session-secret persistence tests it deliberately mirrors, since both
exercise the SAME ``audittrace.console.persistedSecret`` helper.

Anchors: ``feedback_no_more_drifts``, ``feedback_vacuous_neuter_test_antipattern``,
``feedback_ratified_spec_immutable``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
VALUES_FILE = CHART_DIR / "values.yaml"
VALUES_LAPTOP_FILE = CHART_DIR / "values-laptop.yaml"
TEST_RLS_TEMPLATE = CHART_DIR / "templates" / "tests" / "test-rls.yaml"
PUBLISH_YML = REPO_ROOT / ".github" / "workflows" / "publish.yml"

RELEASE = "audittrace"
NAMESPACE = "audittrace"

_LINT_SECRETS: list[str] = []
for kv in (
    "secrets.minio.secretKey=preflight",
    "secrets.minio.kmsKey=preflight",
    "secrets.chromadb.token=preflight",
    "secrets.keycloak.adminPassword=preflight",
    "secrets.postgres.appPassword=preflight",
    "secrets.postgres.password=preflight",
    "secrets.redis.password=preflight",
    "secrets.summariser.password=preflight",
):
    _LINT_SECRETS.extend(["--set", kv])

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — chart-drift tests need it",
)

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _render_full(
    *,
    vault_enabled: bool = True,
    tests_enabled: bool = True,
    console_enabled: bool = True,
    values_files: list[Path] | None = None,
) -> str:
    """Render the ENTIRE chart (every workload + every helm hook) to raw
    YAML text — a full-text grep is the only way to catch a
    `localhost:5000` regression anywhere in the chart, not just in the
    templates this WU directly touched."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        f"vault.enabled={'true' if vault_enabled else 'false'}",
        "--set",
        f"tests.enabled={'true' if tests_enabled else 'false'}",
        "--set",
        f"console.enabled={'true' if console_enabled else 'false'}",
        *_LINT_SECRETS,
    ]
    for vf in values_files or []:
        cmd.extend(["-f", str(vf)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"helm template failed (rc={result.returncode}):\n"
        f"--- args ---\n{cmd}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _find(docs: list[dict], kind: str, name_suffix: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name", "").endswith(
            name_suffix
        ):
            return d
    raise AssertionError(f"no {kind} ending in {name_suffix!r} in rendered docs")


def _parse(rendered: str) -> list[dict]:
    return [
        d for d in yaml.safe_load_all(rendered) if isinstance(d, dict) and d.get("kind")
    ]


# ---------------------------------------------------------------------------
# Values-laptop reproducibility — ONE `helm upgrade -f values-laptop.yaml`
# ---------------------------------------------------------------------------


class TestValuesLaptopReproducible:
    def test_console_enabled_true_folded_in(self) -> None:
        """The rev-262/263 CD agent had to remember a hand
        `--set console.enabled=true` on top of `-f values-laptop.yaml` —
        folded in here so it never has to be remembered again. Neuter
        (drop this key, or set it false) -> this test goes RED."""
        laptop_values = yaml.safe_load(VALUES_LAPTOP_FILE.read_text())
        assert laptop_values["console"]["enabled"] is True, (
            "values-laptop.yaml no longer carries console.enabled: true — "
            "a plain `helm upgrade -f values-laptop.yaml` would silently "
            "leave the console undeployed, the exact CD-agent friction "
            "this WU exists to remove."
        )

    def test_values_yamls_own_default_stays_opt_in(self) -> None:
        """values.yaml's OWN default must stay console.enabled: false — a
        bare memory-server-only install must not suddenly grow a console.
        Only the laptop reference profile opts in."""
        values = yaml.safe_load(VALUES_FILE.read_text())
        assert values["console"]["enabled"] is False

    def test_helm_upgrade_style_render_with_laptop_profile_needs_no_extra_set(
        self,
    ) -> None:
        """The reproducibility claim itself: `-f values-laptop.yaml` with
        NO extra `--set console.enabled=...` renders the console
        workloads (BFF + LibreChat Deployments), proving the folded-in
        value is what actually drives the render, not an artefact of a
        `--set` this test forgot to also pass."""
        cmd = [
            "helm",
            "template",
            RELEASE,
            str(CHART_DIR),
            "-n",
            NAMESPACE,
            "-f",
            str(VALUES_LAPTOP_FILE),
            *_LINT_SECRETS,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        docs = _parse(result.stdout)
        # Console workloads render (proves console.enabled=true actually
        # took effect from the values file alone).
        _find(docs, "Deployment", "-librechat")
        _find(docs, "Deployment", "-librechat-bff")


# ---------------------------------------------------------------------------
# Zero localhost:5000 anywhere in the rendered chart
# ---------------------------------------------------------------------------


class TestZeroLocalhost5000InRenderedChart:
    def test_no_localhost_5000_anywhere_in_full_render(self) -> None:
        """Greps the FULL rendered chart — every Deployment/StatefulSet/
        Job/helm-test-hook Pod, not only the templates this WU directly
        edited. Neuter (reintroduce the local pin on tests.image, or any
        other image) -> RED."""
        rendered = _render_full(vault_enabled=True, tests_enabled=True)
        assert "localhost:5000" not in rendered, (
            "the fully rendered chart (all workloads incl. the helm test "
            "hook) still contains a `localhost:5000` reference — the last "
            "non-reproducible local-registry image pin was supposed to be "
            "eliminated by this WU."
        )

    def test_tests_image_renders_as_repository_tag_digest(self) -> None:
        rendered = _render_full(vault_enabled=True, tests_enabled=True)
        docs = _parse(rendered)
        hook_pod = _find(docs, "Pod", "-test-rls")
        containers = hook_pod["spec"]["containers"]
        tests_container = next(c for c in containers if c["name"] == "tests")
        image = tests_container["image"]

        assert ":latest" not in image, (
            f"tests image {image!r} carries a mutable :latest tag — the "
            "helm-test hook image must be pinned to an immutable version."
        )
        assert not image.startswith("localhost:5000"), image

        repo, _, tag_and_digest = image.partition(":")
        assert repo == "docker.io/lfds/audittrace-tests", (
            f"tests image repository {repo!r} — expected the published "
            "Docker Hub repository, not a local-registry stand-in."
        )
        tag, sep, digest = tag_and_digest.partition("@")
        assert sep == "@" and digest, (
            f"rendered tests image {image!r} carries no digest — expected "
            "`repository:tag@sha256:...` (mirrors the console.bff.image / "
            "console.librechat.image digest-pin render form)."
        )
        assert _SHA256_DIGEST_RE.match(digest), (
            f"tests image digest {digest!r} is not a well-formed sha256:<64-hex> value."
        )
        assert tag, "tests image tag is empty"

    def test_tests_image_digest_field_well_formed_in_values(self) -> None:
        """Rule-2/3 DEFERRED: `docker.io/lfds/audittrace-tests` has never
        been published (the publish.yml job ships in this same commit,
        first fires on the next tag push) — so the digest here is a
        clearly-marked PLACEHOLDER, never a fabricated "verified" value.
        This guard only checks the SHAPE stays well-formed (so the render
        keeps producing `repository:tag@sha256:...`), not that the digest
        is real — that verification is explicitly out of this Rule-1-only
        build's scope."""
        values = yaml.safe_load(VALUES_FILE.read_text())
        digest = values["tests"]["image"]["digest"]
        assert digest, "tests.image.digest is empty"
        assert _SHA256_DIGEST_RE.match(digest), (
            f"tests.image.digest={digest!r} is not a well-formed "
            "sha256:<64-hex> digest."
        )

    def test_tests_image_repository_is_not_local_registry(self) -> None:
        values = yaml.safe_load(VALUES_FILE.read_text())
        repository = values["tests"]["image"]["repository"]
        assert repository == "docker.io/lfds/audittrace-tests", (
            f"tests.image.repository={repository!r} — expected the "
            "published Docker Hub repository."
        )
        assert not repository.startswith("localhost:5000")

    def test_render_form_uses_digest_conditional(self) -> None:
        """Structural non-vacuity guard: the template source must
        actually branch on `.Values.tests.image.digest` (mirrors
        deployment-bff.yaml's `{{ if ... .digest }}@{{ ... }}{{ end }}`
        form) — a hardcoded `@sha256:...` string in the template would
        pass the render-shape assertions above by accident, without
        actually reading the value the operator would override on a real
        digest bump."""
        src = TEST_RLS_TEMPLATE.read_text()
        assert "{{ if .Values.tests.image.digest }}" in src, (
            "test-rls.yaml does not conditionally render the tests image "
            "digest from .Values.tests.image.digest — a hardcoded digest "
            "would defeat future digest bumps."
        )

    def test_localhost_5000_disappears_only_because_of_the_digest_pin(
        self,
    ) -> None:
        """Falsifiability: reverting ONLY tests.image.repository/tag back
        to the pre-WU local-registry pin (leaving everything else intact)
        must reintroduce localhost:5000 in the full render — proves the
        zero-localhost:5000 assertion above is actually exercising this
        WU's change, not passing vacuously for an unrelated reason."""
        cmd = [
            "helm",
            "template",
            RELEASE,
            str(CHART_DIR),
            "-n",
            NAMESPACE,
            "--set",
            "vault.enabled=true",
            "--set",
            "tests.enabled=true",
            "--set",
            "console.enabled=true",
            "--set",
            "tests.image.repository=localhost:5000/audittrace/tests",
            "--set",
            "tests.image.tag=latest",
            "--set",
            "tests.image.digest=",
            *_LINT_SECRETS,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "localhost:5000" in result.stdout, (
            "reverting tests.image to the pre-WU local-registry pin did "
            "NOT reintroduce localhost:5000 in the render — the "
            "zero-localhost:5000 guard above would be vacuous."
        )


# ---------------------------------------------------------------------------
# publish.yml — the audittrace-tests build+push job mirrors its siblings
# ---------------------------------------------------------------------------


def _load_publish_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(PUBLISH_YML.read_text())
    return workflow["jobs"]["publish"]["steps"]


def _find_build_push_step(
    steps: list[dict[str, Any]], *, file: str
) -> dict[str, Any] | None:
    for step in steps:
        if not str(step.get("uses", "")).startswith("docker/build-push-action@"):
            continue
        with_block = step.get("with", {}) or {}
        if with_block.get("file") == file:
            return step
    return None


class TestAuditTraceTestsImagePublished:
    """M3-WU-D2-5F — mirrors tests/test_publish_workflow_bff_image.py
    (D2-5B) exactly, applied to the `audittrace-tests` helm-test-hook
    image. Real YAML parse of publish.yml, not string-contains."""

    def test_workflow_yaml_parses(self) -> None:
        workflow = yaml.safe_load(PUBLISH_YML.read_text())
        assert workflow["jobs"]["publish"]["steps"]

    def test_tests_build_push_step_exists(self) -> None:
        steps = _load_publish_steps()
        step = _find_build_push_step(steps, file="Dockerfile.tests")
        assert step is not None, (
            "no docker/build-push-action step targets Dockerfile.tests — "
            "the audittrace-tests image is missing from publish.yml."
        )

    def test_tests_step_uses_repo_root_context(self) -> None:
        steps = _load_publish_steps()
        step = _find_build_push_step(steps, file="Dockerfile.tests")
        assert step is not None
        assert step["with"].get("context") == ".", (
            "Dockerfile.tests step must build from the repo-root context "
            f"('.'), got {step['with'].get('context')!r} — "
            "Dockerfile.tests's COPY instructions (tests/, pyproject.toml) "
            "are repo-root-relative, mirroring the main image + BFF steps."
        )

    def test_tests_step_tag_pattern_matches_convention(self) -> None:
        steps = _load_publish_steps()
        step = _find_build_push_step(steps, file="Dockerfile.tests")
        assert step is not None
        tags = step["with"].get("tags", "")
        assert "docker.io/lfds/audittrace-tests:" in tags
        assert "${{ steps.ver.outputs.version }}" in tags, (
            "audittrace-tests tag must be pinned to the release version "
            "via steps.ver.outputs.version, matching the sibling images."
        )
        assert "docker.io/lfds/audittrace-tests:latest" in tags

    def test_tests_step_platform_matrix_matches_siblings(self) -> None:
        steps = _load_publish_steps()
        tests_step = _find_build_push_step(steps, file="Dockerfile.tests")
        memory_server_step = _find_build_push_step(steps, file="Dockerfile")
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert tests_step is not None
        assert memory_server_step is not None
        assert bff_step is not None

        expected = "linux/amd64,linux/arm64"
        tests_platforms = tests_step["with"].get("platforms")
        assert tests_platforms == expected, (
            f"audittrace-tests step platforms {tests_platforms!r} != "
            f"expected {expected!r} — no arch may be dropped relative to "
            "the sibling image jobs."
        )
        assert tests_platforms == memory_server_step["with"].get("platforms")
        assert tests_platforms == bff_step["with"].get("platforms")

    def test_tests_step_action_version_matches_siblings(self) -> None:
        steps = _load_publish_steps()
        tests_step = _find_build_push_step(steps, file="Dockerfile.tests")
        memory_server_step = _find_build_push_step(steps, file="Dockerfile")
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert tests_step is not None
        assert memory_server_step is not None
        assert bff_step is not None
        assert tests_step["uses"] == memory_server_step["uses"] == bff_step["uses"]

    def test_tests_step_pushes(self) -> None:
        steps = _load_publish_steps()
        step = _find_build_push_step(steps, file="Dockerfile.tests")
        assert step is not None
        assert step["with"].get("push") is True, (
            "audittrace-tests build step must set push: true — a "
            "build-only step would satisfy the file/context assertions "
            "above without actually publishing the image (vacuous pass)."
        )

    def test_tests_step_pins_base_image_to_the_just_published_runtime(
        self,
    ) -> None:
        """Dockerfile.tests's own docstring says CI/release pipelines
        should override TESTS_BASE_IMAGE to "the immutable published
        runtime tag they want to validate" — confirm the publish job
        actually does this, pointed at the SAME version's
        audittrace-memory-server image it just built above (never a
        locally-built stand-in, never a different/stale version)."""
        steps = _load_publish_steps()
        step = _find_build_push_step(steps, file="Dockerfile.tests")
        assert step is not None
        build_args = step["with"].get("build-args", "") or ""
        assert "TESTS_BASE_IMAGE=" in build_args
        assert "docker.io/lfds/audittrace-memory-server:" in build_args
        assert "${{ steps.ver.outputs.version }}" in build_args

    def test_publish_job_reuses_the_single_shared_registry_login(self) -> None:
        steps = _load_publish_steps()
        login_indices = [
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/login-action@")
        ]
        tests_index = next(
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
            and (step.get("with", {}) or {}).get("file") == "Dockerfile.tests"
        )
        assert len(login_indices) == 1, (
            f"expected exactly one docker/login-action step, found "
            f"{len(login_indices)} — the audittrace-tests step must reuse "
            "the existing job-level registry auth."
        )
        assert login_indices[0] < tests_index

    def test_tests_step_runs_after_memory_server_image_is_pushed(self) -> None:
        """The base-image build-arg only resolves once the memory-server
        image has actually been pushed — confirm step ORDER, not just
        step PRESENCE (a race here would intermittently fail the publish
        job, or worse, silently pull a stale cached base)."""
        steps = _load_publish_steps()
        memory_server_index = next(
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
            and (step.get("with", {}) or {}).get("file") == "Dockerfile"
        )
        tests_index = next(
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
            and (step.get("with", {}) or {}).get("file") == "Dockerfile.tests"
        )
        assert memory_server_index < tests_index
