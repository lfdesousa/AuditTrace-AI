"""M3-WU-D2-5E — DHCP-proof front-door DNS chart tests.

Guards the fix for the 2026-09-01 LibreChat OIDC hard-down incident: an
overnight DHCP renewal moved the host LAN IP (192.168.1.231 ->
10.12.40.59), the pod's static `hostAliases` entry pinned to the OLD IP
went dead (hostAliases override CoreDNS, no self-heal), and LibreChat's
own one-shot (no-retry) OIDC discovery fetch failed permanently for the
process life.

The fix removes the static-IP `hostAliases` pin entirely and instead
templates a `coredns-custom` overlay ConfigMap (kube-system) that
rewrites the front-door hostnames to the k3s NODE NAME —
`console.frontDoorNodeName` — which k3s's own CoreDNS already tracks to
the node's CURRENT IP via NodeHosts (self-healing, no pod restart).

Three falsifiable invariants (spec acceptance Rule 1, a/b/c):

(a) the overlay ConfigMap rewrites exactly the three front-door hosts
    (`console.librechat.frontDoorHost`, `console.frontDoorCloudHost`,
    `console.librechat.host`) to `console.frontDoorNodeName` —
    ``test_overlay_rewrites_three_hosts_to_node_name``;
(b) NO `frontDoorHostAlias` field survives anywhere in the chart
    source (values or templates) — neuter by re-adding the field to
    `values-laptop.yaml` (or a template `.Values.console.librechat.
    frontDoorHostAlias` reference) and this class of test goes RED —
    ``TestNoFrontDoorHostAliasSurvives``; AND no IPv4 literal reaches
    the actual laptop-profile `frontDoorNodeName` value or the
    rendered overlay's rewrite targets — neuter by setting
    `frontDoorNodeName` to an IP and
    ``test_laptop_profile_node_name_is_not_ip_shaped`` /
    ``test_rewrite_targets_are_never_ip_shaped`` go RED;
(c) the LibreChat pod has NO `hostAliases` key at all —
    ``test_librechat_pod_has_no_host_aliases``.
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
TEMPLATES_DIR = CHART_DIR / "templates"

# Mirrors `make helm-lint` / `scripts/deploy-preflight.sh`'s throwaway
# secrets, plus the console-only bff exchange secret and the observability
# / externalLLM FQDNs `required`-gated elsewhere in the chart (ADR-045) —
# needed so a full `console.enabled=true` render doesn't abort on an
# unrelated `required` guard.
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

_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

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


def _find_optional(resources: list[dict], kind: str, name: str) -> dict | None:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    return None


def _find(resources: list[dict], kind: str, name: str) -> dict:
    found = _find_optional(resources, kind, name)
    if found is None:
        raise AssertionError(f"{kind}/{name} not found in rendered chart")
    return found


def _override_lines(cm: dict) -> list[str]:
    raw = (cm.get("data") or {}).get("frontdoor.override", "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


class TestOverlayRendersAndGuardsCleanly:
    def test_overlay_rewrites_three_hosts_to_node_name(self) -> None:
        """(a) — with frontDoorNodeName set, the coredns-custom ConfigMap
        rewrites exactly the three front-door hosts to that node name."""
        resources = _render(
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node-frontdoor",
            ]
        )
        cm = _find(resources, "ConfigMap", "coredns-custom")
        assert cm["metadata"]["namespace"] == "kube-system", (
            "coredns-custom MUST render into kube-system — that is the "
            "fixed name/namespace k3s's packaged CoreDNS auto-imports "
            "*.override keys from; any other namespace is inert."
        )
        lines = _override_lines(cm)
        assert len(lines) == 3, f"expected exactly 3 rewrite lines, got: {lines}"
        expected_hosts = {
            "audittrace.local",
            "audittrace.allaboutdata.eu",
            "librechat.audittrace.local",
        }
        seen_hosts: set[str] = set()
        for line in lines:
            m = re.match(r"^rewrite name exact (\S+) (\S+)$", line)
            assert m, f"malformed rewrite line: {line!r}"
            host, target = m.group(1), m.group(2)
            seen_hosts.add(host)
            assert target == "test-node-frontdoor", (
                f"host {host!r} rewrites to {target!r}, expected the "
                "configured frontDoorNodeName"
            )
        assert seen_hosts == expected_hosts, (
            f"rewritten hosts {seen_hosts} != expected {expected_hosts}"
        )

    def test_overlay_absent_when_node_name_unset(self) -> None:
        """Guard: `console.enabled=true` with no `frontDoorNodeName` set
        (the default) renders NO coredns-custom ConfigMap — helm-lint /
        CI must stay clean with no live compute step, same posture the
        removed `frontDoorHostAlias` hostAliases guard had."""
        resources = _render(["--set", "console.enabled=true"])
        assert _find_optional(resources, "ConfigMap", "coredns-custom") is None

    def test_overlay_absent_by_default(self) -> None:
        """The chart's own bare defaults (console disabled, no node name)
        never render the overlay."""
        resources = _render([])
        assert _find_optional(resources, "ConfigMap", "coredns-custom") is None


class TestLibrechatPodHasNoHostAliases:
    def test_librechat_pod_has_no_host_aliases(self) -> None:
        """(c) — the static hostAliases pin is gone from the pod spec,
        with or without frontDoorNodeName set."""
        for extra in (
            ["--set", "console.enabled=true"],
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node-frontdoor",
            ],
        ):
            resources = _render(extra)
            dep = _find(resources, "Deployment", "audittrace-librechat")
            pod_spec = dep["spec"]["template"]["spec"]
            assert "hostAliases" not in pod_spec, (
                f"hostAliases resurfaced in the librechat pod spec with "
                f"--set {extra} — the static-IP pin must stay removed."
            )


class TestNoIpv4LiteralInFrontDoorPath:
    def test_rewrite_targets_are_never_ip_shaped(self) -> None:
        """(b) — the rendered overlay's rewrite TARGETS (the node-name
        column) must never be an IPv4 literal, even if an operator
        mistakenly sets frontDoorNodeName to one. This is the render-side
        half of invariant (b); NEUTER: set frontDoorNodeName to an IP and
        this goes RED."""
        resources = _render(
            [
                "--set",
                "console.enabled=true",
                "--set",
                "console.frontDoorNodeName=test-node-frontdoor",
            ]
        )
        cm = _find(resources, "ConfigMap", "coredns-custom")
        for line in _override_lines(cm):
            target = line.rsplit(" ", 1)[-1]
            assert not _IPV4_RE.match(target), (
                f"rewrite target {target!r} is IP-shaped — the whole "
                "point of M3-WU-D2-5E is resolving by NAME, never an IP."
            )

    def test_laptop_profile_node_name_is_not_ip_shaped(self) -> None:
        """The COMMITTED laptop-profile value itself (values-laptop.yaml,
        the file a real `helm upgrade -f values-laptop.yaml` reads) is a
        node NAME, never an IP. NEUTER: set
        `console.frontDoorNodeName` to an IP in values-laptop.yaml and
        this goes RED."""
        laptop_values = yaml.safe_load(VALUES_LAPTOP.read_text(encoding="utf-8"))
        node_name = laptop_values.get("console", {}).get("frontDoorNodeName")
        assert node_name, "values-laptop.yaml must set console.frontDoorNodeName"
        assert not _IPV4_RE.match(str(node_name)), (
            f"console.frontDoorNodeName={node_name!r} in values-laptop.yaml "
            "is IP-shaped — DHCP will invalidate it again."
        )

    def test_laptop_profile_render_has_no_ip_shaped_rewrite_targets(self) -> None:
        """End-to-end: rendering with the ACTUAL committed
        values-laptop.yaml never produces an IP-shaped rewrite target."""
        resources = _render(
            ["--set", "console.enabled=true"], values_file=VALUES_LAPTOP
        )
        cm = _find(resources, "ConfigMap", "coredns-custom")
        for line in _override_lines(cm):
            target = line.rsplit(" ", 1)[-1]
            assert not _IPV4_RE.match(target)


class TestNoFrontDoorHostAliasSurvives:
    """(b) — the `frontDoorHostAlias` field (and the hostAliases
    mechanism it fed) must not survive anywhere in the chart source.
    NEUTER: re-add `frontDoorHostAlias` to values-laptop.yaml, values.yaml,
    or any template, and these go RED."""

    def _chart_source_files(self) -> list[Path]:
        out: list[Path] = list(CHART_DIR.glob("values*.yaml"))
        for ext in ("*.yaml", "*.yml", "*.tpl"):
            out.extend(TEMPLATES_DIR.rglob(ext))
        return out

    def test_no_frontdoorhostalias_string_anywhere_in_chart_source(self) -> None:
        """Scoped to LIVE usage (a YAML key or a `.Values...` template
        reference), not prose — the removal is documented in comments
        (e.g. "the removed `frontDoorHostAlias` static-IP pin") that
        deliberately keep saying the retired field's name for the next
        reader; that history is desirable and must not itself trip this
        guard. A comment line is any line whose stripped text starts
        with `#`."""
        live_pattern = re.compile(
            r"frontDoorHostAlias\s*:|\.Values[.\w]*frontDoorHostAlias\b"
        )
        offenders = []
        for path in self._chart_source_files():
            for lineno, raw_line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = raw_line.strip()
                if stripped.startswith("#"):
                    continue
                if live_pattern.search(raw_line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        assert not offenders, (
            "Drift: `frontDoorHostAlias` reappeared as a LIVE key/template "
            "reference in chart source — this is the exact static-IP-pin "
            "mechanism M3-WU-D2-5E removed (2026-09-01 DHCP hard-down "
            f"incident). Offenders: {offenders}"
        )

    def test_values_laptop_console_librechat_has_no_frontdoorhostalias_key(
        self,
    ) -> None:
        laptop_values = yaml.safe_load(VALUES_LAPTOP.read_text(encoding="utf-8"))
        librechat = laptop_values.get("console", {}).get("librechat", {}) or {}
        assert "frontDoorHostAlias" not in librechat

    def test_values_default_console_librechat_has_no_frontdoorhostalias_key(
        self,
    ) -> None:
        values_default = yaml.safe_load(
            (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
        )
        librechat = values_default.get("console", {}).get("librechat", {}) or {}
        assert "frontDoorHostAlias" not in librechat
