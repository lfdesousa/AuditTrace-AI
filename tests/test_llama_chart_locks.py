"""LC-5 / M3-WU-4 -- the chart half of the llama endpoint isolation locks.

Hermetic: ``helm template`` only (a chart-only invocation the hermetic guard
allows); no ``kubectl``, no cluster. Three things are pinned:

1. **The key delivery** (``memoryServer.llmApiKey.enabled``): flag off renders
   no ``llm`` ``with secret`` and none of the three ``AUDITTRACE_*_API_KEY``
   names; flag on renders all three, from the Vault path
   ``kv/data/audittrace/llm/api-key``; the ``audittrace-server.hcl`` policy
   carries the ``llm/*`` read line in BOTH states (unconditional, so
   ``setup-vault.sh`` can apply it between the two deploy passes). Vault off
   (c0): no key env either way. The three names equal the ``Settings`` env names.
2. **The overlays** (one key each) and ``values-laptop.yaml`` (adds only
   ``console.networkPolicy.enabled: true``); ``console.bff.secretSource`` is
   untouched.
3. **The console egress NetworkPolicy** (gate 5): the pod selector matches
   EXACTLY the rendered librechat and bff pod templates under Kubernetes
   label-selector semantics, the egress rules equal the allow-list table of
   the spec, the :8200 Vault row exists only when the BFF renders with the
   Vault agent, and the ingressgateway port is templated from
   ``console.networkPolicy.ingressGatewayTargetPort`` (default 8443) -- a
   literal in the template turns the override test RED.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from audittrace.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH -- chart-rendering tests need it",
)

# Throwaway secrets + FQDN-only host fields the chart requires to render at all
# (mirrors tests/test_fleet_route_mistral_chart.py). Placeholders only.
_BASE = [
    "--set", "secrets.minio.secretKey=ci-test",
    "--set", "secrets.minio.kmsKey=ci-test",
    "--set", "secrets.chromadb.token=ci-test",
    "--set", "secrets.keycloak.adminPassword=ci-test",
    "--set", "secrets.postgres.appPassword=ci-test",
    "--set", "secrets.postgres.password=ci-test",
    "--set", "secrets.redis.password=ci-test",
    "--set", "secrets.summariser.password=ci-test",
    "--set", "externalLLM.host=host.audittrace.local",
    "--set", "observability.external.langfuseHost=langfuse.test.invalid",
    "--set", "observability.external.tempoHost=tempo.test.invalid",
    "--set", "observability.external.lokiHost=loki.test.invalid",
]  # fmt: skip

KEY_NAMES = (
    "AUDITTRACE_LLAMA_API_KEY",
    "AUDITTRACE_EMBED_API_KEY",
    "AUDITTRACE_SUMMARIZER_API_KEY",
)
LLM_PATH = "kv/data/audittrace/llm/api-key"
TEMPLATE_ANNOTATION = "vault.hashicorp.com/agent-inject-template-env"


def render(*sets: str, files: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    argv = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "-n",
        "audittrace",
        *_BASE,
    ]
    for f in files:
        argv += ["-f", str(CHART_DIR / f)]
    for s in sets:
        argv += ["--set", s]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs: list[dict], kind: str, name: str) -> dict:
    hits = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(hits) == 1, f"expected exactly one {kind}/{name}, found {len(hits)}"
    return hits[0]


def memory_template(docs: list[dict]) -> str:
    dep = find(docs, "Deployment", "audittrace-memory-server")
    return dep["spec"]["template"]["metadata"]["annotations"].get(
        TEMPLATE_ANNOTATION, ""
    )


def server_hcl(docs: list[dict]) -> str:
    return find(docs, "ConfigMap", "audittrace-vault-policies")["data"][
        "audittrace-server.hcl"
    ]


# ═══════════════════════ 1. key delivery behind the flag ═══════════════════════


class TestKeyDelivery:
    def test_flag_off_renders_no_llm_secret_and_no_key_names(self):
        docs = render("vault.enabled=true")
        template = memory_template(docs)
        assert template, "vault on: the memory-server must carry the agent template"
        assert "llm/api-key" not in template
        for name in KEY_NAMES:
            assert name not in template
        assert not any(n in yaml.safe_dump(docs) for n in KEY_NAMES), (
            "no AUDITTRACE_*_API_KEY name may appear anywhere in the render with the flag off"
        )

    def test_flag_on_renders_all_three_names_from_the_llm_path(self):
        docs = render("vault.enabled=true", "memoryServer.llmApiKey.enabled=true")
        template = memory_template(docs)
        assert f'with secret "{LLM_PATH}"' in template
        for name in KEY_NAMES:
            assert f"export {name}='{{{{ .Data.data.api_key }}}}'" in template, name
        # the pod-template annotation NAMES the path (window check C1, status-only)
        assert LLM_PATH in template

    def test_flag_on_renders_no_literal_key_value(self):
        docs = render("vault.enabled=true", "memoryServer.llmApiKey.enabled=true")
        template = memory_template(docs)
        for name in KEY_NAMES:
            line = next(ln for ln in template.splitlines() if f"export {name}=" in ln)
            assert ".Data.data.api_key" in line and "ci-test" not in line

    @pytest.mark.parametrize("flag", ["false", "true"])
    def test_vault_off_c0_renders_no_key_env_either_way(self, flag):
        docs = render("vault.enabled=false", f"memoryServer.llmApiKey.enabled={flag}")
        dumped = yaml.safe_dump(docs)
        assert not any(n in dumped for n in KEY_NAMES)
        assert "llm/api-key" not in dumped

    @pytest.mark.parametrize("flag", ["false", "true"])
    def test_policy_hcl_carries_the_llm_read_line_in_both_states(self, flag):
        hcl = server_hcl(
            render("vault.enabled=true", f"memoryServer.llmApiKey.enabled={flag}")
        )
        assert re.search(
            r'path "kv/data/audittrace/llm/\*"\s*\{\s*capabilities = \["read"\]\s*\}',
            hcl,
        ), "the llm/* read line must be in the policy in both flag states"
        for existing in ("postgres", "summariser", "redis", "chromadb", "minio"):
            assert f'path "kv/data/audittrace/{existing}/*"' in hcl, existing

    def test_the_three_names_are_the_settings_env_names(self):
        prefix = Settings.model_config.get("env_prefix", "")
        fields = ("llama_api_key", "embed_api_key", "summarizer_api_key")
        assert tuple(f"{prefix}{f}".upper() for f in fields) == KEY_NAMES
        for f in fields:
            assert Settings.model_fields[f].default == "", (
                "empty = no header (unlocked env keeps working)"
            )

    def test_no_role_change_the_role_binds_the_policy_name(self):
        """The policy BODY gained a line; no role or policy name moved."""
        cm = find(
            render("vault.enabled=true"), "ConfigMap", "audittrace-vault-policies"
        )["data"]
        assert "audittrace-server.hcl" in cm
        role = cm["role-audittrace-server.env"]
        assert "audittrace-server" in role


# ═══════════════════════════════ 2. the overlays ═══════════════════════════════


def _leaves(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): node}


class TestOverlays:
    @pytest.mark.parametrize(
        ("file", "expected"),
        [
            ("values-llm-key-on.yaml", {"memoryServer.llmApiKey.enabled": True}),
            ("values-llm-key-off.yaml", {"memoryServer.llmApiKey.enabled": False}),
            ("values-netpol-off.yaml", {"console.networkPolicy.enabled": False}),
        ],
    )
    def test_each_overlay_sets_exactly_one_key(self, file, expected):
        assert _leaves(yaml.safe_load((CHART_DIR / file).read_text())) == expected

    def test_values_laptop_gains_only_the_network_policy_switch(self):
        laptop = yaml.safe_load((CHART_DIR / "values-laptop.yaml").read_text())
        assert laptop["console"]["networkPolicy"] == {"enabled": True}
        assert set(laptop["console"]) == {
            "enabled", "frontDoorNodeName", "librechat", "networkPolicy"
        }  # fmt: skip
        assert "secretSource" not in str(laptop), (
            "LC-5 must not change console.bff.secretSource"
        )

    def test_chart_defaults(self):
        base = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert base["memoryServer"]["llmApiKey"] == {"enabled": False}
        assert base["console"]["networkPolicy"] == {
            "enabled": False,
            "ingressGatewayTargetPort": 8443,
        }
        assert base["console"]["bff"]["secretSource"] == "vault", (
            "secretSource is not an LC-5 change"
        )

    def test_overlay_wins_over_laptop_when_stacked(self):
        on = render("console.enabled=true", files=("values-laptop.yaml",))
        off = render(
            "console.enabled=true",
            files=("values-laptop.yaml", "values-netpol-off.yaml"),
        )
        assert find(on, "NetworkPolicy", "audittrace-console-egress")
        assert not [
            d for d in off if d["metadata"]["name"] == "audittrace-console-egress"
        ]


# ════════════════════════════ 3. the console NetworkPolicy ════════════════════════════

NP_NAME = "audittrace-console-egress"


def selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    """Kubernetes label-selector semantics (matchLabels AND matchExpressions)."""
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        present = key in labels
        if op == "In" and not (present and labels[key] in values):
            return False
        if op == "NotIn" and present and labels[key] in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
    return True


def pod_templates(docs: list[dict]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet"):
            out[d["metadata"]["name"]] = d["spec"]["template"]["metadata"].get(
                "labels", {}
            )
    return out


def console_docs(*extra: str) -> list[dict]:
    return render(
        "console.enabled=true",
        "console.networkPolicy.enabled=true",
        "vault.enabled=true",
        *extra,
    )


class TestNetworkPolicySelector:
    def test_selects_exactly_the_librechat_and_bff_pod_templates(self):
        docs = console_docs()
        sel = find(docs, "NetworkPolicy", NP_NAME)["spec"]["podSelector"]
        selected = {
            n for n, lab in pod_templates(docs).items() if selector_matches(sel, lab)
        }
        assert selected == {"audittrace-librechat", "audittrace-librechat-bff"}

    def test_no_app_label_is_used_none_exists_on_a_console_pod(self):
        docs = console_docs()
        sel = find(docs, "NetworkPolicy", NP_NAME)["spec"]["podSelector"]
        assert "app" not in (sel.get("matchLabels") or {})
        for name in ("audittrace-librechat", "audittrace-librechat-bff"):
            assert "app" not in pod_templates(docs)[name]

    def test_policy_is_egress_only(self):
        spec = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]
        assert spec["policyTypes"] == ["Egress"] and "ingress" not in spec

    def test_selector_check_is_not_vacuous_one_renamed_label_unselects_the_pod(self):
        docs = console_docs()
        sel = find(docs, "NetworkPolicy", NP_NAME)["spec"]["podSelector"]
        labels = dict(pod_templates(docs)["audittrace-librechat-bff"])
        assert selector_matches(sel, labels)
        labels["app.kubernetes.io/component"] = "librechat-bff-renamed"
        assert not selector_matches(sel, labels)
        labels = dict(pod_templates(docs)["audittrace-librechat"])
        labels["app.kubernetes.io/instance"] = "another-release"
        assert not selector_matches(sel, labels)

    def test_matcher_semantics(self):
        lab = {"a": "1", "b": "2"}
        assert selector_matches(
            {"matchExpressions": [{"key": "a", "operator": "In", "values": ["1"]}]}, lab
        )
        assert not selector_matches(
            {"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["1"]}]},
            lab,
        )
        assert selector_matches(
            {"matchExpressions": [{"key": "b", "operator": "Exists"}]}, lab
        )
        assert selector_matches(
            {"matchExpressions": [{"key": "z", "operator": "DoesNotExist"}]}, lab
        )
        assert not selector_matches({"matchLabels": {"a": "9"}}, lab)


def _rule_key(rule: dict) -> tuple:
    peers = []
    for peer in rule["to"]:
        ns = (peer.get("namespaceSelector") or {}).get("matchLabels")
        pod = (peer.get("podSelector") or {}).get("matchLabels")
        peers.append(
            (tuple(sorted((ns or {}).items())), tuple(sorted((pod or {}).items())))
        )
    ports = tuple(sorted((p["protocol"], p["port"]) for p in rule["ports"]))
    return (tuple(peers), ports)


def _expected_rule(ns: dict | None, pod: dict, ports: list[tuple[str, int]]) -> tuple:
    return (
        ((tuple(sorted((ns or {}).items())), tuple(sorted(pod.items()))),),
        tuple(sorted(ports)),
    )


_INST = {"app.kubernetes.io/instance": "audittrace"}


def _same_ns(component: str) -> dict:
    return {**_INST, "app.kubernetes.io/component": component}


def expected_rules(*, vault_row: bool, gateway_port: int = 8443) -> set[tuple]:
    rules = [
        _expected_rule({"kubernetes.io/metadata.name": "kube-system"}, {"k8s-app": "kube-dns"},
                       [("UDP", 53), ("TCP", 53)]),
        _expected_rule(None, _same_ns("librechat-bff"), [("TCP", 8766)]),
        _expected_rule(None, _same_ns("memory-server"), [("TCP", 8765)]),
        _expected_rule(None, _same_ns("keycloak"), [("TCP", 8080)]),
        _expected_rule(None, _same_ns("librechat-mongodb"), [("TCP", 27017)]),
        _expected_rule({"kubernetes.io/metadata.name": "istio-system"}, {"app": "istiod"},
                       [("TCP", 15012)]),
        _expected_rule({"kubernetes.io/metadata.name": "kube-system"},
                       {"svccontroller.k3s.cattle.io/svcname": "istio-ingressgateway"},
                       [("TCP", 443)]),
        _expected_rule({"kubernetes.io/metadata.name": "istio-system"}, {"istio": "ingressgateway"},
                       [("TCP", gateway_port)]),
    ]  # fmt: skip
    if vault_row:
        rules.append(
            _expected_rule(None, {"app.kubernetes.io/name": "vault", "component": "server"},
                           [("TCP", 8200)])
        )  # fmt: skip
    return set(rules)


class TestNetworkPolicyEgressTable:
    def test_rendered_egress_equals_the_spec_table_with_the_vault_row(self):
        rules = find(
            console_docs("console.bff.secretSource=vault"), "NetworkPolicy", NP_NAME
        )["spec"]["egress"]
        assert {_rule_key(r) for r in rules} == expected_rules(vault_row=True)
        assert len(rules) == 9

    def test_rendered_egress_equals_the_spec_table_without_the_vault_row(self):
        rules = find(
            console_docs("console.bff.secretSource=values"), "NetworkPolicy", NP_NAME
        )["spec"]["egress"]
        assert {_rule_key(r) for r in rules} == expected_rules(vault_row=False)
        assert len(rules) == 8

    def test_there_is_no_ipblock_anywhere(self):
        spec = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]
        assert "ipBlock" not in yaml.safe_dump(spec)

    def test_llama_ports_are_never_allowed(self):
        spec = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]
        ports = {p["port"] for r in spec["egress"] for p in r["ports"]}
        assert not ports & {11434, 11435, 11436, 11437, 11438, 11450}

    def test_dns_is_the_only_rule_with_two_protocols(self):
        rules = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]["egress"]
        multi = [r for r in rules if len(r["ports"]) > 1]
        assert len(multi) == 1 and {p["port"] for p in multi[0]["ports"]} == {53}

    def test_a_row_neuter_is_visible_to_the_table_check(self):
        """Dropping one rendered row must make the table comparison fail."""
        rules = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]["egress"]
        assert {_rule_key(r) for r in rules[1:]} != expected_rules(vault_row=True)
        bumped = [dict(rules[1], ports=[dict(rules[1]["ports"][0], port=9999)])]
        assert {
            _rule_key(r) for r in [*rules[:1], *bumped, *rules[2:]]
        } != expected_rules(vault_row=True)


class TestVaultRowFollowsTheBffSidecar:
    """B2: the :8200 rule exists exactly when the BFF renders with the agent."""

    @pytest.mark.parametrize(
        ("vault_enabled", "source", "expect_vault"),
        [
            ("true", "vault", True),
            ("true", "values", False),
            ("false", "vault", False),
            ("false", "values", False),
        ],
    )
    def test_vault_rule_presence_equals_bff_annotation_presence(
        self, vault_enabled, source, expect_vault
    ):
        docs = render(
            "console.enabled=true",
            "console.networkPolicy.enabled=true",
            f"vault.enabled={vault_enabled}",
            f"console.bff.secretSource={source}",
        )
        bff = find(docs, "Deployment", "audittrace-librechat-bff")
        annotations = bff["spec"]["template"]["metadata"].get("annotations") or {}
        has_agent = "vault.hashicorp.com/agent-inject" in annotations
        rules = find(docs, "NetworkPolicy", NP_NAME)["spec"]["egress"]
        has_8200 = any(p["port"] == 8200 for r in rules for p in r["ports"])
        assert has_agent == expect_vault
        assert has_8200 == has_agent


class TestIngressGatewayTargetPort:
    """C2 (HERMETIC): the ingressgateway port is TEMPLATED, never a literal.
    The live equality against `kubectl get svc` is a read-only WINDOW check
    before step 4b, not part of this suite."""

    def test_default_is_8443(self):
        rules = find(console_docs(), "NetworkPolicy", NP_NAME)["spec"]["egress"]
        igw = [
            r
            for r in rules
            if r["to"][0].get("podSelector", {}).get("matchLabels")
            == {"istio": "ingressgateway"}
        ]
        assert len(igw) == 1 and igw[0]["ports"][0]["port"] == 8443

    @pytest.mark.parametrize("port", [9443, 15443, 18443])
    def test_an_override_reaches_the_rendered_rule(self, port):
        rules = find(
            console_docs(f"console.networkPolicy.ingressGatewayTargetPort={port}"),
            "NetworkPolicy",
            NP_NAME,
        )["spec"]["egress"]
        assert _rule_key(rules[-1]) == _expected_rule(
            {"kubernetes.io/metadata.name": "istio-system"},
            {"istio": "ingressgateway"},
            [("TCP", port)],
        ), "the template ignored the value: a literal port in the template"
        assert not any(p["port"] == 8443 for r in rules for p in r["ports"])

    def test_the_template_has_no_literal_gateway_port(self):
        text = (CHART_DIR / "templates/console/networkpolicy-egress.yaml").read_text()
        assert "{{ .Values.console.networkPolicy.ingressGatewayTargetPort }}" in text
        body = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert not any("8443" in ln for ln in body)


class TestPolicyIsOffUnlessAskedFor:
    def test_default_render_has_no_console_policy(self):
        docs = render("console.enabled=true")
        assert not [d for d in docs if d["metadata"]["name"] == NP_NAME]

    def test_policy_needs_the_console(self):
        docs = render("console.enabled=false", "console.networkPolicy.enabled=true")
        assert not [d for d in docs if d["metadata"]["name"] == NP_NAME]

    def test_secretsource_is_untouched_by_lc5(self):
        docs = console_docs()
        bff = find(docs, "Deployment", "audittrace-librechat-bff")
        assert (
            "vault.hashicorp.com/agent-inject"
            in bff["spec"]["template"]["metadata"]["annotations"]
        )
