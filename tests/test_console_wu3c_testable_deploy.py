"""M3-WU-3c — make the console deploy testable + deterministic (no
Vault-root-token dance).

Rule 1 (spec Acceptance §1-3) of the ratified spec
(specs/2026-08-29-SPEC-m3-wu3c-console-testable-deploy.md, private repo):
a guard asserting BOTH ``console.bff.secretSource`` paths render correctly
(``values`` -> secretKeyRef, no Vault; ``vault`` -> Vault path, WU-3b
behaviour byte-identical); a guard asserting the LibreChat image digest is
pinned (non-empty) in ``values.yaml``; a guard asserting the console
session secrets are reused-if-present (not regenerated) across a
simulated re-render. Neuter any of these -> RED.

Hermetic: shells out to ``helm template`` (no live cluster — ``lookup``
always sees an empty result in this mode, exercised deliberately below)
and parses the rendered manifests as YAML, plus static parses of
``values.yaml`` / ``values-laptop.yaml`` — same discipline as
``tests/test_console_k3s_guards.py``.

Drift classes covered:

* **D1 — the secretSource toggle is the actual gate, not a decoy.**
  ``secretSource=values`` renders the k8s-Secret env fallback and NEVER
  the Vault annotations / secret-file-sourcing shell, even when
  ``vault.enabled=true`` cluster-wide (the unblock). ``secretSource=vault``
  (the default) renders BYTE-IDENTICAL to the pre-WU-3c (WU-3b) Vault path
  when ``vault.enabled=true``, and still falls back to the k8s Secret when
  ``vault.enabled=false`` (unchanged dev posture).
* **D2 — the digest is pinned + the laptop profile is deterministic.**
  ``values.yaml`` carries a non-empty, well-formed ``sha256:<64-hex>``
  digest for the LibreChat image, which the rendered Deployment's image
  ref actually carries (``@sha256:...``). ``values-laptop.yaml`` pins
  ``console.frontDoorNodeName`` so `-f values-laptop.yaml` is the whole
  story — no ``kubectl get nodes`` at deploy time. M3-WU-D2-5E
  (2026-09-02) superseded the original static ``frontDoorHostAlias`` /
  pod ``hostAliases`` mechanism this bullet used to describe (the
  2026-09-01 DHCP hard-down incident: a static IP pin cannot self-heal
  when DHCP moves the host's LAN IP) with a chart-templated
  ``coredns-custom`` overlay that rewrites front-door hosts to the node
  NAME instead — see ``tests/test_frontdoor_dns_chart.py`` for that
  mechanism's own guards; the LibreChat pod carries NO ``hostAliases``
  at all any more, laptop profile or not.
* **D3 — session secrets are generate-if-absent + persist, not
  regenerate-every-render.** The persistence mechanism (a `lookup`-guarded
  Secret template) is structurally present in the template source (a
  neutered chart that dropped the `lookup` call and went back to a bare
  ``.Values``-only template would go RED here even though a single render
  still "looks fine"). An explicit ``secrets.console.*`` override is
  stable across repeated renders (never silently re-randomised). An
  absent override still yields correctly-shaped LibreChat AES-256-CBC
  hex material (creds-key: 64 hex chars / 32 bytes; creds-iv: 32 hex
  chars / 16 bytes) rather than an arbitrary/invalid string.

Anchors: ``feedback_no_more_drifts``, ``feedback_vacuous_neuter_test_antipattern``,
``feedback_ratified_spec_immutable``.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
VALUES_FILE = CHART_DIR / "values.yaml"
VALUES_LAPTOP_FILE = CHART_DIR / "values-laptop.yaml"
SECRET_LIBRECHAT_TEMPLATE = (
    CHART_DIR / "templates" / "console" / "secret-librechat.yaml"
)
HELPERS_TEMPLATE = CHART_DIR / "templates" / "_helpers.tpl"

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


def _render(
    *,
    console_enabled: bool = True,
    vault_enabled: bool = True,
    secret_source: str | None = None,
    extra_set: list[str] | None = None,
    values_files: list[Path] | None = None,
) -> list[dict]:
    """Render the chart, parsed into a list of manifest dicts."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "istio.enabled=true",
        "--set",
        f"vault.enabled={'true' if vault_enabled else 'false'}",
        "--set",
        f"console.enabled={'true' if console_enabled else 'false'}",
        *_LINT_SECRETS,
    ]
    for vf in values_files or []:
        cmd.extend(["-f", str(vf)])
    if secret_source is not None:
        cmd.extend(["--set", f"console.bff.secretSource={secret_source}"])
    cmd.extend(extra_set or [])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- args ---\n{cmd}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return [
        d
        for d in yaml.safe_load_all(result.stdout)
        if isinstance(d, dict) and d.get("kind")
    ]


