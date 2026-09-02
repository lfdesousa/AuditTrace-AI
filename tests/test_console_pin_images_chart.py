"""M3-WU-D2-5C — pin (and digest) the console images; incorporate the D2-4
fork pin (`specs/2026-09-01-SPEC-console-hardening-c-pin-console-images.md`,
private repo).

Background — the chart pinned the BFF at
``localhost:5000/audittrace/librechat-bff`` (tag default, ``wu3b`` in the
live release): a local-registry, digest-less pin that is neither portable
nor reproducible, and is exactly what let a stale image run in production
(the D2-5B incident). Unlike the memory-server / (D2-4) librechat image
refs, ``deployment-bff.yaml``'s image was ``repository:tag`` only — no
digest field, no ``@sha256:...`` suffix ever possible.

This module guards the fix (spec Acceptance Rule 1, a-d):

(a) BOTH console images (BFF, LibreChat) render as
    ``repository:tag@sha256:<64-hex>`` — ``TestBothImagesRenderWithDigest``.
(b) the digest-field plumbing exists in the BFF template (the
    ``{{ if .digest }}@{{ .digest }}{{ end }}`` conditional, mirroring the
    memory-server / librechat digest-pin pattern) — proven by NEUTER/RESTORE:
    clearing ``console.bff.image.digest`` must drop the ``@sha256:...``
    suffix (never crash, never silently keep a stale digest) —
    ``TestBffDigestPlumbing``.
(c) NO ``localhost:5000`` / ``:wu3b`` string survives in either console
    image ref — ``TestNoLocalRegistryOrStaleTagSurvives``. Proven
    non-vacuous by NEUTER (reintroduce
    ``console.bff.image.repository=localhost:5000/audittrace/librechat-bff``
    via ``--set`` and confirm the guard itself would catch it) /
    RESTORE. Scoped to the two CONSOLE image refs this spec actually
    touches (BFF + LibreChat Deployments) — the pre-existing, unrelated
    ``tests.image.repository=localhost:5000/audittrace/tests`` (the
    ``helm test`` RLS-hook image) is out of this spec's scope
    (``console.bff.image`` / D2-4 only) and is asserted UNCHANGED by
    ``test_unrelated_tests_hook_image_pin_is_untouched`` rather than
    silently swept into the same guard.
(d) the D2-4 fail-closed memory-backend contract is preserved: a
    non-``sovereign`` ``AUDITTRACE_MEMORY_BACKEND`` still renders
    VERBATIM (the fork's own ``config.js`` is what fails closed to Mongo
    — the chart's job is only to never coerce the value) —
    ``TestMemoryBackendFailsClosedVerbatim``.

Also verifies the D2-4 sovereign env (memory backend / BFF base url /
timeout) and that E's (coredns/frontDoorNodeName, no hostAliases) and H's
(wait-for-oidc-discovery initContainer) chart state coexist unchanged
alongside this WU's image-pin + D2-4 changes — ``TestD2FourAndEAndHCoexist``.

Both real digests (BFF ``sha256:286fe5b6...``, fork
``sha256:2d9590a5...``) were verified against Docker Hub's
``docker-content-digest`` header at build time (read-only manifest GET,
no push) — see the build-record for the captured transcript. This module
does not re-verify against the live registry (no network dependency in
the test suite); it guards that the chart renders the pinned values
faithfully.

Anchors: ``feedback_no_more_drifts``, ``feedback_vacuous_neuter_test_antipattern``,
``feedback_ratified_spec_immutable``, ``feedback_no_static_host_ip_pin_resolve_by_name``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
VALUES_DEFAULT = CHART_DIR / "values.yaml"
VALUES_LAPTOP = CHART_DIR / "values-laptop.yaml"
DEPLOYMENT_BFF_TEMPLATE = CHART_DIR / "templates" / "console" / "deployment-bff.yaml"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Mirrors tests/test_frontdoor_dns_chart.py / test_librechat_oidc_startup_gate_chart.py's
# base set — the throwaway secrets + FQDNs a full console.enabled=true
# render needs so it doesn't abort on an unrelated `required` guard
# (ADR-045).
_BASE_SET: list[str] = []
for kv in (
    "secrets.minio.secretKey=ci-test",
    "secrets.minio.kmsKey=ci-test",
    "secrets.chromadb.token=ci-test",
    "secrets.keycloak.adminPassword=ci-test",
    "secrets.postgres.appPassword=ci-test",
    "secrets.postgres.password=ci-test",
    "secrets.redis.password=ci-test",
    "secrets.summariser.password=ci-test",
    "secrets.console.bffExchangeClientSecret=ci-test",
    "externalLLM.host=llm.test.invalid",
    "observability.external.langfuseHost=langfuse.test.invalid",
    "observability.external.tempoHost=tempo.test.invalid",
    "observability.external.lokiHost=loki.test.invalid",
):
    _BASE_SET.extend(["--set", kv])

_CONSOLE_ENABLED = [
    "--set",
    "console.enabled=true",
    "--set",
    "console.frontDoorNodeName=test-node",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — chart render tests need it",
)


def _render(extra_sets: list[str], values_file: Path | None = None) -> list[dict]:
    """Render the chart, returning parsed YAML docs (comments stripped)."""
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "--namespace",
        "audittrace",
        *_BASE_SET,
        *extra_sets,
    ]
    if values_file is not None:
        cmd.extend(["-f", str(values_file)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find(resources: list[dict], kind: str, name: str) -> dict:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    raise AssertionError(f"{kind}/{name} not found in rendered chart")


def _container(dep: dict, name: str) -> dict:
    containers = dep["spec"]["template"]["spec"].get("containers") or []
    match = next((c for c in containers if c.get("name") == name), None)
    assert match is not None, f"container {name!r} not found; got {containers}"
    return match


def _env_map(container: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def _bff_image(resources: list[dict]) -> str:
    dep = _find(resources, "Deployment", "audittrace-librechat-bff")
    return _container(dep, "bff")["image"]


def _librechat_image(resources: list[dict]) -> str:
    dep = _find(resources, "Deployment", "audittrace-librechat")
    return _container(dep, "librechat")["image"]


class TestBothImagesRenderWithDigest:
    """(a) — BOTH console images render as repository:tag@sha256:<64-hex>."""

    def test_bff_image_is_repo_tag_digest(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        image = _bff_image(resources)
        assert image.startswith("docker.io/lfds/audittrace-librechat-bff:"), (
            f"unexpected BFF repository in rendered image ref: {image!r}"
        )
        repo_tag, _, digest = image.partition("@")
        assert digest, f"BFF image {image!r} carries no @digest suffix"
        assert _SHA256_RE.match(digest), f"malformed digest {digest!r}"
        assert repo_tag.endswith(":1.25.1"), f"unexpected BFF tag: {repo_tag!r}"

    def test_bff_digest_matches_pinned_values_digest(self) -> None:
        values = yaml.safe_load(VALUES_DEFAULT.read_text(encoding="utf-8"))
        pinned = values["console"]["bff"]["image"]["digest"]
        assert pinned == (
            "sha256:286fe5b699a9b7248e72990a063d353ce6692f7a37e823177e324f5a42bab488"
        ), f"BFF digest in values.yaml drifted from the verified pin: {pinned!r}"
        resources = _render(_CONSOLE_ENABLED)
        assert _bff_image(resources).endswith(f"@{pinned}")

    def test_librechat_image_is_repo_tag_digest(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        image = _librechat_image(resources)
        assert image.startswith("docker.io/lfds/audittrace-librechat:"), (
            f"unexpected LibreChat repository in rendered image ref: {image!r} "
            "— the D2-4 fork pin must be incorporated"
        )
        repo_tag, _, digest = image.partition("@")
        assert digest, f"LibreChat image {image!r} carries no @digest suffix"
        assert _SHA256_RE.match(digest), f"malformed digest {digest!r}"
        assert repo_tag.endswith(":0f08e22"), f"unexpected LibreChat tag: {repo_tag!r}"

    def test_librechat_digest_matches_pinned_values_digest(self) -> None:
        values = yaml.safe_load(VALUES_DEFAULT.read_text(encoding="utf-8"))
        pinned = values["console"]["librechat"]["image"]["digest"]
        assert pinned == (
            "sha256:2d9590a567bd256d7be4fa16bac238f18427ac7f795666bddd472e42c8ff1474"
        ), f"LibreChat digest in values.yaml drifted from the D2-4 pin: {pinned!r}"
        resources = _render(_CONSOLE_ENABLED)
        assert _librechat_image(resources).endswith(f"@{pinned}")

    def test_bff_and_librechat_digests_are_distinct(self) -> None:
        """Sanity: the two pins must not accidentally collide (would mask
        a copy-paste error where one image ref got the other's digest)."""
        resources = _render(_CONSOLE_ENABLED)
        bff_digest = _bff_image(resources).split("@", 1)[1]
        librechat_digest = _librechat_image(resources).split("@", 1)[1]
        assert bff_digest != librechat_digest


class TestBffDigestPlumbing:
    """(b) — the digest-field plumbing in deployment-bff.yaml is real, not
    decorative. NEUTER: clear console.bff.image.digest -> the @sha256
    suffix must DISAPPEAR (never keep rendering a stale digest, never
    crash the render)."""

    def test_template_source_carries_the_conditional_digest_form(self) -> None:
        source = DEPLOYMENT_BFF_TEMPLATE.read_text(encoding="utf-8")
        assert "console.bff.image.digest" in source, (
            "deployment-bff.yaml's image ref does not reference "
            "console.bff.image.digest at all — the digest field is not "
            "wired to the render"
        )
        assert re.search(
            r"\{\{-?\s*if\s+\.Values\.console\.bff\.image\.digest\s*-?\}\}"
            r"@\{\{\s*\.Values\.console\.bff\.image\.digest\s*\}\}"
            r"\{\{-?\s*end\s*-?\}\}",
            source,
        ), (
            "expected the `{{ if .digest }}@{{ .digest }}{{ end }}` "
            "conditional form (mirroring the memory-server / librechat "
            "digest-pin pattern) — got something else"
        )

    def test_neuter_clearing_digest_drops_the_suffix(self) -> None:
        resources = _render([*_CONSOLE_ENABLED, "--set", "console.bff.image.digest="])
        image = _bff_image(resources)
        assert "@" not in image, (
            f"clearing console.bff.image.digest should drop the @digest "
            f"suffix entirely, got {image!r} — the conditional either "
            "isn't guarding the digest, or a stale value leaked through"
        )
        assert image == "docker.io/lfds/audittrace-librechat-bff:1.25.1"

    def test_restore_setting_digest_brings_the_suffix_back(self) -> None:
        custom_digest = "sha256:" + "ab" * 32
        resources = _render(
            [
                *_CONSOLE_ENABLED,
                "--set",
                f"console.bff.image.digest={custom_digest}",
            ]
        )
        image = _bff_image(resources)
        assert image.endswith(f"@{custom_digest}"), (
            f"setting console.bff.image.digest should flow straight "
            f"through to the rendered image ref, got {image!r}"
        )


class TestNoLocalRegistryOrStaleTagSurvives:
    """(c) — no localhost:5000 / :wu3b string survives in either console
    image ref, scoped to the two Deployments this spec pins (BFF +
    LibreChat). NEUTER: reintroducing the old
    localhost:5000/audittrace/librechat-bff pin via --set makes this
    guard's own assertion fail, proving it is load-bearing, not vacuous."""

    def test_bff_image_carries_no_local_registry_or_stale_tag(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        image = _bff_image(resources)
        assert "localhost:5000" not in image, f"local-registry pin survived: {image!r}"
        assert "wu3b" not in image, f"stale wu3b tag survived: {image!r}"

    def test_librechat_image_carries_no_local_registry_or_stale_tag(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        image = _librechat_image(resources)
        assert "localhost:5000" not in image, f"local-registry pin survived: {image!r}"
        assert "wu3b" not in image, f"stale wu3b tag survived: {image!r}"

    def test_values_yaml_default_bff_pin_is_no_longer_local_registry(self) -> None:
        values = yaml.safe_load(VALUES_DEFAULT.read_text(encoding="utf-8"))
        repo = values["console"]["bff"]["image"]["repository"]
        assert repo == "docker.io/lfds/audittrace-librechat-bff", (
            f"console.bff.image.repository is still {repo!r} — the "
            "localhost:5000 default was not replaced"
        )

    def test_neuter_reintroducing_local_registry_pin_would_be_caught(self) -> None:
        """Proves the guard above is non-vacuous: simulate the OLD,
        broken pin via --set and confirm the exact same predicate the
        other tests in this class assert on the real default (no
        `localhost:5000`, no `wu3b`) is FALSE against the neutered
        render — i.e. this class would have caught the original bug."""
        resources = _render(
            [
                *_CONSOLE_ENABLED,
                "--set",
                "console.bff.image.repository=localhost:5000/audittrace/librechat-bff",
                "--set",
                "console.bff.image.tag=wu3b",
                "--set",
                "console.bff.image.digest=",
            ]
        )
        image = _bff_image(resources)
        guard_holds = "localhost:5000" not in image and "wu3b" not in image
        assert not guard_holds, (
            f"expected the neutered (old-pin) render {image!r} to trip "
            "the no-local-registry/no-stale-tag guard, but the guard's "
            "predicate held true anyway — the guard is vacuous"
        )

    def test_unrelated_tests_hook_image_pin_is_untouched(self) -> None:
        """The pre-existing `tests.image.repository` (the `helm test`
        RLS-hook image, templates/tests/test-rls.yaml) is a DIFFERENT,
        unrelated component this spec never touches (out of scope:
        console.bff.image / D2-4 only). Asserted explicitly UNCHANGED
        here so this WU's fix doesn't accidentally mask or silently
        sweep it up."""
        values = yaml.safe_load(VALUES_DEFAULT.read_text(encoding="utf-8"))
        assert (
            values["tests"]["image"]["repository"] == "localhost:5000/audittrace/tests"
        )


class TestMemoryBackendFailsClosedVerbatim:
    """(d) — the D2-4 fail-closed memory-backend contract survives this
    WU's image-pin work unchanged: a non-"sovereign" value renders
    VERBATIM (never coerced), so the fork's own config.js is what falls
    back to Mongo, not the chart."""

    def test_default_memory_backend_is_sovereign(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        dep = _find(resources, "Deployment", "audittrace-librechat")
        env = _env_map(_container(dep, "librechat"))
        assert env.get("AUDITTRACE_MEMORY_BACKEND") == "sovereign"

    def test_non_sovereign_value_renders_verbatim_not_coerced(self) -> None:
        for value in ("mongo", "", "Sovereign", "typo-value"):
            resources = _render(
                [
                    *_CONSOLE_ENABLED,
                    "--set",
                    f"console.librechat.memoryBackend={value}",
                ]
            )
            dep = _find(resources, "Deployment", "audittrace-librechat")
            env = _env_map(_container(dep, "librechat"))
            assert env.get("AUDITTRACE_MEMORY_BACKEND") == value, (
                f"console.librechat.memoryBackend={value!r} did not "
                f"render verbatim (got {env.get('AUDITTRACE_MEMORY_BACKEND')!r}) "
                "— the chart must never coerce this value; only the "
                "fork's own config.js decides the fail-closed fallback"
            )

    def test_bff_base_url_and_timeout_present(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        dep = _find(resources, "Deployment", "audittrace-librechat")
        env = _env_map(_container(dep, "librechat"))
        assert env.get("AUDITTRACE_BFF_BASE_URL") == (
            "http://audittrace-librechat-bff:8766"
        )
        assert env.get("AUDITTRACE_BFF_TIMEOUT_MS") == "15000"


class TestD2FourAndEAndHCoexist:
    """E's DNS overlay work, H's OIDC startup gate, and D2-4's fork pin +
    sovereign env must ALL coexist in the final chart state — none was
    silently dropped while incorporating D2-4 on top of current main."""

    def test_no_static_host_aliases_e_invariant_intact(self) -> None:
        """E removed the static hostAliases pin (2026-09-01 DHCP
        incident) — must stay removed."""
        resources = _render(_CONSOLE_ENABLED)
        dep = _find(resources, "Deployment", "audittrace-librechat")
        pod_spec = dep["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec, (
            "a static hostAliases entry reappeared on the librechat pod "
            "— this is exactly the 2026-09-01 DHCP hard-down root cause "
            "E fixed; must stay gone"
        )

    def test_wait_for_oidc_discovery_init_container_h_invariant_intact(
        self,
    ) -> None:
        """H's initContainer must still be present and still reuse the
        (now fork-pinned) image — proves H's fix and this WU's D2-4
        image-repo change compose cleanly."""
        resources = _render(_CONSOLE_ENABLED)
        dep = _find(resources, "Deployment", "audittrace-librechat")
        pod_spec = dep["spec"]["template"]["spec"]
        init_containers = pod_spec.get("initContainers") or []
        gate = next(
            (c for c in init_containers if c.get("name") == "wait-for-oidc-discovery"),
            None,
        )
        assert gate is not None, (
            "wait-for-oidc-discovery initContainer (H) is missing after "
            "incorporating D2-4 — H's fix must not have been dropped"
        )
        main = _container(dep, "librechat")
        assert (
            gate["image"]
            == main["image"]
            == (
                "docker.io/lfds/audittrace-librechat:0f08e22"
                "@sha256:2d9590a567bd256d7be4fa16bac238f18427ac7f795666bddd472e42c8ff1474"
            )
        )

    def test_d2_4_sovereign_env_present_alongside_e_and_h(self) -> None:
        resources = _render(_CONSOLE_ENABLED)
        dep = _find(resources, "Deployment", "audittrace-librechat")
        env = _env_map(_container(dep, "librechat"))
        assert env.get("AUDITTRACE_MEMORY_BACKEND") == "sovereign"
        assert "AUDITTRACE_BFF_BASE_URL" in env
        assert "AUDITTRACE_BFF_TIMEOUT_MS" in env

    def test_laptop_profile_renders_all_three_together(self) -> None:
        """End-to-end: the ACTUAL committed values-laptop.yaml (the file
        a real `helm upgrade -f values-laptop.yaml ...` reads) renders
        E + H + D2-4 + this WU's image pins together with no conflict."""
        resources = _render(
            ["--set", "console.enabled=true"], values_file=VALUES_LAPTOP
        )
        dep = _find(resources, "Deployment", "audittrace-librechat")
        pod_spec = dep["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec
        init_containers = pod_spec.get("initContainers") or []
        assert any(c.get("name") == "wait-for-oidc-discovery" for c in init_containers)
        env = _env_map(_container(dep, "librechat"))
        assert env.get("AUDITTRACE_MEMORY_BACKEND") == "sovereign"
        image = _container(dep, "librechat")["image"]
        assert image.startswith("docker.io/lfds/audittrace-librechat:")
        bff_image = _bff_image(resources)
        assert bff_image.startswith("docker.io/lfds/audittrace-librechat-bff:")
