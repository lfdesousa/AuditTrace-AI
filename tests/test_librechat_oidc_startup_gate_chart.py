"""M3-WU-D2-5H — LibreChat OIDC discovery startup-gate chart tests.

Guards the fix for the 2026-09-01 PM reboot recurrence: even with Part E's
DNS fix (`console.frontDoorNodeName`) intact and the host IP *unchanged*,
the librechat pod won the boot-time race against Keycloak's front-door
readiness. LibreChat configures its `openid` passport strategy via a
ONE-SHOT, NO-RETRY fetch of the OIDC discovery URL at process start; if
that fetch does not return HTTP 200, the strategy is never registered for
the life of the process and every login fails with `Unknown authentication
strategy "openid"` — even though the pod itself looks healthy (2/2,
/health green).

The fix adds a `wait-for-oidc-discovery` **initContainer** to the librechat
pod that blocks (bounded, via `console.librechat.oidcWait`) until the
discovery URL returns HTTP 200, reusing the SAME fork image as the init
image (no new egress image / CA-trust surface).

Four falsifiable invariants (spec acceptance Rule 1, a/b/c/d):

(a) the librechat pod has an initContainer that polls the discovery URL —
    ``TestInitContainerPresentAndPolls``;
(b) the init image is the SAME fork image already pulled for the pod (no
    new image) — ``test_init_container_reuses_fork_image``;
(c) the wait is bounded by the templated timeout (a finite,
    values-driven number, never absent/unbounded) —
    ``TestWaitIsBounded``. NEUTER: remove the initContainer, or make the
    timeout unbounded (e.g. delete the `elapsed -ge timeout` exit-1 branch
    or the timeout env var), and this class of test goes RED;
(d) the polled URL equals the main container's `OPENID_ISSUER` value
    (single source of truth — no duplicated literal) —
    ``TestSingleSourceOfTruthForDiscoveryUrl``.
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
VALUES_LAPTOP = CHART_DIR / "values-laptop.yaml"
VALUES_DEFAULT = CHART_DIR / "values.yaml"
DEPLOYMENT_TEMPLATE = CHART_DIR / "templates" / "console" / "deployment-librechat.yaml"

# Mirrors tests/test_frontdoor_dns_chart.py's base set — the throwaway
# secrets + FQDNs needed so a full console.enabled=true render doesn't
# abort on an unrelated `required` guard (ADR-045).
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


def _librechat_pod_spec(extra_sets: list[str], values_file: Path | None = None) -> dict:
    resources = _render(extra_sets, values_file=values_file)
    dep = _find(resources, "Deployment", "audittrace-librechat")
    return dep["spec"]["template"]["spec"]


def _env_map(container: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def _init_container(pod_spec: dict) -> dict:
    init_containers = pod_spec.get("initContainers") or []
    assert len(init_containers) >= 1, (
        "no initContainers rendered on the librechat pod — the "
        "wait-for-oidc-discovery gate is missing"
    )
    gate = next(
        (c for c in init_containers if c.get("name") == "wait-for-oidc-discovery"),
        None,
    )
    assert gate is not None, (
        f"no initContainer named 'wait-for-oidc-discovery' found; got "
        f"{[c.get('name') for c in init_containers]}"
    )
    return gate


def _main_container(pod_spec: dict) -> dict:
    containers = pod_spec.get("containers") or []
    main = next((c for c in containers if c.get("name") == "librechat"), None)
    assert main is not None, "no 'librechat' container found in pod spec"
    return main


class TestInitContainerPresentAndPolls:
    """(a) — the pod has an initContainer that polls the discovery URL."""

    def test_init_container_present(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        assert gate["name"] == "wait-for-oidc-discovery"

    def test_init_container_command_polls_discovery_url_env(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        env = _env_map(gate)
        assert "OIDC_DISCOVERY_URL" in env, (
            "the init container must carry the discovery URL as an "
            "env var it actually polls"
        )
        assert env["OIDC_DISCOVERY_URL"].startswith("https://")
        # The command must reference the env var it's polling (not a
        # hardcoded/duplicated literal baked into the shell script).
        command_blob = "\n".join(str(c) for c in gate.get("command", []))
        assert "OIDC_DISCOVERY_URL" in command_blob

    def test_init_container_present_regardless_of_frontdoor_node_name(self) -> None:
        """The gate exists whether or not Part E's frontDoorNodeName is
        set — it is an independent, complementary fix, not conditional on
        E's DNS overlay rendering."""
        pod_spec = _librechat_pod_spec(["--set", "console.enabled=true"])
        _init_container(pod_spec)  # must not raise