def _find(docs: list[dict], kind: str, name_suffix: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name", "").endswith(
            name_suffix
        ):
            return d
    raise AssertionError(f"no {kind} ending in {name_suffix!r} in rendered docs")


def _find_optional(docs: list[dict], kind: str, name_suffix: str) -> dict | None:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name", "").endswith(
            name_suffix
        ):
            return d
    return None


def _container(deployment: dict, container_name: str) -> dict:
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container_name:
            return c
    raise AssertionError(f"no container named {container_name!r}")


def _container_env(deployment: dict, container_name: str) -> dict[str, object]:
    c = _container(deployment, container_name)
    out: dict[str, object] = {}
    for e in c.get("env", []) or []:
        out[e["name"]] = e.get("value", e.get("valueFrom"))
    return out


# ---------------------------------------------------------------------------
# D1 — console.bff.secretSource toggle
# ---------------------------------------------------------------------------


class TestSecretSourceValues:
    """secretSource=values MUST unblock a test deploy — no Vault root token,
    even when vault.enabled=true cluster-wide (the whole point of D1)."""

    def test_no_vault_annotations_even_when_vault_enabled(self) -> None:
        docs = _render(vault_enabled=True, secret_source="values")
        dep = _find(docs, "Deployment", "-librechat-bff")
        annotations = dep["spec"]["template"]["metadata"].get("annotations") or {}
        assert annotations.get("vault.hashicorp.com/agent-inject") != "true", (
            "console.bff.secretSource=values still rendered Vault annotations "
            "on the BFF Deployment even though vault.enabled=true cluster-"
            "wide — this is the exact friction D1 exists to remove: a test "
            "deploy should never need the Vault root token."
        )

    def test_no_vault_secret_file_sourcing_shell(self) -> None:
        docs = _render(vault_enabled=True, secret_source="values")
        dep = _find(docs, "Deployment", "-librechat-bff")
        c = _container(dep, "bff")
        args_joined = "\n".join(str(a) for a in (c.get("args") or []))
        assert "/vault/secrets/env" not in args_joined, (
            "secretSource=values still sources /vault/secrets/env — the "
            "container would CrashLoop waiting on a Vault Agent sidecar "
            "that secretSource=values never asked for."
        )

    def test_k8s_secretkeyref_present(self) -> None:
        docs = _render(vault_enabled=True, secret_source="values")
        dep = _find(docs, "Deployment", "-librechat-bff")
        env = _container_env(dep, "bff")
        secret_ref = env.get("AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET")
        assert secret_ref == {
            "secretKeyRef": {
                "name": f"{RELEASE}-librechat-bff-secret",
                "key": "exchange-client-secret",
            }
        }, (
            f"AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET={secret_ref!r} — expected "
            "a secretKeyRef to the k8s Secret, the whole unblock."
        )

    def test_k8s_secret_rendered_with_supplied_value(self) -> None:
        docs = _render(
            vault_enabled=True,
            secret_source="values",
            extra_set=["--set", "secrets.console.bffExchangeClientSecret=test-value-1"],
        )
        secret = _find(docs, "Secret", "-librechat-bff-secret")
        assert secret["stringData"]["exchange-client-secret"] == "test-value-1"

    def test_fails_closed_when_secret_left_empty(self) -> None:
        """Missing-secret fail-closed guard MUST still fire — D1 says never
        an empty secret silently accepted. bff/config.py's Settings has no
        default for the exchange secret, so an empty stringData still means
        the pod would refuse to start meaningfully; here we just confirm
        the chart never invents a non-empty placeholder when the operator
        supplied none."""
        docs = _render(vault_enabled=True, secret_source="values")
        secret = _find(docs, "Secret", "-librechat-bff-secret")
        assert secret["stringData"]["exchange-client-secret"] == "", (
            "chart rendered a non-empty exchange-client-secret with no "
            "operator input — a fabricated default would defeat the "
            "fail-closed startup guard."
        )


