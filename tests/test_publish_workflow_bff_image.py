"""Drift-guard: the LibreChat BFF image MUST be published on every
release tag (D2-5B, `console-hardening-b-bff-in-publish`).

Background — the 2026-08-31 live E2E found the deployed BFF frozen at
``localhost:5000/audittrace/librechat-bff:wu3b``, an image that
predates the ``/memory`` proxy (#312, commit ``ad187dc``), because
``audittrace-librechat-bff`` was never added to
``.github/workflows/publish.yml``. The chart's release-versioned BFF
tag could therefore point at an image that was never published for
that version — a supply-chain gap, not a one-off.

This test pins the fix: a ``docker/build-push-action`` step targeting
``bff/Dockerfile`` with repo-root build context MUST exist in the
``publish`` job, tagged ``docker.io/lfds/audittrace-librechat-bff:
<version>`` (mirroring the ``<version>``/``latest`` tag convention the
sibling ``audittrace-memory-server`` and ``audittrace-llm-stub`` steps
already carry), and MUST NOT carry a weaker platform matrix or a
different build-push-action version than those siblings (no silently
dropped arch, no silently weakened provenance/attestation posture —
``docker/build-push-action`` generates build provenance attestations
by default from this action version onward, so "same action version"
is the mechanical proxy for "same provenance posture").

Falsifiability: every assertion in ``TestBffImagePublished`` fails if
the BFF step is removed, or is pointed at the wrong dockerfile/context,
or drops an arch, or diverges from the sibling steps' action version —
proven live during the build (neuter -> RED, restore -> GREEN;
see the build-record for the captured transcript).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_YML = REPO_ROOT / ".github" / "workflows" / "publish.yml"

_EXPECTED_PLATFORMS = "linux/amd64,linux/arm64"


def _load_publish_steps() -> list[dict[str, Any]]:
    """Parse publish.yml and return the ``publish`` job's step list."""
    workflow = yaml.safe_load(PUBLISH_YML.read_text())
    return workflow["jobs"]["publish"]["steps"]


def _find_build_push_step(
    steps: list[dict[str, Any]], *, file: str
) -> dict[str, Any] | None:
    """Find a ``docker/build-push-action`` step targeting ``file``."""
    for step in steps:
        if not str(step.get("uses", "")).startswith("docker/build-push-action@"):
            continue
        with_block = step.get("with", {}) or {}
        if with_block.get("file") == file:
            return step
    return None


class TestBffImagePublished:
    """The publish job MUST build + push the LibreChat BFF image."""

    def test_workflow_yaml_parses(self) -> None:
        # Rule 1 (verification) — the workflow YAML lints/parses.
        workflow = yaml.safe_load(PUBLISH_YML.read_text())
        assert workflow["jobs"]["publish"]["steps"], (
            "publish.yml did not parse into a non-empty step list"
        )

    def test_bff_build_push_step_exists(self) -> None:
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert bff_step is not None, (
            "no docker/build-push-action step targets bff/Dockerfile — "
            "the LibreChat BFF image is missing from publish.yml "
            "(the D2-5B regression)."
        )

    def test_bff_step_uses_repo_root_context(self) -> None:
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert bff_step is not None
        with_block = bff_step["with"]
        assert with_block.get("context") == ".", (
            "bff/Dockerfile step must build from the repo-root context "
            f"('.'), got {with_block.get('context')!r}. "
            "bff/Dockerfile's COPY instructions are repo-root-relative "
            "(COPY bff/requirements.txt, COPY bff/)."
        )

    def test_bff_step_tag_pattern_matches_convention(self) -> None:
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert bff_step is not None
        tags = bff_step["with"].get("tags", "")
        assert "docker.io/lfds/audittrace-librechat-bff:" in tags, (
            f"unexpected BFF image repository in tags: {tags!r}"
        )
        assert "${{ steps.ver.outputs.version }}" in tags, (
            "BFF tag must be pinned to the release version via "
            "steps.ver.outputs.version, matching the sibling images' "
            "tag pattern."
        )
        assert "docker.io/lfds/audittrace-librechat-bff:latest" in tags, (
            "BFF image must also carry the moving `:latest` alias, "
            "matching the audittrace-memory-server / audittrace-llm-stub "
            "convention."
        )

    def test_bff_step_platform_matrix_matches_siblings(self) -> None:
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        memory_server_step = _find_build_push_step(steps, file="Dockerfile")
        llm_stub_step = _find_build_push_step(steps, file="images/llm-stub/Dockerfile")
        assert bff_step is not None
        assert memory_server_step is not None
        assert llm_stub_step is not None

        bff_platforms = bff_step["with"].get("platforms")
        assert bff_platforms == _EXPECTED_PLATFORMS, (
            f"BFF step platforms {bff_platforms!r} != expected "
            f"{_EXPECTED_PLATFORMS!r} — no arch may be dropped relative "
            "to the sibling image jobs."
        )
        assert bff_platforms == memory_server_step["with"].get("platforms")
        assert bff_platforms == llm_stub_step["with"].get("platforms")

    def test_bff_step_action_version_matches_siblings(self) -> None:
        # "Same action version" is the mechanical proxy for "same
        # provenance/attestation posture" — docker/build-push-action
        # generates build provenance attestations by default; a step
        # pinned to an older/different action version could silently
        # regress that posture without changing anything else visible
        # in the diff.
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        memory_server_step = _find_build_push_step(steps, file="Dockerfile")
        llm_stub_step = _find_build_push_step(steps, file="images/llm-stub/Dockerfile")
        assert bff_step is not None
        assert memory_server_step is not None
        assert llm_stub_step is not None

        assert bff_step["uses"] == memory_server_step["uses"] == llm_stub_step["uses"]

    def test_bff_step_pushes(self) -> None:
        steps = _load_publish_steps()
        bff_step = _find_build_push_step(steps, file="bff/Dockerfile")
        assert bff_step is not None
        assert bff_step["with"].get("push") is True, (
            "BFF build step must set push: true — a build-only step "
            "would satisfy the file/context assertions above without "
            "actually publishing the image (vacuous pass)."
        )

    def test_publish_job_has_single_shared_registry_login(self) -> None:
        # Registry auth (docker/login-action) is job-level, shared by
        # every docker/build-push-action step in the job — including
        # the new BFF step. Confirm exactly one login step exists (no
        # weaker/duplicated auth path was introduced for the BFF step)
        # and that it precedes the BFF build step.
        steps = _load_publish_steps()
        login_indices = [
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/login-action@")
        ]
        bff_index = next(
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
            and (step.get("with", {}) or {}).get("file") == "bff/Dockerfile"
        )
        assert len(login_indices) == 1, (
            f"expected exactly one docker/login-action step, found "
            f"{len(login_indices)} — the BFF step must reuse the "
            "existing job-level registry auth, not introduce a new one."
        )
        assert login_indices[0] < bff_index, (
            "docker/login-action must run before the BFF build/push step."
        )