class TestInitContainerReusesForkImage:
    """(b) — no new image; init image == the fork image already pulled."""

    def test_init_container_reuses_fork_image(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        main = _main_container(pod_spec)
        assert gate["image"] == main["image"], (
            f"init image {gate['image']!r} != main container image "
            f"{main['image']!r} — the spec requires reusing the fork "
            "image, never a new egress image / CA-trust surface"
        )
        # M3-WU-D2-5C (2026-09-02) — the D2-4 sovereign fork cutover
        # repointed console.librechat.image from the D1 stock upstream
        # image to the pinned fork; the invariant this test guards (init
        # image == main image, no NEW egress image) is unchanged, only
        # the concrete repository moved.
        assert gate["image"].startswith("docker.io/lfds/audittrace-librechat:")


class TestWaitIsBounded:
    """(c) — the wait is bounded by the templated timeout.

    NEUTER: remove the OIDC_WAIT_TIMEOUT_SECONDS env var, or the
    ``elapsed -ge`` exit-1 guard in the script, and these go RED.
    """

    def test_timeout_env_var_present_and_finite(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        env = _env_map(gate)
        assert "OIDC_WAIT_TIMEOUT_SECONDS" in env
        timeout = int(env["OIDC_WAIT_TIMEOUT_SECONDS"])
        assert 0 < timeout < 3600, (
            f"timeout {timeout} is not a sane finite bound (0, 3600) — "
            "a genuinely-down Keycloak must eventually fail the init "
            "loudly rather than wait forever"
        )

    def test_interval_env_var_present_and_positive(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        env = _env_map(gate)
        assert "OIDC_WAIT_INTERVAL_SECONDS" in env
        assert int(env["OIDC_WAIT_INTERVAL_SECONDS"]) > 0

    def test_script_exits_nonzero_on_timeout(self) -> None:
        """The poll loop must have a bounded exit path: an explicit
        `exit 1` gated on elapsed >= timeout, referencing the templated
        timeout env var (not a hardcoded number, not an infinite loop
        with no exit)."""
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        script = "\n".join(str(c) for c in gate.get("command", []))
        assert re.search(r"-ge\s+\"?\$\{?OIDC_WAIT_TIMEOUT_SECONDS\}?\"?", script), (
            "no bounded timeout check against OIDC_WAIT_TIMEOUT_SECONDS "
            "found in the init container's poll script"
        )
        assert re.search(r"exit\s+1\b", script), (
            "no non-zero exit on timeout found — an unbounded wait "
            "would silently start the main container into an "
            "unregistered-strategy state instead of failing loudly"
        )

    def test_values_default_oidc_wait_is_a_sane_finite_bound(self) -> None:
        values_default = yaml.safe_load(VALUES_DEFAULT.read_text(encoding="utf-8"))
        oidc_wait = (
            values_default.get("console", {}).get("librechat", {}).get("oidcWait") or {}
        )
        assert isinstance(oidc_wait.get("timeoutSeconds"), int)
        assert 0 < oidc_wait["timeoutSeconds"] < 3600
        assert isinstance(oidc_wait.get("intervalSeconds"), int)
        assert oidc_wait["intervalSeconds"] > 0
        assert oidc_wait["intervalSeconds"] < oidc_wait["timeoutSeconds"]

    def test_custom_timeout_value_flows_through_to_rendered_env(self) -> None:
        """Bounds are parameterized via values, not hardcoded in the
        template — overriding the value changes the rendered env."""
        pod_spec = _librechat_pod_spec(
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node",
                "--set",
                "console.librechat.oidcWait.timeoutSeconds=42",
                "--set",
                "console.librechat.oidcWait.intervalSeconds=3",
            ]
        )
        gate = _init_container(pod_spec)
        env = _env_map(gate)
        assert env["OIDC_WAIT_TIMEOUT_SECONDS"] == "42"
        assert env["OIDC_WAIT_INTERVAL_SECONDS"] == "3"


class TestSingleSourceOfTruthForDiscoveryUrl:
    """(d) — the polled URL == the main container's OPENID_ISSUER."""

    def test_polled_url_equals_openid_issuer(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        main = _main_container(pod_spec)
        gate_env = _env_map(gate)
        main_env = _env_map(main)
        assert gate_env["OIDC_DISCOVERY_URL"] == main_env["OPENID_ISSUER"], (
            f"init container polls {gate_env['OIDC_DISCOVERY_URL']!r} but "
            f"the main container's OPENID_ISSUER is "
            f"{main_env['OPENID_ISSUER']!r} — these MUST be the same "
            "value (single source of truth), or the gate could pass "
            "while the app's real discovery fetch still fails"
        )

    def test_polled_url_equals_openid_issuer_with_custom_frontdoor_host(
        self,
    ) -> None:
        """The single-source-of-truth invariant must hold under a custom
        frontDoorHost too, not just the default."""
        pod_spec = _librechat_pod_spec(
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node",
                "--set",
                "console.librechat.frontDoorHost=custom-front-door.example",
            ]
        )
        gate = _init_container(pod_spec)
        main = _main_container(pod_spec)
        gate_env = _env_map(gate)
        main_env = _env_map(main)
        assert gate_env["OIDC_DISCOVERY_URL"] == main_env["OPENID_ISSUER"]
        assert "custom-front-door.example" in main_env["OPENID_ISSUER"]

    def test_polled_url_equals_openid_issuer_on_laptop_profile(self) -> None:
        """End-to-end: rendering with the ACTUAL committed
        values-laptop.yaml (the file a real
        `helm upgrade -f values-laptop.yaml ...` reads) still holds the
        invariant."""
        pod_spec = _librechat_pod_spec(
            ["--set", "console.enabled=true"], values_file=VALUES_LAPTOP
        )
        gate = _init_container(pod_spec)
        main = _main_container(pod_spec)
        gate_env = _env_map(gate)
        main_env = _env_map(main)
        assert gate_env["OIDC_DISCOVERY_URL"] == main_env["OPENID_ISSUER"]

    def test_no_second_literal_well_known_url_hardcoded_in_template(self) -> None:
        """The chart source itself must define the discovery URL literal
        exactly once (the `$oidcDiscoveryURL` template variable) — a
        second, independently-typed `.well-known/openid-configuration`
        printf/string literal in the same file would be the exact drift
        trap the spec warns about."""
        text = DEPLOYMENT_TEMPLATE.read_text(encoding="utf-8")
        # Count occurrences of the literal URL-building expression
        # (the printf that builds the well-known URL from frontDoorHost).
        occurrences = text.count(".well-known/openid-configuration")
        assert occurrences == 1, (
            f"expected the discovery-URL literal to appear exactly once "
            f"in {DEPLOYMENT_TEMPLATE.name} (the single $oidcDiscoveryURL "
            f"definition), found {occurrences} — a second independently "
            "typed literal is the URL-drift trap the spec calls out"
        )


class TestNoNewCaTrustSurface:
    """No new image / no new egress target: the init container's TLS
    trust wiring mirrors the main container's exactly (same
    NODE_EXTRA_CA_CERTS / insecureTLS conditional, same ca-trust
    volumeMount)."""

    def test_init_container_mounts_same_ca_trust_volume_as_main(self) -> None:
        pod_spec = _librechat_pod_spec(_CONSOLE_ENABLED)
        gate = _init_container(pod_spec)
        main = _main_container(pod_spec)
        gate_mounts = {m["name"] for m in gate.get("volumeMounts", [])}
        main_mounts = {m["name"] for m in main.get("volumeMounts", [])}
        assert "ca-trust" in gate_mounts
        assert (
            gate_mounts <= main_mounts | {"librechat-yaml"} or "ca-trust" in main_mounts
        )

    def test_init_container_no_ca_trust_mount_when_insecure_tls(self) -> None:
        pod_spec = _librechat_pod_spec(
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node",
                "--set",
                "console.librechat.insecureTLS=true",
            ]
        )
        gate = _init_container(pod_spec)
        gate_mounts = {m["name"] for m in gate.get("volumeMounts", [])}
        assert "ca-trust" not in gate_mounts
        env = _env_map(gate)
        assert env.get("NODE_TLS_REJECT_UNAUTHORIZED") == "0"
        assert "NODE_EXTRA_CA_CERTS" not in env