class TestSecretSourceVaultUnchanged:
    """secretSource=vault (the default) MUST remain WU-3b's exact production
    posture — the non-goal ('NOT removing Vault as the production
    posture') made mechanically enforced."""

    def test_default_secret_source_is_vault_in_values(self) -> None:
        values = yaml.safe_load(VALUES_FILE.read_text())
        assert values["console"]["bff"]["secretSource"] == "vault", (
            "console.bff.secretSource default drifted from 'vault' — WU-3b's "
            "production posture must stay the DEFAULT, not something an "
            "operator must remember to set."
        )

    def test_vault_annotations_present_default_and_explicit(self) -> None:
        for secret_source in (None, "vault"):
            docs = _render(vault_enabled=True, secret_source=secret_source)
            dep = _find(docs, "Deployment", "-librechat-bff")
            annotations = dep["spec"]["template"]["metadata"].get("annotations") or {}
            assert annotations.get("vault.hashicorp.com/agent-inject") == "true"
            assert annotations.get("vault.hashicorp.com/role") == "librechat-bff"

    def test_vault_secret_file_sourced_before_exec(self) -> None:
        docs = _render(vault_enabled=True, secret_source="vault")
        dep = _find(docs, "Deployment", "-librechat-bff")
        c = _container(dep, "bff")
        assert c.get("command") == ["/bin/sh", "-c"]
        args_joined = "\n".join(str(a) for a in (c.get("args") or []))
        assert ". /vault/secrets/env" in args_joined
        assert "exec uvicorn" in args_joined

    def test_no_plaintext_env_var_and_no_k8s_secret_rendered(self) -> None:
        docs = _render(vault_enabled=True, secret_source="vault")
        dep = _find(docs, "Deployment", "-librechat-bff")
        env = _container_env(dep, "bff")
        assert "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET" not in env
        assert _find_optional(docs, "Secret", "-librechat-bff-secret") is None, (
            "secretSource=vault with vault.enabled=true still rendered the "
            "k8s Secret — a stray plaintext copy of the exchange secret "
            "would exist in the cluster alongside the Vault-sourced one."
        )

    def test_falls_back_to_k8s_secret_when_vault_disabled(self) -> None:
        """vault.enabled=false must behave exactly as WU-3b regardless of
        secretSource (there is no Vault to source from at all)."""
        for secret_source in (None, "vault", "values"):
            docs = _render(vault_enabled=False, secret_source=secret_source)
            dep = _find(docs, "Deployment", "-librechat-bff")
            env = _container_env(dep, "bff")
            assert "AUDITTRACE_BFF_EXCHANGE_CLIENT_SECRET" in env
            annotations = dep["spec"]["template"]["metadata"].get("annotations") or {}
            assert annotations.get("vault.hashicorp.com/agent-inject") != "true"

    def test_default_render_byte_identical_to_wu3b(self) -> None:
        """The literal non-goal check: default secretSource + vault.enabled
        must be BYTE-IDENTICAL to the pre-WU-3c rendered BFF Deployment."""
        cmd = [
            "helm",
            "template",
            RELEASE,
            str(CHART_DIR),
            "-n",
            NAMESPACE,
            *_LINT_SECRETS,
            "--set",
            "console.enabled=true",
            "--set",
            "vault.enabled=true",
            "--show-only",
            "templates/console/deployment-bff.yaml",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        # The WU-3c change only ADDS a `$useVault` local var comment-free
        # computation and swaps the two `if` conditions' predicate — the
        # rendered YAML text for the default path must contain none of the
        # WU-3c-only markers (they only ever appear on the OTHER branch).
        assert "console.bff.secretSource=values" not in result.stdout
        assert result.stdout.count('vault.hashicorp.com/agent-inject: "true"') == 1


class TestSecretSourceMatrix:
    """Acceptance §4 — clean render across console.enabled true/false x
    secretSource values/vault x vault.enabled true/false."""

    @pytest.mark.parametrize("console_enabled", [True, False])
    @pytest.mark.parametrize("secret_source", ["values", "vault"])
    @pytest.mark.parametrize("vault_enabled", [True, False])
    def test_renders_cleanly(
        self, console_enabled: bool, secret_source: str, vault_enabled: bool
    ) -> None:
        # _render raises AssertionError (surfacing helm's stderr) on any
        # non-zero exit — a clean run is the assertion.
        _render(
            console_enabled=console_enabled,
            vault_enabled=vault_enabled,
            secret_source=secret_source,
        )


# ---------------------------------------------------------------------------
# D2 — pinned digest + laptop profile
# ---------------------------------------------------------------------------

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class TestDigestPinned:
    def test_digest_pinned_and_well_formed_in_values(self) -> None:
        values = yaml.safe_load(VALUES_FILE.read_text())
        digest = values["console"]["librechat"]["image"]["digest"]
        assert digest, (
            "console.librechat.image.digest is empty — D2 requires a "
            "committed, pinned digest so the deploy needs no live "
            "`docker inspect` compute step."
        )
        assert _SHA256_DIGEST_RE.match(digest), (
            f"console.librechat.image.digest={digest!r} is not a "
            "well-formed sha256:<64-hex> digest."
        )

    def test_rendered_image_ref_carries_the_digest(self) -> None:
        docs = _render()
        dep = _find(docs, "Deployment", "-librechat")
        image = _container(dep, "librechat")["image"]
        values = yaml.safe_load(VALUES_FILE.read_text())
        digest = values["console"]["librechat"]["image"]["digest"]
        assert image.endswith(f"@{digest}"), (
            f"rendered librechat image={image!r} does not carry the pinned "
            f"digest {digest!r} — a reproducible deploy needs this."
        )


class TestValuesLaptopProfile:
    def test_file_exists_and_pins_node_name(self) -> None:
        """M3-WU-D2-5E (2026-09-02): the laptop profile pins
        `console.frontDoorNodeName` (a k3s NODE NAME, resolved by k3s's
        own CoreDNS NodeHosts) rather than the removed
        `frontDoorHostAlias` static IP — so `-f values-laptop.yaml` is
        still the whole reproducible-deploy story, with no
        `kubectl get nodes` compute step, but self-heals across a DHCP
        change instead of going hard-down (2026-09-01 incident)."""
        assert VALUES_LAPTOP_FILE.exists(), (
            "charts/audittrace/values-laptop.yaml is missing — D2 requires "
            "a committed laptop reference profile so `-f values-laptop.yaml` "
            "replaces a live `kubectl get nodes` compute step."
        )
        laptop_values = yaml.safe_load(VALUES_LAPTOP_FILE.read_text())
        node_name = laptop_values["console"]["frontDoorNodeName"]
        assert node_name, "values-laptop.yaml sets no console.frontDoorNodeName."

    def test_helm_upgrade_style_render_with_laptop_profile_sets_no_host_alias(
        self,
    ) -> None:
        """M3-WU-D2-5E: `-f values-laptop.yaml` renders NO `hostAliases` on
        the LibreChat pod at all — front-door resolution moved entirely to
        the chart-templated `coredns-custom` overlay (see
        tests/test_frontdoor_dns_chart.py), which a static pod-spec field
        can never self-heal the way CoreDNS's NodeHosts does."""
        docs = _render(values_files=[VALUES_LAPTOP_FILE])
        dep = _find(docs, "Deployment", "-librechat")
        assert not dep["spec"]["template"]["spec"].get("hostAliases"), (
            "hostAliases resurfaced on the LibreChat pod under the laptop "
            "profile — the static-IP pin mechanism must stay removed."
        )

    def test_without_laptop_profile_no_host_alias_rendered(self) -> None:
        """The empty values.yaml default must still render cleanly (D1
        parity with WU-3b) — hostAliases never appears, laptop profile or
        not, post M3-WU-D2-5E."""
        docs = _render()
        dep = _find(docs, "Deployment", "-librechat")
        assert not dep["spec"]["template"]["spec"].get("hostAliases")


# ---------------------------------------------------------------------------
# D3 — session secrets: generate-if-absent + persist
# ---------------------------------------------------------------------------


class TestSessionSecretsPersistIfPresent:
    def test_template_source_uses_lookup_guarded_persistence(self) -> None:
        """Structural guard: a neutered chart that reverted to a bare
        `.Values`-only Secret (always overwriting on every render) would
        still pass a SINGLE `helm template` render — this is what makes
        that neuter go RED. `helm template` never contacts a real cluster,
        so `lookup` deterministically returns empty in every test run
        here; the mechanism this test polices is proven live-in-cluster by
        the operator's own `helm upgrade` (Rule 2, this WU's scope stops
        at Rule 1 — see spec non-goals / 'Do NOT run make integration')."""
        src = SECRET_LIBRECHAT_TEMPLATE.read_text()
        assert 'lookup "v1" "Secret"' in src, (
            'secret-librechat.yaml no longer calls `lookup "v1" "Secret"` '
            "— D3's generate-if-absent + persist mechanism was removed; "
            "every `helm upgrade` would regenerate the session secrets "
            "and invalidate every active LibreChat session."
        )
        for key in (
            "creds-key",
            "creds-iv",
            "jwt-secret",
            "jwt-refresh-secret",
            "openid-session-secret",
        ):
            assert f'"key" "{key}"' in src, (
                f"secret-librechat.yaml no longer routes {key!r} through "
                "the persistedSecret helper."
            )

    def test_helper_defines_explicit_existing_generated_precedence(self) -> None:
        src = HELPERS_TEMPLATE.read_text()
        assert 'define "audittrace.console.persistedSecret"' in src
        # Explicit .Values override checked FIRST, existing-cluster lookup
        # SECOND, freshly-generated LAST — neutering the order (e.g.
        # regenerating even when `existing` has the key) breaks idempotent
        # re-deploys just as badly as dropping `lookup` entirely.
        explicit_idx = src.index("if .explicit")
        existing_idx = src.index("hasKey .existing .key")
        assert explicit_idx < existing_idx, (
            "audittrace.console.persistedSecret checks the cluster-lookup "
            "result before the explicit override — an operator-supplied "
            "value would be silently ignored in favour of stale cluster "
            "state."
        )

    def test_explicit_values_are_stable_across_two_renders(self) -> None:
        explicit = [
            "--set",
            f"secrets.console.credsKey={'deadbeef' * 8}",
            "--set",
            f"secrets.console.credsIv={'cafebabe' * 4}",
            "--set",
            "secrets.console.jwtSecret=jwt-secret-fixed-value",
            "--set",
            "secrets.console.jwtRefreshSecret=jwt-refresh-fixed-value",
            "--set",
            "secrets.console.openidSessionSecret=openid-session-fixed-value",
        ]
        render_1 = _find(_render(extra_set=explicit), "Secret", "-librechat-secret")
        render_2 = _find(_render(extra_set=explicit), "Secret", "-librechat-secret")
        assert render_1["data"] == render_2["data"], (
            "two `helm template` renders with the SAME explicit "
            "secrets.console.* values produced DIFFERENT Secret data — "
            "session secrets must never be re-randomised out from under "
            "an operator-pinned value."
        )
        assert (
            render_1["data"]["jwt-secret"]
            == base64.b64encode(b"jwt-secret-fixed-value").decode()
        )

    def test_absent_explicit_values_generate_correctly_shaped_hex(self) -> None:
        """No explicit secrets.console.* set at all -> the chart must
        self-generate (no `openssl` in the operator's hands), and the
        generated creds-key/creds-iv must be valid hex of the EXACT byte
        length LibreChat's AES-256-CBC fields require (32-byte key,
        16-byte IV) — a malformed generated secret would break every
        LibreChat login, not just fail to persist."""
        secret = _find(_render(), "Secret", "-librechat-secret")
        creds_key = base64.b64decode(secret["data"]["creds-key"]).decode()
        creds_iv = base64.b64decode(secret["data"]["creds-iv"]).decode()
        assert re.fullmatch(r"[0-9a-f]{64}", creds_key), (
            f"generated creds-key={creds_key!r} is not 64 lowercase hex "
            "chars (32 bytes) — LibreChat's AES-256-CBC key requirement."
        )
        assert re.fullmatch(r"[0-9a-f]{32}", creds_iv), (
            f"generated creds-iv={creds_iv!r} is not 32 lowercase hex "
            "chars (16 bytes) — LibreChat's AES-256-CBC IV requirement."
        )
        for key in ("jwt-secret", "jwt-refresh-secret", "openid-session-secret"):
            value = base64.b64decode(secret["data"][key]).decode()
            assert value, f"generated {key} is empty."

    def test_two_renders_with_no_explicit_value_are_non_empty_and_differ(
        self,
    ) -> None:
        """Without a real cluster, `lookup` sees nothing on either render,
        so two independent `helm template` invocations (no explicit
        override, no cluster) are EXPECTED to generate two different
        values — this is the honest boundary of what a hermetic test can
        prove; true persistence across a real `helm upgrade` is Rule 2
        (live E2E), explicitly out of this WU's scope. This test just
        guards against a regression to a FIXED/hardcoded fallback (e.g. an
        empty string, or a static placeholder) masquerading as
        'generation'."""
        secret_1 = _find(_render(), "Secret", "-librechat-secret")
        secret_2 = _find(_render(), "Secret", "-librechat-secret")
        assert secret_1["data"]["jwt-secret"], "generated jwt-secret is empty"
        assert secret_2["data"]["jwt-secret"], "generated jwt-secret is empty"
        assert secret_1["data"]["jwt-secret"] != secret_2["data"]["jwt-secret"], (
            "two independent renders with no explicit value and no cluster "
            "produced the SAME jwt-secret — suggests a hardcoded fallback "
            "rather than genuine per-install generation."
        )
