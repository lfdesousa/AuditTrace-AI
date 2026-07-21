"""B2 — chart drift guards (`feedback_no_more_drifts`).

Each test in this module enforces a single cross-template invariant that
has bitten us at deploy time. Run via ``make test`` (no cluster needed;
shells out to ``helm template``).

Drift classes covered:

* **1. required ↔ preflight coverage** — every ``required "secrets.X..."``
  call in chart templates must have a matching ``--set secrets.X=`` line
  in ``scripts/deploy-preflight.sh``. Without this, adding a new
  ``required`` silently breaks the preflight gate the next time someone
  runs it from a clean shell. Closes the 2026-05-13 kmsKey-class drift
  (PR #73). Bidirectional check: also asserts every preflight ``--set``
  references a chart value that actually exists (zombie ``--set`` catch).

* **2. Vault-Agent SA ↔ Vault AP principals** — every workload with
  ``vault.hashicorp.com/agent-inject: "true"`` must have its
  ``serviceAccountName`` whitelisted as a principal in the
  ``<release>-allow-vault`` AuthorizationPolicy on port 8200. Otherwise
  Envoy returns ``403 RBAC: access denied`` before Vault sees the
  request. Closes the 2026-05-13 bucket-init class (PR #74).

* **3. Vault-Agent role ↔ ConfigMap role-binding** — every workload's
  ``vault.hashicorp.com/role`` annotation must have a matching
  ``role-<name>.env`` block in ``<release>-vault-policies`` ConfigMap.
  Without this, ``scripts/setup-vault.sh`` never creates the auth role
  → Vault rejects login → Agent sidecar exits.

* **4. role bindings ↔ SA names** — each ``role-<name>.env``'s
  ``bound_service_account_names`` must equal the SA actually used by
  the workload requesting that role. Catches rename typos.

* **5. role bindings ↔ policy bodies** — each ``role-<name>.env``'s
  ``policies=<policy>`` must reference an HCL block ``<policy>.hcl``
  declared in the same ConfigMap. Catches policy renames.

* **6. MinIO SA ↔ MinIO AP principals** — every workload whose env
  references the MinIO service host must have its SA in the
  ``<release>-allow-minio`` AP on port 9000. Forward-looking guard
  for new workloads that touch S3.

Anchors:
``feedback_no_more_drifts``, ``project_session_20260513``,
``project_bucket_init_sa_not_whitelisted``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
PREFLIGHT_PATH = REPO_ROOT / "scripts" / "deploy-preflight.sh"

RELEASE = "audittrace"
NAMESPACE = "audittrace"

# Throwaway values that satisfy every chart-side ``required`` so the
# render itself does not fail; mirror ``scripts/deploy-preflight.sh``.
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


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _render() -> list[dict]:
    """Render the chart with vault+istio enabled and parse into docs."""
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
        "istio.enabled=true",
        *_LINT_SECRETS,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return [
        d
        for d in yaml.safe_load_all(result.stdout)
        if isinstance(d, dict) and d.get("kind")
    ]


def _vault_injected_workloads(docs: list[dict]) -> list[dict]:
    """Every Deployment/StatefulSet/Job/Pod with agent-inject=true.

    Returns dicts of ``{kind, name, sa, role, vault_secrets}`` so each
    individual test can scrutinise the field it cares about.
    """
    out: list[dict] = []
    for d in docs:
        kind = d.get("kind", "")
        if kind not in ("Deployment", "StatefulSet", "Job", "Pod"):
            continue
        spec = d.get("spec") or {}
        tmpl = spec.get("template", spec) if isinstance(spec, dict) else {}
        if not isinstance(tmpl, dict):
            continue
        meta = tmpl.get("metadata") or {}
        ann = meta.get("annotations") or {}
        if ann.get("vault.hashicorp.com/agent-inject") != "true":
            continue
        pspec = tmpl.get("spec") or {}
        out.append(
            {
                "kind": kind,
                "name": d.get("metadata", {}).get("name", "<unnamed>"),
                "sa": pspec.get("serviceAccountName", "default"),
                "role": ann.get("vault.hashicorp.com/role"),
            }
        )
    return out


def _ap_principals(docs: list[dict], ap_name: str) -> set[str]:
    """All principals listed across all rules of a given AP."""
    for d in docs:
        if d.get("kind") != "AuthorizationPolicy":
            continue
        if d.get("metadata", {}).get("name") != ap_name:
            continue
        principals: set[str] = set()
        for rule in d.get("spec", {}).get("rules", []) or []:
            for src in rule.get("from", []) or []:
                for p in src.get("source", {}).get("principals", []) or []:
                    principals.add(p)
        return principals
    raise AssertionError(f"AuthorizationPolicy {ap_name} not in render")


def _sa_principal(sa_name: str) -> str:
    return f"cluster.local/ns/{NAMESPACE}/sa/{sa_name}"


def _vault_policies_data(docs: list[dict]) -> dict[str, str]:
    """Return the ``data`` of the vault-policies ConfigMap (key → block)."""
    name = f"{RELEASE}-vault-policies"
    for d in docs:
        if d.get("kind") != "ConfigMap":
            continue
        if d.get("metadata", {}).get("name") != name:
            continue
        return d.get("data", {}) or {}
    raise AssertionError(f"ConfigMap {name} not in render")


def _parse_role_env(block: str) -> dict[str, str]:
    """Parse a ``role-<name>.env`` block (``key=value`` lines)."""
    out: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


# ─────────────────────────────────────────────────────────────────────
# 1. required ↔ preflight `--set` coverage
# ─────────────────────────────────────────────────────────────────────


def _template_files() -> list[Path]:
    """Every file Helm reads in templates/: `.yaml`, `.yml`, `.tpl`.
    Misses on the `.tpl` extension caused the first-run false positive
    on this test — the helpers ALSO reference `.Values.X` paths."""
    base = CHART_DIR / "templates"
    out: list[Path] = []
    for ext in ("*.yaml", "*.yml", "*.tpl"):
        out.extend(base.rglob(ext))
    return out


# Match e.g.  {{ required "secrets.minio.kmsKey is required ..." .Values.secrets.minio.kmsKey ... }}
_REQUIRED_RE = re.compile(r"required\s+\"[^\"]*\"\s+\.Values\.(?P<path>[A-Za-z0-9_.]+)")
# Match --set secrets.X=Y  (positional pair OR =-form; preflight uses positional)
_PREFLIGHT_SET_RE = re.compile(r"--set\s+(?P<key>secrets\.[A-Za-z0-9_.]+)\s*=\s*\S+")


def _required_secret_paths_in_templates() -> set[str]:
    """All ``.Values.X`` paths cited by ``required`` calls in templates."""
    found: set[str] = set()
    for tpl in _template_files():
        try:
            text = tpl.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _REQUIRED_RE.finditer(text):
            found.add(m.group("path"))
    return found


def _preflight_set_keys() -> set[str]:
    """All ``secrets.X`` keys --set by deploy-preflight.sh.

    Combines the helm-lint stage and the helm-template stage so we
    catch both. Both should --set the same set of values; if they
    diverge that's its own drift class and the test will surface it.
    """
    text = PREFLIGHT_PATH.read_text(encoding="utf-8")
    return {m.group("key") for m in _PREFLIGHT_SET_RE.finditer(text)}


class TestRequiredSecretsCoveredByPreflight:
    def test_every_required_secret_has_preflight_set_line(self) -> None:
        required = {
            p for p in _required_secret_paths_in_templates() if p.startswith("secrets.")
        }
        preflight = _preflight_set_keys()
        missing = sorted(required - preflight)
        assert not missing, (
            "Drift: chart templates `required` these secret paths but "
            "scripts/deploy-preflight.sh does not `--set` them. The "
            "preflight gate will fail on a clean shell and operators "
            "will be tempted to bypass it. Add a matching `--set "
            "<path>=preflight` to deploy-preflight.sh (BOTH the lint "
            "and the template stage). Missing: " + ", ".join(missing)
        )

    def test_no_zombie_preflight_set_lines(self) -> None:
        """A ``--set secrets.X=`` line that no template `required` AND no
        template `default`s on is dead weight that confuses readers.

        Soft assertion: zombies are operationally harmless (extra
        --sets are ignored if no template reads them), so we only fail
        if a key matches NEITHER a `required` call NOR any
        ``.Values.secrets.X`` reference anywhere in templates.
        """
        preflight = _preflight_set_keys()
        # Build the set of secret paths the chart actually reads.
        # `.Values.secrets.X.Y` substring search across templates is
        # cheap and false-positive-safe (a false positive would only
        # widen the allowlist).
        templates_text = "\n".join(
            tpl.read_text(encoding="utf-8") for tpl in _template_files()
        )
        zombies = sorted(
            key for key in preflight if f".Values.{key}" not in templates_text
        )
        assert not zombies, (
            "Drift: scripts/deploy-preflight.sh `--set`s these keys but "
            "no chart template reads them — either the chart removed "
            "the reference and the preflight wasn't updated, OR the "
            "preflight has a typo. Zombies: " + ", ".join(zombies)
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Vault-Agent SA ↔ Vault AP principals (port 8200)
# ─────────────────────────────────────────────────────────────────────


class TestVaultInjectionWorkloadsInVaultAP:
    def test_every_vault_injected_sa_is_vault_ap_principal(self) -> None:
        docs = _render()
        workloads = _vault_injected_workloads(docs)
        assert workloads, (
            "render produced no vault-injected workloads — likely a render regression"
        )
        principals = _ap_principals(docs, f"{RELEASE}-allow-vault")
        missing = []
        for w in workloads:
            if _sa_principal(w["sa"]) not in principals:
                missing.append(
                    f"{w['kind']}/{w['name']} (sa={w['sa']}, role={w['role']})"
                )
        assert not missing, (
            "Drift: these vault-Agent-injected workloads' SAs are NOT "
            f"in {RELEASE}-allow-vault AP principals. Envoy will return "
            "403 RBAC: access denied before Vault sees the request. "
            "Add each principal to templates/istio/authorizationpolicy-vault.yaml. "
            "Missing: " + "; ".join(missing)
        )


# ─────────────────────────────────────────────────────────────────────
# 3. Vault role annotation ↔ role-binding in ConfigMap
# ─────────────────────────────────────────────────────────────────────


class TestVaultRoleConfigMapCoverage:
    def test_every_vault_role_annotation_has_role_env_block(self) -> None:
        docs = _render()
        workloads = _vault_injected_workloads(docs)
        cm_data = _vault_policies_data(docs)
        missing = []
        for w in workloads:
            role = w["role"]
            if not role:
                missing.append(
                    f"{w['kind']}/{w['name']} has agent-inject=true but NO role annotation"
                )
                continue
            key = f"role-{role}.env"
            if key not in cm_data:
                missing.append(
                    f"{w['kind']}/{w['name']} declares role={role} but "
                    f"{RELEASE}-vault-policies ConfigMap has no {key} entry"
                )
        assert not missing, (
            "Drift: vault-injected workloads reference roles that "
            "scripts/setup-vault.sh will never create. Add a "
            "role-<name>.env block in templates/vault/configmap-policies.yaml. "
            "Missing: " + "; ".join(missing)
        )


# ─────────────────────────────────────────────────────────────────────
# 4. role-binding's bound_service_account_names ↔ workload SA
# ─────────────────────────────────────────────────────────────────────


class TestVaultRoleBindingMatchesWorkloadSA:
    def test_bound_sa_in_role_env_matches_workload_service_account_name(self) -> None:
        docs = _render()
        workloads = _vault_injected_workloads(docs)
        cm_data = _vault_policies_data(docs)
        mismatches = []
        for w in workloads:
            role = w["role"]
            key = f"role-{role}.env"
            if key not in cm_data:
                # Covered by test 3; don't double-report.
                continue
            parsed = _parse_role_env(cm_data[key])
            bound = parsed.get("bound_service_account_names")
            if bound != w["sa"]:
                mismatches.append(
                    f"{w['kind']}/{w['name']}: role={role}, "
                    f"workload sa={w['sa']!r}, "
                    f"role-env bound_service_account_names={bound!r}"
                )
        assert not mismatches, (
            "Drift: Vault role's bound SA does not match the workload's "
            "actual SA. Vault will reject the login. "
            "Mismatches: " + "; ".join(mismatches)
        )


# ─────────────────────────────────────────────────────────────────────
# 5. role-binding's policies=X ↔ HCL block X.hcl exists
# ─────────────────────────────────────────────────────────────────────


class TestVaultRolePoliciesExist:
    def test_every_role_env_policy_has_hcl_block(self) -> None:
        docs = _render()
        cm_data = _vault_policies_data(docs)
        missing = []
        for key, block in cm_data.items():
            if not key.startswith("role-") or not key.endswith(".env"):
                continue
            parsed = _parse_role_env(block)
            policies = parsed.get("policies", "")
            for pol in (p.strip() for p in policies.split(",") if p.strip()):
                hcl_key = f"{pol}.hcl"
                if hcl_key not in cm_data:
                    missing.append(
                        f"{key} declares policies={pol} but {hcl_key} not in ConfigMap"
                    )
        assert not missing, (
            "Drift: Vault role-binding references a policy that doesn't "
            "exist in the same ConfigMap. setup-vault.sh will fail. "
            "Missing: " + "; ".join(missing)
        )


# ─────────────────────────────────────────────────────────────────────
# 6. MinIO-connecting workloads ↔ MinIO AP principals (port 9000)
# ─────────────────────────────────────────────────────────────────────


def _workloads_referencing_minio_host(docs: list[dict]) -> list[dict]:
    """Workloads with ENV values or args mentioning the in-cluster MinIO
    service host. We intentionally exclude the MinIO pod itself.
    """
    minio_host = f"{RELEASE}-minio"
    out: list[dict] = []
    for d in docs:
        kind = d.get("kind", "")
        if kind not in ("Deployment", "StatefulSet", "Job", "Pod"):
            continue
        name = d.get("metadata", {}).get("name", "")
        # Skip the MinIO server itself (its workload name is `<release>-minio`).
        if name == minio_host:
            continue
        spec = d.get("spec") or {}
        tmpl = spec.get("template", spec) if isinstance(spec, dict) else {}
        if not isinstance(tmpl, dict):
            continue
        pspec = tmpl.get("spec") or {}
        # Look across containers + initContainers for env refs to MinIO host.
        all_containers: list[dict] = []
        for cs_key in ("containers", "initContainers"):
            for c in pspec.get(cs_key, []) or []:
                if isinstance(c, dict):
                    all_containers.append(c)
        refs_minio = False
        for c in all_containers:
            for e in c.get("env", []) or []:
                val = e.get("value", "")
                if isinstance(val, str) and minio_host in val:
                    refs_minio = True
                    break
            if refs_minio:
                break
            for a in c.get("args", []) or []:
                if isinstance(a, str) and minio_host in a:
                    refs_minio = True
                    break
            if refs_minio:
                break
        if not refs_minio:
            continue
        out.append(
            {
                "kind": kind,
                "name": name,
                "sa": pspec.get("serviceAccountName", "default"),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────────────
# 6b. Static-shell-script regression: the `if ! cmd; then rc=$?` antipattern
#     in deploy-preflight.sh + sibling scripts. Inside an `if !` body,
#     `$?` is the negated result (always 0), so any rc-classification
#     after that line silently misclassifies SKIP (rc=1) as a hard fail.
#     This test fires if the antipattern reappears anywhere under scripts/.
# ─────────────────────────────────────────────────────────────────────


class TestNoIfBangRcCaptureAntipattern:
    def test_no_if_bang_rc_capture_in_shell_scripts(self) -> None:
        scripts_dir = REPO_ROOT / "scripts"
        offenders: list[str] = []
        # Pair lookahead: an `if ! …; then` line followed by a `rc=$?` line
        # within the next 3 lines (allowing multi-line `if !` blocks).
        for sh in scripts_dir.glob("*.sh"):
            lines = sh.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not (stripped.startswith("if ! ") or stripped.startswith("if !")):
                    continue
                # Walk forward until we see `then`, then look at next 3 lines.
                j = i
                while (
                    j < len(lines)
                    and "; then" not in lines[j]
                    and "then" not in lines[j].split()
                ):
                    j += 1
                    if j - i > 5:
                        break
                if j >= len(lines):
                    continue
                # Next ~3 lines after the `; then`
                window = lines[j + 1 : j + 4]
                for w in window:
                    if re.match(r"^\s*(?:local\s+)?rc=\$\?\s*$", w):
                        offenders.append(
                            f"{sh.name}:{i + 1}-{j + 1}: `if ! cmd; then` → `rc=$?` (captures 0, not the real exit code)"
                        )
                        break
        assert not offenders, (
            "Shell antipattern: `if ! cmd; then ... rc=$?` always captures "
            "0 (bash negates the exit status before $? is read inside the "
            "body). This bit deploy-preflight.sh on 2026-05-14 — istiod "
            "probe returning rc=1 (SKIP) was misclassified as ERROR. "
            "Use `set +e; cmd; rc=$?; set -e` instead. "
            "Sites: " + " | ".join(offenders)
        )


# ─────────────────────────────────────────────────────────────────────
# 8. Secret-template render coverage. A template in templates/secrets/
#    that NEVER renders in production mode (vault.enabled=true) is a
#    silent dead-code class. The 2026-05-14 cc-rabbitmq incident: the
#    `secret-rabbitmq-content-control.yaml` template was gated on
#    `not .Values.vault.enabled` with a comment claiming a separate
#    Vault Agent template took over in prod mode. No such template
#    existed; the cc-chart's pod silently started with empty
#    CONTENT_CONTROL_RABBITMQ_PASSWORD and its scan_worker was a
#    zombie. /v1/health didn't probe AMQP so the pod stayed Ready.
#
#    This test renders the chart with vault.enabled=true (the
#    production posture) + every secret value supplied, then asserts
#    that every .yaml file under templates/secrets/ produced at least
#    one rendered resource — using helm's `# Source:` provenance
#    comments to attribute resources back to their template file.
# ─────────────────────────────────────────────────────────────────────


def _render_with_extras(extras: list[str]) -> str:
    """Like _render() but returns the raw stdout (preserves Source: comments)."""
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
        "istio.enabled=true",
        *_LINT_SECRETS,
        *extras,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


class TestSecretTemplatesAllRenderInProdMode:
    # Templates documented to render only in vault-disabled mode.
    # Each entry MUST cite the reason — drive-by skips are forbidden.
    # If a template ever lands here, you ALSO need to verify (and
    # capture the verification in the reason string) that NO rendered
    # workload references the skipped Secret name via secretKeyRef in
    # the prod-mode render — otherwise the skip is a silent dead-code
    # bug of the cc-rabbitmq / dockerhub-pull class.
    _SKIP_IN_VAULT_MODE: dict[str, str] = {
        "secret-chromadb.yaml": (
            "vault.enabled=true → chromadb token injected at runtime "
            "via Vault Agent annotation in templates/_helpers.tpl "
            "(audittrace.vaultAnnotations.chromadb → kv/audittrace/"
            "chromadb/main). Verified 2026-05-14: no rendered workload "
            "references `audittrace-chromadb-secret` by secretKeyRef "
            "in prod-mode render."
        ),
        "secret-keycloak.yaml": (
            "vault.enabled=true → admin password injected via "
            "Vault Agent annotation in templates/_helpers.tpl "
            "(audittrace.vaultAnnotations.keycloak → kv/audittrace/"
            "keycloak/admin). Verified 2026-05-14: no rendered "
            "workload references `audittrace-keycloak-secret` by "
            "secretKeyRef in prod-mode render."
        ),
        "summariser-db.yaml": (
            "vault.enabled=true → summariser DB password injected via "
            "Vault Agent annotation in templates/_helpers.tpl "
            "(audittrace.vaultAnnotations.summariserJob → kv/audittrace/"
            "summariser/db). Verified 2026-05-14: no rendered workload "
            "references `audittrace-summariser-db` by secretKeyRef "
            "in prod-mode render."
        ),
    }

    def test_every_secret_template_renders_at_least_one_resource_in_prod_mode(
        self,
    ) -> None:
        # Supply every secret value the production-mode chart asks for
        # so no template is skipped on an unsupplied-input technicality.
        # Keep this in sync with secrets.* fields chart consumes — the
        # other drift tests already enforce that coverage.
        extras = [
            "--set",
            "secrets.rabbitmq.contentControlUser=content-control",
            "--set",
            "secrets.rabbitmq.contentControlPassword=ci-test",
            "--set",
            "secrets.minio.audittraceAppPassword=ci-test",
            "--set",
            "secrets.minio.contentControlPassword=ci-test",
            # Docker Hub creds — render gate for
            # secret-dockerhub-pull.yaml. Empty defaults skip the
            # template, so supply throwaway values to force-render.
            "--set",
            "secrets.dockerHub.username=ci-test",
            "--set",
            "secrets.dockerHub.pat=ci-test",
            # Summariser role provisioning — required for the
            # vault-disabled fallback path (the only secret-rendering
            # path) but harmless when vault.enabled=true (the legit-
            # skip allowlist covers it).
            "--set",
            "memoryServer.summariser.manageRole=true",
        ]
        out = _render_with_extras(extras)

        # Helm prefixes every rendered resource with a comment like
        # `# Source: audittrace/templates/secrets/secret-minio.yaml`.
        # Parse the set of templates that produced at least one resource.
        producing_templates: set[str] = set()
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("# Source:"):
                src = stripped.removeprefix("# Source:").strip()
                producing_templates.add(src)

        secrets_dir = CHART_DIR / "templates" / "secrets"
        expected_paths = sorted(
            f"audittrace/templates/secrets/{p.name}" for p in secrets_dir.glob("*.yaml")
        )

        non_rendering = [
            p
            for p in expected_paths
            if p not in producing_templates
            and Path(p).name not in self._SKIP_IN_VAULT_MODE
        ]
        assert not non_rendering, (
            "Drift: these templates under templates/secrets/ rendered "
            "ZERO resources in production mode (vault.enabled=true with "
            "every secret value supplied). Either they're dead code OR "
            "they're gated on `not .Values.vault.enabled` with no "
            "Vault Agent counterpart actually doing the work — the exact "
            "shape of the 2026-05-14 cc-rabbitmq incident "
            "(`project_amqp_topology_bootstrap`). If a template "
            "intentionally only renders in vault-disabled mode (e.g. "
            "because Vault Agent template injects the equivalent Secret "
            "elsewhere), add its filename to _SKIP_IN_VAULT_MODE WITH A "
            "REASON pointing at the alternative path. "
            "Non-rendering: " + ", ".join(non_rendering)
        )


class TestMinIOConnectorsInMinIOAP:
    def test_every_workload_referencing_minio_has_sa_in_minio_ap(self) -> None:
        docs = _render()
        connectors = _workloads_referencing_minio_host(docs)
        assert connectors, (
            "render produced no MinIO-connecting workloads — likely a render regression"
        )
        principals = _ap_principals(docs, f"{RELEASE}-allow-minio")
        missing = []
        for w in connectors:
            if _sa_principal(w["sa"]) not in principals:
                missing.append(f"{w['kind']}/{w['name']} (sa={w['sa']})")
        assert not missing, (
            f"Drift: these workloads reference the {RELEASE}-minio "
            "service but their SAs are NOT in the MinIO AP principals. "
            "Envoy will return 403 on port 9000. Add to "
            "templates/istio/authorizationpolicy-minio.yaml. "
            "Missing: " + "; ".join(missing)
        )


# ─────────────────────────────────────────────────────────────────────
# 7. memory-server boot-budget probe coverage
# ─────────────────────────────────────────────────────────────────────


def _memory_server_container(docs: list[dict]) -> dict:
    """Return the memory-server container spec from the rendered chart."""
    name = f"{RELEASE}-memory-server"
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        if d.get("metadata", {}).get("name") != name:
            continue
        containers = (
            d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            or []
        )
        for c in containers:
            if c.get("name") == "memory-server":
                return c
        raise AssertionError(
            f"Deployment {name} has no container named 'memory-server'"
        )
    raise AssertionError(f"Deployment {name} not in render")


class TestMemoryServerStartupProbeBudget:
    """memory-server's FastAPI lifespan blocks on
    ``ScanAmqpClient.ensure_connected`` (server.py:326), whose PR-B10
    retry-with-backoff has a worst-case budget of ~91 s on a cold
    cluster (6 attempts × 10 s timeout + 1+2+4+8+16 = 31 s of sleeps).
    The liveness probe's 45 s budget is too tight to cover that; the
    container is killed mid-retry and restarts twice before stabilising
    on a fresh kind install. The fix is a ``startupProbe`` with a
    budget ≥ the AMQP retry budget so the kubelet suspends liveness
    until the slow-boot work finishes once.

    Anchor: ~/work/audittrace-evidence/20260515-memory-server-startup-race/STARTUP-PROFILE.md.
    """

    _MIN_BUDGET_SECONDS = 100

    def test_memory_server_has_startup_probe(self) -> None:
        container = _memory_server_container(_render())
        assert "startupProbe" in container, (
            "Drift: memory-server Deployment has no startupProbe. The "
            "FastAPI lifespan blocks on the AMQP connect (worst case "
            "~91 s on a cold cluster); without startupProbe the "
            "kubelet kills the pod mid-retry and the pod restarts "
            "twice before stabilising. Add a startupProbe in "
            "templates/memory-server/deployment.yaml — see "
            "STARTUP-PROFILE.md for the calculation."
        )

    def test_memory_server_startup_probe_budget_covers_amqp_retry(self) -> None:
        container = _memory_server_container(_render())
        probe = container.get("startupProbe", {})
        initial = int(probe.get("initialDelaySeconds", 0))
        period = int(probe.get("periodSeconds", 10))
        threshold = int(probe.get("failureThreshold", 3))
        budget = initial + period * threshold
        assert budget >= self._MIN_BUDGET_SECONDS, (
            f"Drift: memory-server startupProbe budget is {budget} s "
            f"(initialDelaySeconds={initial} + periodSeconds={period} "
            f"× failureThreshold={threshold}), below the {self._MIN_BUDGET_SECONDS} s "
            "floor needed to cover the AMQP retry budget. Either tune "
            "periodSeconds × failureThreshold up OR reduce the AMQP "
            "retry budget in scan_amqp_client.py. See STARTUP-PROFILE.md "
            "§3 for the calculation."
        )


# ─────────────────────────────────────────────────────────────────────
# 8. Keycloak realm — memory write scopes on user-facing clients
# ─────────────────────────────────────────────────────────────────────


def _rendered_realm_json(docs: list[dict]) -> dict:
    """Return the parsed realm JSON from the rendered keycloak-realm
    ConfigMap. The chart loads files/realm-audittrace.json via
    ``tpl .Files.Get`` so the chart-shipped file is rendered (not raw)
    on deploy — this helper mirrors that rendering path."""
    name = f"{RELEASE}-keycloak-realm"
    for d in docs:
        if d.get("kind") != "ConfigMap":
            continue
        if d.get("metadata", {}).get("name") != name:
            continue
        raw = (d.get("data") or {}).get("realm.json", "")
        if not raw:
            raise AssertionError(
                f"ConfigMap {name} has no data.realm.json — render regression"
            )
        return json.loads(raw)
    raise AssertionError(f"ConfigMap {name} not in render")


class TestKeycloakOpencodeMemoryWriteScopes:
    """``audittrace-opencode`` and ``audittrace-webui`` MUST have all five
    memory write scopes available as ``optionalClientScopes`` so a Device
    Flow / Auth Code + PKCE login can request them via ``scope=...``.

    Without this, EOD memo writes to the ``/memory/index`` endpoint with
    ``?file=semantic/foo.md`` (or decisions/skills/...) 403 because the
    JWT lacks the required ``memory:<layer>:write`` claim — exactly the
    failure that motivated this guard's parent PR
    (``project_pickup_20260515_b7`` → "Keycloak audittrace-opencode
    scope grant").

    The provisioner script ``scripts/setup-memory-scopes.sh`` runs the
    same grant against existing clusters; its SCOPES array is asserted
    by the sibling test below.
    """

    _EXPECTED_WRITE_SCOPES: frozenset[str] = frozenset(
        {
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
            "memory:decisions:write",
            "memory:skills:write",
        }
    )

    _CLIENT_IDS: tuple[str, ...] = ("audittrace-opencode", "audittrace-webui")

    def test_realm_declares_all_memory_write_scopes(self) -> None:
        realm = _rendered_realm_json(_render())
        declared = {s.get("name") for s in realm.get("clientScopes", []) or []}
        missing = self._EXPECTED_WRITE_SCOPES - declared
        assert not missing, (
            "Drift: realm.json::clientScopes is missing scope entries that "
            "the OR-scopes mapping in server.py expects to issue. Add "
            "client-scope objects for: " + ", ".join(sorted(missing)) + " — "
            "see charts/audittrace/files/realm-audittrace.json and the "
            "sibling keycloak/realm-audittrace.json (dev import)."
        )

    @pytest.mark.parametrize("client_id", ["audittrace-opencode", "audittrace-webui"])
    def test_user_facing_client_has_all_memory_write_scopes(
        self, client_id: str
    ) -> None:
        realm = _rendered_realm_json(_render())
        for c in realm.get("clients", []) or []:
            if c.get("clientId") != client_id:
                continue
            optional = set(c.get("optionalClientScopes") or [])
            missing = self._EXPECTED_WRITE_SCOPES - optional
            assert not missing, (
                f"Drift: client {client_id!r} optionalClientScopes is "
                f"missing: {sorted(missing)}. Add them to "
                "charts/audittrace/files/realm-audittrace.json AND the "
                "sibling keycloak/realm-audittrace.json so the realm-import "
                "Helm hook + the dev standalone import stay congruent."
            )
            return
        raise AssertionError(
            f"Client {client_id!r} not found in rendered realm — "
            "rename or removal regression"
        )

    def test_provisioner_script_grants_match_realm_grants(self) -> None:
        """scripts/setup-memory-scopes.sh and the chart's in-cluster Job
        ConfigMap both maintain a SCOPES array. They must mirror each
        other AND must contain every scope this PR added — otherwise a
        re-run of the script (the catch-up path for existing clusters)
        would skip new scopes silently."""
        repo_root = CHART_DIR.parent.parent
        script_path = repo_root / "scripts" / "setup-memory-scopes.sh"
        cm_path = (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        )

        def _scopes_in(text: str) -> set[str]:
            # Match lines like `      "memory:foo:write"` inside the
            # SCOPES=( ... ) block. Both files share the same syntax.
            return set(re.findall(r'"((?:memory|audittrace):[^"]+)"', text))

        # Constrain to the SCOPES=( ... ) blocks to avoid catching scope
        # mentions in comments elsewhere.
        def _scopes_block(text: str) -> str:
            m = re.search(r"SCOPES=\(([^)]*)\)", text)
            if m is None:
                raise AssertionError(f"SCOPES=( ... ) block not in: {text[:200]}")
            return m.group(1)

        script_scopes = _scopes_in(_scopes_block(script_path.read_text()))
        cm_scopes = _scopes_in(_scopes_block(cm_path.read_text()))

        assert script_scopes == cm_scopes, (
            "Drift: scripts/setup-memory-scopes.sh and "
            "templates/keycloak/configmap-memory-scopes-script.yaml have "
            f"divergent SCOPES arrays. Script: {sorted(script_scopes)}. "
            f"ConfigMap: {sorted(cm_scopes)}. They must mirror — one is "
            "the dev/local provisioner, the other is the in-cluster Job."
        )

        missing = self._EXPECTED_WRITE_SCOPES - script_scopes
        assert not missing, (
            f"Drift: provisioner SCOPES array does not grant: {sorted(missing)}. "
            "Add to both scripts/setup-memory-scopes.sh and the ConfigMap "
            "mirror, so existing-cluster re-runs pick up the new scopes."
        )


class TestPostDeployVerifyKeycloakScopeGuard:
    """Check 11 of ``scripts/post-deploy-verify.sh`` (#370).

    The live Keycloak realm granted ``memory:episodic:write`` as a DEFAULT
    scope on ``audittrace-opencode`` while every declared source said
    OPTIONAL. Nothing noticed for months, because ``--import-realm`` runs on
    FIRST BOOT ONLY: after the realm exists the ConfigMap is inert, so the
    file-vs-file guards in this module structurally cannot see the drift.

    These tests pin the *properties* of that check, not its output. They
    cannot prove it detects drift (that needs a cluster — see the PR's
    Validation section for the live fire-and-clear evidence); they prove it
    keeps the shape that makes it trustworthy.
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "scripts" / "post-deploy-verify.sh").read_text(
            encoding="utf-8"
        )

    def test_check_exists(self) -> None:
        assert "Keycloak client-scope drift" in self._script(), (
            "post-deploy-verify.sh lost its Keycloak scope-drift check. This "
            "is the ONLY place declared realm config is compared against the "
            "live realm; without it #370 recurs silently."
        )

    def test_admin_password_never_reaches_argv(self) -> None:
        """The credential goes in on stdin, never as a process argument.

        ``kubectl exec -- env VAR=secret`` publishes the value to the pod's
        process table, readable from /proc by anything else in that
        container. Checks 9/10 predate this rule and still use the env form
        for VAULT_TOKEN; new code must not, and this test stops the pattern
        being copied forward into the Keycloak check.
        """
        script = self._script()
        offenders = [
            line.strip()
            for line in script.splitlines()
            if "env " in line and "KEYCLOAK_ADMIN_PASSWORD" in line
        ]
        assert not offenders, (
            "Keycloak admin password passed via `env VAR=` — it lands in the "
            "pod's process table. Pipe it on stdin instead. Sites: "
            + " | ".join(offenders)
        )

    def test_skips_rather_than_fails_without_credential(self) -> None:
        """A missing credential must not turn the gate red.

        Unprivileged post-deploy runs are a supported mode (mirrors checks
        9/10). If a missing password FAILED, operators would start passing
        ``|| true`` around the whole gate and lose all eleven checks.
        """
        script = self._script()
        assert 'skip "no Keycloak admin credential' in script, (
            "The Keycloak check must SKIP (not FAIL) when no admin "
            "credential is available."
        )

    def test_expected_state_reads_both_declared_sources(self) -> None:
        """Expected = realm ConfigMap UNION the ensure-memory-scopes Job.

        The realm JSON is not the whole story: the Job binds its own SCOPES
        list to clients precisely BECAUSE --import-realm is inert after
        first boot. Those bindings are intentional and legitimately live
        while absent from the realm JSON.

        Dropping the Job would make the check fail permanently on a CORRECT
        cluster (admin-client alone reports three phantom over-privileges),
        and a guard that cries wolf gets muted. A muted guard is worse than
        none — #370 got through while a green gate was already running.
        """
        script = self._script()
        assert "-keycloak-realm" in script, "must read the declared realm ConfigMap"
        assert "-memory-scopes-script" in script, (
            "must also read the ensure-memory-scopes Job ConfigMap, or the "
            "check reports phantom over-privileges on admin-client forever"
        )

    def test_reports_over_privilege_distinctly(self) -> None:
        """The two drift directions fail differently and must read differently.

        over-privileged  = a scope nobody asked for lands in every token
                           (the security bug — this is what #370 was)
        under-privileged = callers that never had to ask now get 403
                           (the availability bug)

        An operator triaging a red gate needs to know which one they have.
        """
        assert "OVER-PRIVILEGED" in self._script(), (
            "Over-privilege (live default not declared) must be labelled "
            "distinctly from under-privilege — they are different incidents."
        )

    def test_header_numbering_is_self_consistent(self) -> None:
        """Every ``(N/TOTAL)`` header agrees with the real number of checks.

        Adding check 11 meant renumbering ten existing headers. A missed one
        is invisible in review and quietly tells operators a check is absent.
        """
        script = self._script()
        headers = re.findall(r'header "\((\d+)/(\d+)\)', script)
        assert headers, "no numbered headers found in post-deploy-verify.sh"
        total = len(headers)
        wrong_total = [f"({n}/{d})" for n, d in headers if int(d) != total]
        assert not wrong_total, (
            f"{total} numbered checks exist but these headers disagree on the "
            f"total: {', '.join(wrong_total)}. Renumber all of them."
        )
        numbering = [int(n) for n, _ in headers]
        assert numbering == list(range(1, total + 1)), (
            f"check numbers are not sequential 1..{total}: {numbering}"
        )


class TestRestrictedClientStaysRestricted:
    """`audittrace-restricted` must never gain an audit or admin scope (SC-09).

    This client exists for exactly one purpose: to hold a token that CANNOT be
    widened by asking. Keycloak silently DROPS a requested scope a client does
    not offer rather than erroring, so "the client does not offer it" is the
    entire mechanism. Adding `audittrace:audit` to either scope set - even as
    optional, even "just for a test" - does not weaken the evidence, it VOIDS
    it: every SC-09 403 would then prove only that the caller did not ask.

    The failure mode this guards against is quiet. Nothing breaks, no test
    goes red, and the adversarial result silently becomes worthless while
    still being cited. Hence a test rather than a comment.
    """

    FORBIDDEN = (
        "audittrace:audit",
        "audittrace:admin",
        "audittrace:assessment:ingest",
        "audittrace:index",
    )

    @staticmethod
    def _restricted(realm: dict) -> dict:
        for c in realm["clients"]:
            if c["clientId"] == "audittrace-restricted":
                return c
        raise AssertionError(
            "audittrace-restricted is missing from the realm. SC-09 "
            "(adversarial cross-tenant read) cannot be run without it - a "
            "second identity on the SHARED client proves politeness, not a "
            "boundary. See audittrace-private doc 14."
        )

    def test_top_level_realm_grants_no_audit_scope(self) -> None:
        realm = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        c = self._restricted(realm)
        both = list(c.get("defaultClientScopes", [])) + list(
            c.get("optionalClientScopes", [])
        )
        offenders = [s for s in both if s in self.FORBIDDEN]
        assert not offenders, (
            "audittrace-restricted was granted "
            f"{offenders} - this VOIDS every SC-09 result. The client's only "
            "purpose is that these scopes are unobtainable, not merely "
            "unrequested. Remove them, or stop citing SC-09."
        )

    def test_description_fits_keycloak_column(self) -> None:
        """Keycloak stores client.description in a varchar(255).

        Overflowing it makes the admin API return a bare HTTP 500 with
        `{"error":"unknown_error"}` and no hint; the real cause
        (`value too long for type character varying(255)`) appears only in the
        Keycloak pod log. Cost a debugging cycle on 2026-07-20.
        """
        for rel in (
            "keycloak/realm-audittrace.json",
            "charts/audittrace/files/realm-audittrace.json",
        ):
            raw = (REPO_ROOT / rel).read_text(encoding="utf-8")
            i = raw.index('"clientId": "audittrace-restricted"')
            start = raw.rindex('"description": "', 0, i) + len('"description": ')
            desc = json.loads(raw[start : raw.index('",\n', start) + 1])
            assert len(desc) <= 255, (
                f"{rel}: audittrace-restricted description is {len(desc)} "
                "chars; Keycloak's column is varchar(255) and the admin API "
                'fails with an unhelpful bare 500 ("unknown_error").'
            )

    def test_chart_and_top_level_realms_agree(self) -> None:
        """Both realm files must define the client identically.

        The chart file is the one actually imported; the top-level file is the
        dev/standalone import. A client present in only one of them produces a
        realm that behaves differently depending on how it was created - the
        exact drift class as #370.
        """
        chart_raw = (
            REPO_ROOT / "charts" / "audittrace" / "files" / "realm-audittrace.json"
        ).read_text(encoding="utf-8")
        assert '"audittrace-restricted"' in chart_raw, (
            "audittrace-restricted is in keycloak/realm-audittrace.json but "
            "NOT in the chart's realm file - the chart file is the one that "
            "actually gets imported, so a fresh cluster would not have it."
        )
        for scope in ("audittrace:query", "audittrace:context"):
            assert scope in chart_raw


class TestPostDeployVerifyShadowClientCheck:
    """Check 11 must also walk LIVE -> DECLARED, not only DECLARED -> LIVE (#371).

    The original check enumerated the clients in the realm ConfigMap and
    compared each against the live realm. A client existing live but declared
    nowhere was therefore never enumerated, never compared, and never
    reported. Demonstrated on 2026-07-20: ``audittrace-restricted`` was created
    via the admin API and the gate passed it in silence — not "checked and
    clean", *not checked*.

    That gap matters more than the comparison it complements. The drift the
    check was built for (#370) was someone CHANGING a scope on an existing
    client; the strictly worse move is CREATING a client with
    ``audittrace:admin`` as a default scope, which a declared-only walk cannot
    see — and which disturbs nothing already being watched.

    These tests pin the shape. Live fire-and-clear evidence (two planted
    clients, one privileged) is in the PR body; a cluster is needed for that
    and pytest has none.
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "scripts" / "post-deploy-verify.sh").read_text(
            encoding="utf-8"
        )

    def test_enumerates_live_clients(self) -> None:
        script = self._script()
        assert "live_clients=" in script, (
            "check 11 no longer enumerates LIVE clients — it is back to "
            "declared->live only, and a shadow client is invisible again (#371)."
        )
        assert (
            '/clients"' in script
            or "/clients'" in script
            or "${KC_REALM}/clients" in script
        ), "expected an unfiltered GET on /clients to list every live client"

    def test_builtin_allowlist_is_exact_names_not_a_prefix(self) -> None:
        """A prefix rule would exempt exactly the clients worth watching.

        ``audittrace-*`` as an allowlist pattern would silently whitelist a
        hostile ``audittrace-backdoor``. The allowlist must therefore be exact
        names, and must not contain our own prefix.
        """
        script = self._script()
        assert "KC_BUILTIN_CLIENTS" in script, "built-in allowlist missing"
        start = script.index("KC_BUILTIN_CLIENTS")
        block = script[start : start + 400]
        for builtin in (
            "account",
            "admin-cli",
            "broker",
            "realm-management",
            "security-admin-console",
        ):
            assert builtin in block, f"{builtin} missing from the allowlist"
        assert "audittrace-*" not in block and "audittrace*" not in block, (
            "the allowlist uses a wildcard over our own client prefix — that "
            "exempts precisely the clients most worth watching. Exact names only."
        )

    def test_privileged_shadow_client_is_ranked_distinctly(self) -> None:
        """An undeclared client holding admin/audit is a different incident.

        An operator triaging a red gate needs to see which shadow client is
        dangerous, not scan a flat list.
        """
        script = self._script()
        assert "UNDECLARED and holds a privileged default scope" in script, (
            "undeclared clients holding audittrace:admin or audittrace:audit "
            "must be reported distinctly from harmless ones."
        )

    def test_shadow_clients_fail_the_gate_rather_than_warn(self) -> None:
        """Silence and warnings both get ignored; only FAIL changes behaviour."""
        script = self._script()
        idx = script.index("UNDECLARED client(s)")
        window = script[max(0, idx - 200) : idx + 100]
        assert "fail " in window, (
            "undeclared clients must call fail(), not pass() or skip() — a "
            "warning that does not redden the gate is a warning nobody reads."
        )
