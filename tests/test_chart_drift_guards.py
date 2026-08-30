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


class TestConsoleMemoryProxyScopeGovernance:
    """M3-WU-D2-1 — the Souvenirs panel's memory-proxy write scopes
    (``memory:{episodic,procedural,semantic}:write``) reach
    ``audittrace-librechat`` ONLY as OPTIONAL scopes, via a dedicated
    ``MEMORY_CONSOLE_WRITE_SCOPES`` array + bind loop kept separate from
    the plain ``SCOPES`` fan-out (which also carries ``audittrace:admin``
    and ``audittrace:assessment:ingest`` — see
    ``TestKeycloakOpencodeMemoryWriteScopes``). This governance class is
    the WU-D2-1 sibling of ``TestCorpusScopeGovernance``.

    Falsifiable:

    * ``scripts/setup-memory-scopes.sh`` and the chart's in-cluster Job
      ConfigMap declaring divergent/incomplete
      ``MEMORY_CONSOLE_WRITE_SCOPES`` arrays fails
      ``test_provisioner_arrays_match_and_exact``;
    * either provisioner's dedicated bind loop targeting a client other
      than ``audittrace-librechat`` (e.g. silently widening to
      ``audittrace-opencode``/``audittrace-webui``) fails
      ``test_bind_loop_targets_only_librechat``;
    * ``audittrace:admin`` appearing in either provisioner's
      ``MEMORY_CONSOLE_WRITE_SCOPES`` array fails
      ``test_never_admin_in_console_write_scopes`` — the spec's
      non-negotiable "console never hard-deletes the audit trail"
      invariant, enforced at the provisioner level (the realm-level
      sibling is ``TestLibrechatConsoleClient.test_no_admin_scope_granted``).
    """

    _EXPECTED_CONSOLE_WRITE_SCOPES: frozenset[str] = frozenset(
        {
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }
    )

    _OTHER_END_USER_CLIENTS: tuple[str, ...] = (
        "audittrace-opencode",
        "audittrace-webui",
    )

    @staticmethod
    def _console_write_scopes_in(text: str) -> set[str]:
        m = re.search(r"MEMORY_CONSOLE_WRITE_SCOPES=\(([^)]*)\)", text)
        if m is None:
            raise AssertionError(
                "MEMORY_CONSOLE_WRITE_SCOPES=( ... ) block not found — "
                "M3-WU-D2-1 requires a dedicated array, separate from SCOPES."
            )
        return set(re.findall(r'"(memory:[^"]+)"', m.group(1)))

    @staticmethod
    def _console_bind_loop_body(text: str) -> str:
        m = re.search(
            r'for SCOPE in "\$\{MEMORY_CONSOLE_WRITE_SCOPES\[@\]\}"; do(.*?)\bdone\b',
            text,
            re.S,
        )
        if m is None:
            raise AssertionError(
                "MEMORY_CONSOLE_WRITE_SCOPES bind loop (`for SCOPE in "
                '"${MEMORY_CONSOLE_WRITE_SCOPES[@]}"; do ... done`) not '
                "found — M3-WU-D2-1 requires a bind loop scoped to "
                "audittrace-librechat only, separate from the CLIENT_KIND "
                "fan-out."
            )
        return m.group(1)

    def test_provisioner_arrays_match_and_exact(self) -> None:
        repo_root = CHART_DIR.parent.parent
        script_path = repo_root / "scripts" / "setup-memory-scopes.sh"
        cm_path = (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        )
        script_scopes = self._console_write_scopes_in(script_path.read_text())
        cm_scopes = self._console_write_scopes_in(cm_path.read_text())

        assert script_scopes == cm_scopes == self._EXPECTED_CONSOLE_WRITE_SCOPES, (
            "Drift: scripts/setup-memory-scopes.sh and "
            "templates/keycloak/configmap-memory-scopes-script.yaml have "
            f"divergent/incomplete MEMORY_CONSOLE_WRITE_SCOPES arrays. "
            f"Script: {sorted(script_scopes)}. ConfigMap: {sorted(cm_scopes)}. "
            f"Expected: {sorted(self._EXPECTED_CONSOLE_WRITE_SCOPES)}."
        )

    def test_bind_loop_targets_only_librechat(self) -> None:
        repo_root = CHART_DIR.parent.parent
        script_path = repo_root / "scripts" / "setup-memory-scopes.sh"
        cm_path = (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        )
        for path, label in ((script_path, "script"), (cm_path, "configmap")):
            loop_body = self._console_bind_loop_body(path.read_text())
            assert "audittrace-librechat" in loop_body, (
                f"{label}: the MEMORY_CONSOLE_WRITE_SCOPES bind loop does "
                "not bind to audittrace-librechat."
            )
            for forbidden_client in self._OTHER_END_USER_CLIENTS:
                assert forbidden_client not in loop_body, (
                    f"{label}: the MEMORY_CONSOLE_WRITE_SCOPES bind loop "
                    f"references {forbidden_client!r} — M3-WU-D2-1 scopes "
                    "the write grant to audittrace-librechat only."
                )

    def test_never_admin_in_console_write_scopes(self) -> None:
        assert "audittrace:admin" not in self._EXPECTED_CONSOLE_WRITE_SCOPES
        repo_root = CHART_DIR.parent.parent
        script_path = repo_root / "scripts" / "setup-memory-scopes.sh"
        cm_path = (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        )
        for path, label in ((script_path, "script"), (cm_path, "configmap")):
            scopes = self._console_write_scopes_in(path.read_text())
            assert "audittrace:admin" not in scopes, (
                f"{label}: MEMORY_CONSOLE_WRITE_SCOPES illegally carries "
                "audittrace:admin."
            )


class TestCorpusScopeGovernance:
    """ADR-062 WU-A2/A3 — granular ``memory:corpus:<collection>:{read,write}``
    scopes for Layer 5 (the Shared Corpus), one read/write pair per recall
    collection (``decisions``, ``skills``, ``semantic``).

    Write (and, in this Phase-A pass, read too — there is no Curator client
    yet) is operator/curator-tier: optional-only on ``admin-client``, never
    granted (default or optional) to end-user clients (``audittrace-opencode``,
    ``audittrace-webui``) or to the reserved SC-09 ``audittrace-restricted``
    client (see the sibling guard added to
    ``TestRestrictedClientStaysRestricted`` below).

    Falsifiable:

    * a corpus scope declared in a realm file but missing from
      ``audittrace.auth.ALL_SCOPES`` (or vice-versa) fails
      ``test_corpus_scopes_match_all_scopes_dict``;
    * granting a corpus scope to ``audittrace-opencode``/``audittrace-webui``
      fails ``test_corpus_scopes_not_on_end_user_clients``;
    * granting a corpus scope to any client other than ``admin-client``
      fails ``test_corpus_write_only_granted_to_admin_client``.
    """

    _EXPECTED_CORPUS_SCOPES: frozenset[str] = frozenset(
        {
            "memory:corpus:decisions:read",
            "memory:corpus:decisions:write",
            "memory:corpus:skills:read",
            "memory:corpus:skills:write",
            "memory:corpus:semantic:read",
            "memory:corpus:semantic:write",
        }
    )

    _END_USER_CLIENT_IDS: tuple[str, ...] = ("audittrace-opencode", "audittrace-webui")

    def test_realm_declares_all_corpus_scopes(self) -> None:
        """Chart-rendered realm (the file actually imported on a fresh
        cluster) declares all six corpus scopes as clientScopes."""
        realm = _rendered_realm_json(_render())
        declared = {s.get("name") for s in realm.get("clientScopes", []) or []}
        missing = self._EXPECTED_CORPUS_SCOPES - declared
        assert not missing, (
            "Drift: charts/audittrace/files/realm-audittrace.json is "
            "missing corpus clientScope declarations for: " + ", ".join(sorted(missing))
        )

    def test_top_level_realm_declares_all_corpus_scopes(self) -> None:
        """The dev/standalone import file declares the same six corpus
        scopes as the chart file (WU-A2: declare in BOTH realm files, kept
        identical)."""
        realm = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        declared = {s.get("name") for s in realm.get("clientScopes", []) or []}
        missing = self._EXPECTED_CORPUS_SCOPES - declared
        assert not missing, (
            "Drift: keycloak/realm-audittrace.json is missing corpus "
            "clientScope declarations for: "
            + ", ".join(sorted(missing))
            + " — it must mirror charts/audittrace/files/realm-audittrace.json."
        )

    def test_corpus_scopes_match_all_scopes_dict(self) -> None:
        """Cross-check the realm-declared corpus scopes against
        ``audittrace.auth.ALL_SCOPES`` in both directions. The OpenAPI
        security scheme is generated FROM ``ALL_SCOPES``, so a realm-only
        scope would be grantable but invisible in Swagger/OpenAPI, and an
        ``ALL_SCOPES``-only entry would be documented but never issuable —
        this is the drift class WU-A2 exists to prevent."""
        from audittrace.auth import ALL_SCOPES

        code_corpus = {s for s in ALL_SCOPES if s.startswith("memory:corpus:")}
        realm = _rendered_realm_json(_render())
        realm_corpus = {
            s.get("name")
            for s in realm.get("clientScopes", []) or []
            if (s.get("name") or "").startswith("memory:corpus:")
        }
        assert code_corpus == realm_corpus, (
            "Drift between audittrace.auth.ALL_SCOPES and the realm's "
            f"declared memory:corpus:* scopes. In ALL_SCOPES only: "
            f"{sorted(code_corpus - realm_corpus)}. In realm only: "
            f"{sorted(realm_corpus - code_corpus)}."
        )
        assert code_corpus == self._EXPECTED_CORPUS_SCOPES

    @pytest.mark.parametrize("client_id", ["audittrace-opencode", "audittrace-webui"])
    def test_corpus_scopes_not_on_end_user_clients(self, client_id: str) -> None:
        """WU-A3: end-user clients get NO corpus scope in EITHER set for
        this governance-floor pass — the whole ``memory:corpus:*`` family
        stays operator/curator-tier."""
        realm = _rendered_realm_json(_render())
        for c in realm.get("clients", []) or []:
            if c.get("clientId") != client_id:
                continue
            both = set(c.get("defaultClientScopes") or []) | set(
                c.get("optionalClientScopes") or []
            )
            offenders = sorted(s for s in both if s.startswith("memory:corpus:"))
            assert not offenders, (
                f"Drift: client {client_id!r} was granted corpus scope(s) "
                f"{offenders} — WU-A3 (ADR-062 §4) requires corpus scopes "
                "stay operator/curator-tier; end-user clients never get "
                "them, in either scope set."
            )
            return
        raise AssertionError(f"Client {client_id!r} not found in rendered realm")

    def test_corpus_write_only_granted_to_admin_client(self) -> None:
        """No client other than the operator-tier ``admin-client`` may hold
        ANY corpus scope (read or write) in this Phase-A pass — there is no
        Curator client yet (SDLC-ADR-002 is future work)."""
        realm = _rendered_realm_json(_render())
        offenders: dict[str, list[str]] = {}
        for c in realm.get("clients", []) or []:
            client_id = c.get("clientId")
            if client_id == "admin-client":
                continue
            both = set(c.get("defaultClientScopes") or []) | set(
                c.get("optionalClientScopes") or []
            )
            granted = sorted(s for s in both if s.startswith("memory:corpus:"))
            if granted:
                offenders[client_id] = granted
        assert not offenders, (
            f"Drift: non-operator client(s) hold corpus scopes: {offenders}. "
            "Only admin-client (operator-tier) may hold memory:corpus:* in "
            "this Phase-A pass."
        )

    @staticmethod
    def _corpus_bind_loop_body(text: str) -> str:
        m = re.search(
            r'for SCOPE in "\$\{CORPUS_SCOPES\[@\]\}"; do(.*?)\bdone\b',
            text,
            re.S,
        )
        if m is None:
            raise AssertionError(
                'CORPUS_SCOPES bind loop (`for SCOPE in "${CORPUS_SCOPES[@]}"; '
                "do ... done`) not found — WU-A3 requires a bind loop scoped "
                "to admin-client only, separate from the CLIENT_KIND fan-out."
            )
        return m.group(1)

    def test_provisioner_corpus_scopes_match_and_admin_only(self) -> None:
        """scripts/setup-memory-scopes.sh and the chart's in-cluster Job
        ConfigMap must maintain identical ``CORPUS_SCOPES`` arrays (all six
        scopes), and the corpus bind loop must target ``admin-client``
        only — never ``audittrace-opencode``/``audittrace-webui``."""
        repo_root = CHART_DIR.parent.parent
        script_path = repo_root / "scripts" / "setup-memory-scopes.sh"
        cm_path = (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        )
        script_text = script_path.read_text()
        cm_text = cm_path.read_text()

        def _corpus_scopes_in(text: str) -> set[str]:
            m = re.search(r"CORPUS_SCOPES=\(([^)]*)\)", text)
            if m is None:
                raise AssertionError(
                    f"CORPUS_SCOPES=( ... ) block not in: {text[:200]}"
                )
            return set(re.findall(r'"(memory:corpus:[^"]+)"', m.group(1)))

        script_corpus = _corpus_scopes_in(script_text)
        cm_corpus = _corpus_scopes_in(cm_text)

        assert script_corpus == cm_corpus == self._EXPECTED_CORPUS_SCOPES, (
            "Drift: scripts/setup-memory-scopes.sh and "
            "templates/keycloak/configmap-memory-scopes-script.yaml have "
            f"divergent/incomplete CORPUS_SCOPES arrays. Script: "
            f"{sorted(script_corpus)}. ConfigMap: {sorted(cm_corpus)}. "
            f"Expected: {sorted(self._EXPECTED_CORPUS_SCOPES)}."
        )

        for text, label in ((script_text, "script"), (cm_text, "configmap")):
            loop_body = self._corpus_bind_loop_body(text)
            assert "admin-client" in loop_body, (
                f"{label}: the CORPUS_SCOPES bind loop does not bind to "
                "admin-client — WU-A3 requires corpus scopes reach only "
                "the operator-tier client."
            )
            for forbidden_client in self._END_USER_CLIENT_IDS:
                assert forbidden_client not in loop_body, (
                    f"{label}: the CORPUS_SCOPES bind loop references "
                    f"{forbidden_client!r} — corpus scopes must never reach "
                    "end-user clients (WU-A3, ADR-062 §4)."
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

    def test_no_corpus_scope_on_restricted_client(self) -> None:
        """WU-A3 (ADR-062 §4) — ``memory:corpus:*`` scopes are operator/
        curator-tier and must never appear on ``audittrace-restricted``
        (SC-09), in EITHER scope set, in EITHER realm file. Same VOID
        mechanism as ``test_top_level_realm_grants_no_audit_scope`` above:
        Keycloak silently drops a requested-but-not-offered scope, so "the
        client does not offer it" is the entire guarantee an adversarial
        cross-tenant/corpus-boundary test relies on.

        Checked against both realm files (not the FORBIDDEN tuple above,
        which predates ADR-062 and is a fixed literal list) so a corpus
        scope added to either file's audittrace-restricted client fails
        here regardless of which realm file it landed in. The top-level
        file is plain JSON; the chart file has Helm templating elsewhere
        (webui redirectUris/webOrigins) so it's read via the rendered
        realm, same as every other chart-file check in this module."""
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        for label, realm in (
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ):
            c = self._restricted(realm)
            both = list(c.get("defaultClientScopes", [])) + list(
                c.get("optionalClientScopes", [])
            )
            offenders = [s for s in both if s.startswith("memory:corpus:")]
            assert not offenders, (
                f"{label}: audittrace-restricted was granted corpus scope(s) "
                f"{offenders} — this VOIDS every SC-09 result the same way "
                "an audit/admin scope would. Corpus scopes are operator/"
                "curator-tier (WU-A3, ADR-062 §4); remove them."
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


class TestPrivilegedCorpusScopeSingleSourceOfTruth:
    """#371 — the granular-corpus-scope residual gap named in ADR-062 §4:
    "the scope-drift guard must learn the granular corpus scopes so an
    undeclared holder is flagged." The app classifier
    (``identity.is_privileged_scope``) and Check 11's shadow-client ranking
    must never drift apart.

    Unlike the string-shape pins in ``TestPostDeployVerifyShadowClientCheck``
    above, the ranking tests here actually EXECUTE the extracted shell
    snippet in a real ``bash`` subprocess against synthetic comma-joined
    default-scope lists — a genuinely falsifiable test, not a text search:
    delete the ``memory:corpus:*:write`` case arm (or shrink the glob) and
    every "ranked privileged" assertion below goes RED because the real
    shell evaluates to no match.
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "scripts" / "post-deploy-verify.sh").read_text(
            encoding="utf-8"
        )

    def _extract_ranking_snippet(self) -> str:
        """Pull the exact per-scope ranking block (the ``IFS=','`` split +
        ``case`` loop) out of Check 11 so it can be executed standalone."""
        script = self._script()
        start_marker = "IFS=',' read -ra _shadow_scopes <<< \"$sc\""
        start = script.index(start_marker)
        end_marker = "esac\n            done\n"
        end = script.index(end_marker, start) + len(end_marker)
        return script[start:end]

    def _rank(self, sc: str) -> str:
        """Run the extracted ranking snippet in a real bash subshell against
        a synthetic comma-joined ``sc`` (default-client-scopes) string and
        return the ``$privileged`` value it computes."""
        snippet = self._extract_ranking_snippet()
        shell_script = (
            f'sc="{sc}"\nprivileged=""\n{snippet}\nprintf "%s" "$privileged"\n'
        )
        result = subprocess.run(
            ["bash", "-c", shell_script], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"extracted ranking snippet failed to run: {result.stderr}"
        )
        return result.stdout

    def test_glob_literal_matches_python_constant(self) -> None:
        """The shell guard's ``case`` pattern mirrors
        ``identity.CORPUS_WRITE_SCOPE_GLOB`` VERBATIM — checked inside the
        EXECUTABLE ranking snippet itself (not just anywhere in the file, so
        a copy left behind in a comment cannot mask the literal being
        dropped from the actual ``case`` arm). A change to one without the
        other is exactly how #370/#371-class drift happens; this test is
        the single source-of-truth enforcement point."""
        from audittrace.identity import CORPUS_WRITE_SCOPE_GLOB

        assert CORPUS_WRITE_SCOPE_GLOB in self._extract_ranking_snippet(), (
            "the shell guard's corpus-write glob pattern has drifted from "
            f"identity.CORPUS_WRITE_SCOPE_GLOB ({CORPUS_WRITE_SCOPE_GLOB!r}) "
            "— they must be the exact same literal string inside the "
            "ranking `case` arm, or the app classifier and the shell guard "
            "can silently disagree."
        )

    def test_undeclared_corpus_writer_is_ranked_privileged(self) -> None:
        """A synthetic UNDECLARED client holding
        ``memory:corpus:decisions:write`` must rank ``!!`` privileged — the
        exact scenario ADR-062 §4 names and #371 exists to close."""
        assert (
            self._rank("memory:corpus:decisions:write")
            == "memory:corpus:decisions:write"
        )

    @pytest.mark.parametrize("collection", ["decisions", "skills", "semantic"])
    def test_every_declared_collection_is_ranked_privileged(
        self, collection: str
    ) -> None:
        """ADR-062 §4's granular collections — every one, not just
        ``decisions``, must be recognised."""
        sc = f"memory:corpus:{collection}:write"
        assert self._rank(sc) == sc

    def test_corpus_read_scope_is_not_ranked_privileged(self) -> None:
        assert self._rank("memory:corpus:decisions:read") == ""

    def test_plain_layer_write_scope_is_not_ranked_privileged(self) -> None:
        """Regression lock: an ordinary per-user writer scope
        (``memory:episodic:write``) must NOT be flagged, or the guard cries
        wolf on every legitimate opencode/webui deployment."""
        assert self._rank("memory:episodic:write") == ""

    def test_admin_and_audit_ranking_unchanged(self) -> None:
        """No new false positives / no lost coverage on a correct cluster:
        the pre-#371 admin/audit ranking still works after the corpus-write
        extension."""
        assert self._rank("audittrace:admin") == "audittrace:admin"
        assert self._rank("audittrace:audit") == "audittrace:audit"

    def test_mixed_scope_list_does_not_bleed_across_tokens(self) -> None:
        """Regression for the glob-bleed bug this rewrite fixes: a
        comma-joined list containing a corpus-READ scope and an unrelated
        write scope must not falsely match ``memory:corpus:*:write`` by the
        middle ``*`` wildcard spanning across two unrelated scope tokens."""
        assert self._rank("memory:corpus:decisions:read,other:write") == ""

    def test_no_privileged_scope_ranks_empty(self) -> None:
        assert self._rank("memory:read,session:read-own") == ""


class TestChromaDBPersistPathMatchesMount:
    """The PVC must be mounted where ChromaDB actually persists (#372).

    ChromaDB 0.x persisted to ``/chroma/chroma``; 1.x persists to ``/data``.
    The chart's mountPath was written for 0.x and never updated when the image
    moved to 1.x, so ChromaDB wrote its entire database to the container's
    EPHEMERAL overlay while a 10Gi PVC sat empty. Every pod restart wiped the
    semantic memory layer — 160 restarts before it was found on 2026-07-21.

    Nothing surfaced it. The pod was Ready, both probes hit
    ``/api/v2/heartbeat`` which does not touch persistence, and an empty store
    answers every query correctly with zero results. Recall silently returned
    nothing forever, and the model read that as "the document does not exist"
    (#374).

    These tests pin the chart side. The runtime side — that the persist path
    is on a real volume rather than the overlay — is
    ``post-deploy-verify.sh`` check 12, which is the only check that would
    have caught this.
    """

    @staticmethod
    def _sts() -> str:
        return (
            REPO_ROOT
            / "charts"
            / "audittrace"
            / "templates"
            / "chromadb"
            / "statefulset.yaml"
        ).read_text(encoding="utf-8")

    def test_mount_path_is_driven_by_persist_path_value(self) -> None:
        """The mount must not be a hardcoded literal that can drift again."""
        sts = self._sts()
        assert "chromadb.persistPath" in sts, (
            "chromadb statefulset no longer derives mountPath from "
            ".Values.chromadb.persistPath — a hardcoded mountPath is exactly "
            "how #372 happened (0.x path left behind after a 1.x image bump)."
        )
        assert "/chroma/chroma" not in sts.split("persistPath")[-1][:200], (
            "the ChromaDB 0.x path /chroma/chroma reappeared as the mount; "
            "1.x persists to /data"
        )

    def test_default_persist_path_matches_chromadb_1x(self) -> None:
        values = (REPO_ROOT / "charts" / "audittrace" / "values.yaml").read_text(
            encoding="utf-8"
        )
        assert re.search(r"^\s*persistPath:\s*/data\s*$", values, re.M), (
            "chromadb.persistPath must default to /data for ChromaDB 1.x. "
            "If the image is downgraded to 0.x this becomes /chroma/chroma — "
            "and the value must be changed deliberately, never left to drift."
        )

    def test_runtime_guard_exists(self) -> None:
        """A chart-side test cannot see a container's real filesystem.

        Only the runtime check can prove the persist path is on the PVC, so it
        must not be dropped in favour of these cheaper tests.
        """
        script = (REPO_ROOT / "scripts" / "post-deploy-verify.sh").read_text(
            encoding="utf-8"
        )
        assert "ChromaDB persistence is durable" in script, (
            "the runtime persistence check is gone — chart tests alone cannot "
            "detect a mount landing on the container overlay."
        )
        assert "CONTAINER OVERLAY" in script, (
            "the runtime check must compare the persist path's device against "
            "/ and fail when they match"
        )


class TestMemoryLayerContentCheck:
    """E2E must prove layers hold REAL DATA, not merely that they respond (#372).

    Every assertion we had was satisfiable by an empty semantic layer: chat
    returned 200, prompt_tokens looked large (that was the EPISODIC layer),
    tool_calls recorded ``recall_semantic`` even when it returned nothing, and
    the Bruno evidence chain corroborated tool-call COUNTS rather than content.
    So an empty store passed every gate for months.
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "scripts" / "post-deploy-verify.sh").read_text(
            encoding="utf-8"
        )

    def test_layer_content_check_exists(self) -> None:
        assert "Memory layers hold real data" in self._script(), (
            "the layer-content check is gone — without it an empty memory "
            "layer passes every other assertion in the gate."
        )

    def test_distinguishes_unreadable_from_empty(self) -> None:
        """A 401 must not read as "empty".

        The first version of this check parsed ``.items`` length without
        looking at the HTTP status. A 401 returns a JSON error body with no
        ``items``, so jq yielded 0 and an AUTH FAILURE was reported as an
        EMPTY LAYER — the same absence-of-evidence trap the check exists to
        catch. Caught 2026-07-21 by running it against a failing auth path.
        """
        script = self._script()
        assert "UNREADABLE (HTTP" in script, (
            "the layer check must report a non-200 as UNREADABLE with its "
            "status code, never as EMPTY — an auth failure and an empty layer "
            "are different incidents with different fixes."
        )
        assert "%{http_code}" in script, (
            "the check must capture the HTTP status separately from the body; "
            "parsing alone cannot distinguish 401-with-error-body from an "
            "empty result set."
        )


class TestSingleOwnershipKeyInVectorMetadata:
    """Every Chroma writer must tag chunks with ``user_id`` (#372 / #374).

    ``ChromaSemanticService.search`` filters non-admin callers on
    ``where={"user_id": ...}``. Until 2026-07-21 no writer set that key:
    the markdown path set no ownership key at all, and the PDF pipeline set
    ``ingested_by_user_id``. So the filter matched nothing and EVERY
    non-admin recall returned zero results for EVERY collection.

    It failed silently by construction. An empty result set is
    indistinguishable from "no relevant documents", so recall simply returned
    nothing and the model reported that documents did not exist (#374).
    Nothing in the stack could tell the difference.

    One key. If a writer needs to record who ingested something as distinct
    from who owns it, add a SEPARATE field — do not rename this one.
    """

    WRITERS = (
        "src/audittrace/routes/memory.py",
        "src/audittrace/routes/memory_pdf/pipeline.py",
    )

    def test_no_writer_uses_a_second_ownership_key(self) -> None:
        offenders: list[str] = []
        for rel in self.WRITERS:
            for i, line in enumerate(
                (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # historical notes may name the old key
                if '"ingested_by_user_id"' in stripped:
                    offenders.append(f"{rel}:{i}")
        assert not offenders, (
            "a second ownership key is back in Chroma metadata: "
            f"{offenders}. ChromaSemanticService.search filters on `user_id` "
            "and nothing else, so chunks tagged with another key are "
            "invisible to every non-admin caller — silently, because an empty "
            "result set looks like 'nothing found'."
        )

    def test_both_writers_set_user_id(self) -> None:
        for rel in self.WRITERS:
            body = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert '"user_id": user_id' in body or '"user_id": user.user_id' in body, (
                f"{rel} builds Chroma metadata without a `user_id` key. "
                "Non-admin recall will return nothing from anything it writes."
            )

    def test_search_filter_key_matches_the_writers(self) -> None:
        """Pin reader and writer together so they cannot drift apart again."""
        svc = (REPO_ROOT / "src" / "audittrace" / "services" / "semantic.py").read_text(
            encoding="utf-8"
        )
        assert 'where = {"user_id": user_context.user_id}' in svc, (
            "the search filter key changed. It must stay `user_id`, or every "
            "writer must change with it in the same commit — a reader/writer "
            "mismatch here produces zero results with no error anywhere."
        )


class TestLibrechatConsoleClient:
    """M3-WU-1 — the ``audittrace-librechat`` OIDC client (the M3 LibreChat
    console's browser-facing public PKCE client, ADR-064) must exist in
    BOTH realm files with the expected scope grant.

    **M3-WU-D2-1 (2026-08-30) superseded the original read/ask-only
    boundary this class enforced.** The ratified
    ``2026-08-30-SPEC-m3-souvenirs-sovereign-memory.md`` deliberately
    grants ``audittrace-librechat`` the three per-user-layer
    ``memory:*:write`` scopes — as OPTIONAL, never default — so the BFF's
    Souvenirs-panel memory-proxy exchange (``bff/memory_proxy.py``,
    ``bff/memory_scopes.py``) can request them explicitly. The browser's
    OWN login token never carries them (they are absent from
    ``defaultClientScopes``); only a BFF-mediated RFC 8693 exchange that
    asks for them by name in its ``scope=`` parameter can obtain them (see
    ``bff/exchange.py::exchange_token``'s ``requested_scope``). A
    companion ADR documenting this boundary change is queued per the
    spec's "Companion ADR" section.

    Falsifiable, one assertion per guard:

    * renaming/removing the client from either realm file fails
      ``test_client_present_in_both_realms``;
    * its ``defaultClientScopes`` drifting from the exact read/ask set fails
      ``test_default_scopes_match_expected_read_ask_set`` — write access
      must NEVER be a default (always-issued) scope;
    * its ``optionalClientScopes`` drifting from the exact expected set
      (``offline_access`` + the three memory-write scopes) fails
      ``test_optional_scopes_match_expected_set``;
    * granting it ``audittrace:admin`` (default OR optional) fails
      ``test_no_admin_scope_granted`` — the spec is explicit: the console
      must never obtain hard-delete of the audit trail;
    * granting it a ``memory:corpus:*`` scope fails
      ``test_no_corpus_scope_granted`` (ADR-062 §4 — corpus scopes are
      operator/curator-tier);
    * flipping ``publicClient``/enabling implicit/ROPC/service-accounts, or
      adding a client secret, fails ``test_public_client_no_secret_no_implicit_no_ropc``;
    * dropping PKCE S256 fails ``test_pkce_s256_enabled``;
    * dropping the ``aud-audittrace-server`` mapper (or its
      ``included.custom.audience``) fails ``test_audience_mapper_present`` —
      without it, issued tokens would not carry ``aud=audittrace-server``
      and ``/v1`` would reject them;
    * dropping ``preferred-username``/``email`` mappers fails
      ``test_identity_mappers_present`` — audit rows/traces would lose the
      human-legible identity alongside ``sub``;
    * a wildcard ``redirectUris``/``webOrigins`` entry (the
      OIDC-REDIRECT-URI-DRIFT regression) fails
      ``test_no_wildcard_redirect_uris``.
    """

    _EXPECTED_DEFAULT_SCOPES: frozenset[str] = frozenset(
        {
            "audittrace:query",
            "audittrace:context",
            "audittrace:audit",
            "memory:episodic:read",
            "memory:procedural:read",
            "memory:conversational:read-own",
            "memory:semantic:read",
            # M3-WU-3b (D3) — standard OIDC scopes the LibreChat console
            # needs for its own identity claims (LibreChat's
            # OPENID_SCOPE="openid profile email offline_access").
            "profile",
            "email",
        }
    )
    # M3-WU-3b (D3) — offline_access (refresh token) is deliberately
    # OPTIONAL, not default: it is the one scope that changes Keycloak's
    # token-issuance behaviour (a refresh token gets minted), so it stays
    # an explicit per-request opt-in rather than an always-on grant.
    #
    # M3-WU-D2-1 (2026-08-30) added the three per-user-layer memory-write
    # scopes, ALSO optional — same "never auto-granted" discipline: only
    # the BFF's memory-proxy exchange requests them explicitly (see class
    # docstring above).
    _EXPECTED_OPTIONAL_SCOPES: frozenset[str] = frozenset(
        {
            "offline_access",
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
        }
    )

    @staticmethod
    def _client(realm: dict) -> dict:
        for c in realm.get("clients", []) or []:
            if c.get("clientId") == "audittrace-librechat":
                return c
        raise AssertionError(
            "audittrace-librechat is missing from the realm — the M3-WU-1 "
            "console client was renamed or removed."
        )

    @staticmethod
    def _both_realms() -> list[tuple[str, dict]]:
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        return [
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ]

    def test_client_present_in_both_realms(self) -> None:
        for _label, realm in self._both_realms():
            self._client(realm)  # raises AssertionError if missing

    def test_default_scopes_match_expected_read_ask_set(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            default = set(c.get("defaultClientScopes") or [])
            assert default == self._EXPECTED_DEFAULT_SCOPES, (
                f"{label}: audittrace-librechat defaultClientScopes drifted "
                f"from the read/ask boundary. Extra: "
                f"{sorted(default - self._EXPECTED_DEFAULT_SCOPES)}; missing: "
                f"{sorted(self._EXPECTED_DEFAULT_SCOPES - default)}."
            )

    def test_optional_scopes_match_expected_set(self) -> None:
        """M3-WU-3b (D3) — offline_access is the ONLY optional scope;
        neutering the D3 realm reconcile (dropping it, or widening the
        optional set to something else) fails this."""
        for label, realm in self._both_realms():
            c = self._client(realm)
            optional = set(c.get("optionalClientScopes") or [])
            assert optional == self._EXPECTED_OPTIONAL_SCOPES, (
                f"{label}: audittrace-librechat optionalClientScopes drifted. "
                f"Extra: {sorted(optional - self._EXPECTED_OPTIONAL_SCOPES)}; "
                f"missing: {sorted(self._EXPECTED_OPTIONAL_SCOPES - optional)}."
            )

    def test_no_admin_scope_granted(self) -> None:
        """M3-WU-D2-1 deliberately granted the three per-user-layer
        memory-write scopes (optional-only — see
        ``test_optional_scopes_match_expected_set``); it must NEVER extend
        to ``audittrace:admin``, in either scope set. Admin is what would
        let the console hard-delete the audit trail (``?hard=true`` on
        ``DELETE /memory/...``) — the spec's non-negotiable invariant."""
        for label, realm in self._both_realms():
            c = self._client(realm)
            both = set(c.get("defaultClientScopes") or []) | set(
                c.get("optionalClientScopes") or []
            )
            assert "audittrace:admin" not in both, (
                f"{label}: audittrace-librechat was granted audittrace:admin "
                "— this VOIDS the M3-WU-D2-1 invariant that the console can "
                "never hard-delete the audit trail."
            )

    def test_no_corpus_scope_granted(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            both = set(c.get("defaultClientScopes") or []) | set(
                c.get("optionalClientScopes") or []
            )
            offenders = sorted(s for s in both if s.startswith("memory:corpus:"))
            assert not offenders, (
                f"{label}: audittrace-librechat was granted corpus scope(s) "
                f"{offenders} — corpus scopes are operator/curator-tier "
                "(ADR-062 §4); end-user clients never get them."
            )

    def test_public_client_no_secret_no_implicit_no_ropc(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            assert c.get("publicClient") is True, (
                f"{label}: audittrace-librechat must be a public client (no "
                "client secret to hold)."
            )
            assert c.get("standardFlowEnabled") is True, (
                f"{label}: audittrace-librechat must have the Authorization "
                "Code flow enabled."
            )
            assert c.get("implicitFlowEnabled") is not True, (
                f"{label}: implicit grant is forbidden (RFC 9700)."
            )
            assert c.get("directAccessGrantsEnabled") is not True, (
                f"{label}: Resource Owner Password Credentials is forbidden (RFC 9700)."
            )
            assert c.get("serviceAccountsEnabled") is not True, (
                f"{label}: audittrace-librechat is user-facing only — no "
                "service account grant."
            )
            assert "secret" not in c, (
                f"{label}: a public client must carry no client secret."
            )

    def test_pkce_s256_enabled(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            assert (
                c.get("attributes", {}).get("pkce.code.challenge.method") == "S256"
            ), f"{label}: audittrace-librechat must enforce PKCE with S256."

    def test_audience_mapper_present(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            mappers = {m.get("name"): m for m in c.get("protocolMappers", []) or []}
            assert "aud-audittrace-server" in mappers, (
                f"{label}: audittrace-librechat is missing the "
                "aud-audittrace-server audience mapper — issued tokens would "
                "not carry aud=audittrace-server, and /v1 would reject them."
            )
            config = mappers["aud-audittrace-server"].get("config", {})
            assert config.get("included.custom.audience") == "audittrace-server", (
                f"{label}: aud-audittrace-server mapper does not target "
                "audittrace-server."
            )

    def test_identity_mappers_present(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            mappers = {m.get("name") for m in c.get("protocolMappers", []) or []}
            assert {"preferred-username", "email"} <= mappers, (
                f"{label}: audittrace-librechat must carry preferred_username "
                "+ email protocol mappers so audit rows/traces carry a "
                "human-legible identity alongside `sub`."
            )

    def test_no_wildcard_redirect_uris(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            offenders = [
                u
                for u in (c.get("redirectUris") or []) + (c.get("webOrigins") or [])
                if u.endswith("*")
            ]
            assert not offenders, (
                f"{label}: audittrace-librechat carries wildcard URI(s) "
                f"{offenders} — RFC 9700 / ADR-042 §3 forbid redirect-URI/"
                "webOrigin wildcards."
            )


class TestConsoleRealmScopeReconcile:
    """M3-WU-3b (D3) — the ``ensure-memory-scopes`` Job's Step 5 kcadm
    bodies (``PROFILE_BODY``/``EMAIL_BODY``/``OFFLINE_ACCESS_BODY`` in
    ``configmap-memory-scopes-script.yaml``) are what actually provisions
    ``profile``/``email``/``offline_access`` onto a PRE-EXISTING realm (the
    "``--import-realm`` only imports on first run" gap). They must stay in
    lock-step with the ``clientScopes`` entries declared in BOTH realm
    files, or a fresh install and an upgraded pre-existing realm would end
    up with two different scope shapes.

    Falsifiable: change either side (the kcadm body, or a realm file's
    ``protocol``/``attributes``/``protocolMappers`` for one of these three
    scopes) without updating the other, and this goes RED. ``description``
    is deliberately excluded from the comparison — it is realm-file-only
    documentation kcadm's ``client-scopes`` create endpoint does not even
    accept a value for in these bodies, not a drift-worthy field.
    """

    _SCOPE_NAMES = ("profile", "email", "offline_access")
    _COMPARED_KEYS = ("protocol", "attributes", "protocolMappers")

    @staticmethod
    def _script_text() -> str:
        return (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        ).read_text(encoding="utf-8")

    @classmethod
    def _kcadm_bodies(cls) -> dict[str, dict]:
        text = cls._script_text()
        out: dict[str, dict] = {}
        for var, name in (
            ("PROFILE_BODY", "profile"),
            ("EMAIL_BODY", "email"),
            ("OFFLINE_ACCESS_BODY", "offline_access"),
        ):
            m = re.search(rf"{var}='(\{{.*\}})'", text)
            assert m is not None, (
                f"configmap-memory-scopes-script.yaml is missing the {var} "
                "constant Step 5 uses to reconcile the console realm scopes."
            )
            body = json.loads(m.group(1))
            assert body.get("name") == name, (
                f"{var} declares name={body.get('name')!r}, expected {name!r}."
            )
            out[name] = body
        return out

    @staticmethod
    def _realm_scope(realm: dict, name: str) -> dict:
        for s in realm.get("clientScopes", []) or []:
            if s.get("name") == name:
                return s
        raise AssertionError(f"clientScope {name!r} is missing from the realm.")

    @staticmethod
    def _both_realms() -> list[tuple[str, dict]]:
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        return [
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ]

    def test_kcadm_bodies_present_for_all_three_scopes(self) -> None:
        bodies = self._kcadm_bodies()
        assert set(bodies) == set(self._SCOPE_NAMES)

    def test_kcadm_bodies_match_realm_files_field_for_field(self) -> None:
        bodies = self._kcadm_bodies()
        for label, realm in self._both_realms():
            for scope_name in self._SCOPE_NAMES:
                realm_scope = self._realm_scope(realm, scope_name)
                kcadm_body = bodies[scope_name]
                for key in self._COMPARED_KEYS:
                    realm_value = realm_scope.get(key)
                    kcadm_value = kcadm_body.get(key)
                    assert realm_value == kcadm_value, (
                        f"{label}: clientScope {scope_name!r}.{key} drifted "
                        f"from the ensure-memory-scopes Job's kcadm body. "
                        f"realm={realm_value!r} kcadm={kcadm_value!r} — a "
                        "fresh install (realm.json) and an upgraded "
                        "pre-existing realm (Step 5's kcadm reconcile) "
                        "would end up with different scope shapes."
                    )

    def test_step5_comment_does_not_reference_a_stale_test_name(self) -> None:
        """The 2026-08-29 independent-review fix: the comment used to name
        a test class that did not exist (a false coverage claim). Assert it
        now names THIS class, so a future rename of either side is caught."""
        text = self._script_text()
        assert "TestConsoleRealmScopeReconcile" in text, (
            "configmap-memory-scopes-script.yaml's Step 5 comment must "
            "reference tests/test_chart_drift_guards.py::"
            "TestConsoleRealmScopeReconcile (this class) by name."
        )


class TestLibrechatBffClient:
    """M3-WU-2 — the ``audittrace-librechat-bff`` confidential client
    (RFC 8693 token-exchange edge for the LibreChat BFF sidecar,
    ADR-042 §5 Option A) must exist in BOTH realm files, be genuinely
    confidential (never a public client with an inherent secret leak),
    hold no scope grants of its own (its only job is authenticating the
    exchange call — the minted token's audience/scope come from the
    exchange ``audience`` parameter targeting ``audittrace-librechat``,
    not from this client's own grants), and never gain write/corpus/
    admin/audit scopes it has no reason to hold.

    Falsifiable, one assertion per guard:

    * renaming/removing the client from either realm file fails
      ``test_client_present_in_both_realms``;
    * flipping ``publicClient`` to ``True``, enabling any interactive
      flow, or committing a literal ``secret`` value fails
      ``test_confidential_client_no_secret_committed``;
    * granting it ANY scope (default or optional) fails
      ``test_no_scopes_granted`` — it needs none, ever;
    * a description over Keycloak's varchar(255) column fails
      ``test_description_fits_keycloak_column`` (same DB-column trap as
      ``TestRestrictedClientStaysRestricted.test_description_fits_keycloak_column``).
    """

    @staticmethod
    def _client(realm: dict) -> dict:
        for c in realm.get("clients", []) or []:
            if c.get("clientId") == "audittrace-librechat-bff":
                return c
        raise AssertionError(
            "audittrace-librechat-bff is missing from the realm — the M3-WU-2 "
            "BFF token-exchange client was renamed or removed."
        )

    @staticmethod
    def _both_realms() -> list[tuple[str, dict]]:
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        return [
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ]

    def test_client_present_in_both_realms(self) -> None:
        for _label, realm in self._both_realms():
            self._client(realm)  # raises AssertionError if missing

    def test_confidential_client_no_secret_committed(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            assert c.get("publicClient") is False, (
                f"{label}: audittrace-librechat-bff must be confidential "
                "(publicClient: false) — it authenticates a server-to-"
                "server token-exchange call, never a browser flow."
            )
            assert c.get("clientAuthenticatorType") == "client-secret", (
                f"{label}: audittrace-librechat-bff must use client-secret "
                "authentication."
            )
            assert c.get("standardFlowEnabled") is not True, (
                f"{label}: audittrace-librechat-bff must not enable the "
                "browser Authorization Code flow — it is never a redirect "
                "target."
            )
            assert c.get("implicitFlowEnabled") is not True, (
                f"{label}: implicit grant is forbidden (RFC 9700)."
            )
            assert c.get("directAccessGrantsEnabled") is not True, (
                f"{label}: Resource Owner Password Credentials is forbidden (RFC 9700)."
            )
            assert "secret" not in c, (
                f"{label}: a client secret must NEVER be committed to the "
                "realm file — Keycloak generates one on import; the BFF "
                "reads it from Vault/env at deploy time."
            )

    def test_no_scopes_granted(self) -> None:
        """The exchange client needs no scopes of its own — the minted
        token's audience/scope come from the ``audience=audittrace-
        librechat`` exchange parameter (that client's own scope profile),
        not from this client. Any scope grant here would be unused
        privilege sitting on a confidential client — a REJECT-worthy
        defect, not a nice-to-catch."""
        for label, realm in self._both_realms():
            c = self._client(realm)
            both = set(c.get("defaultClientScopes") or []) | set(
                c.get("optionalClientScopes") or []
            )
            assert not both, (
                f"{label}: audittrace-librechat-bff was granted scope(s) "
                f"{sorted(both)} — it should hold none; unused privilege "
                "on a confidential client is a defect, not a convenience."
            )

    def test_description_fits_keycloak_column(self) -> None:
        for label, realm in self._both_realms():
            c = self._client(realm)
            desc = c.get("description") or ""
            assert len(desc) <= 255, (
                f"{label}: audittrace-librechat-bff description is "
                f"{len(desc)} chars; Keycloak's CLIENT.DESCRIPTION column "
                "is varchar(255) — realm import fails at container boot."
            )


class TestClientDescriptionsWithinKeycloakColumnLimit:
    """Keycloak's ``CLIENT.DESCRIPTION`` column is ``varchar(255)``. A client
    ``description`` longer than 255 chars fails realm import at container boot
    with ``ERROR: value too long for type character varying(255)`` — the
    keycloak container never turns healthy and the whole stack fails to start.

    This class exists because a JSON-shape check is NOT enough: the file parses
    as valid JSON with a 314-char description, every other realm guard stays
    green, ``make test`` passes, and the failure only surfaces in CI's
    "Build and start stack" step where a real Keycloak imports into a real
    Postgres (PR #306, 2026-08-28). This guard moves that DB-column constraint
    into the fast local gate.

    Falsifiable: restore ANY client ``description`` to >255 chars and this turns
    RED. Applies to BOTH realm files and every client (so WU-2's confidential
    ``audittrace-librechat-bff`` client is covered from day one).
    """

    _KEYCLOAK_VARCHAR_LIMIT = 255

    @staticmethod
    def _both_realms() -> list[tuple[str, dict]]:
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        return [
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ]

    def test_every_client_description_within_255(self) -> None:
        for label, realm in self._both_realms():
            offenders = [
                (c.get("clientId"), len(c.get("description") or ""))
                for c in (realm.get("clients") or [])
                if len(c.get("description") or "") > self._KEYCLOAK_VARCHAR_LIMIT
            ]
            assert not offenders, (
                f"{label}: client description(s) exceed Keycloak's varchar(255) "
                f"CLIENT.DESCRIPTION limit {offenders} — realm import will fail "
                "at container boot (the stack never becomes healthy). Shorten "
                "the description to <=255 chars."
            )


def _keycloak_container(docs: list[dict]) -> dict:
    """Return the keycloak container spec from the rendered chart."""
    name = f"{RELEASE}-keycloak"
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
            if c.get("name") == "keycloak":
                return c
        raise AssertionError(f"Deployment {name} has no container named 'keycloak'")
    raise AssertionError(f"Deployment {name} not in render")


class TestKeycloakTokenExchangeFeatureFlags:
    """M3-WU-2b D1 — RFC 8693 token-exchange is an OFF-by-default preview
    feature in Keycloak 24 (the deployed ``quay.io/keycloak/keycloak:24.0``,
    ``charts/audittrace/values.yaml``); without ``--features=token-exchange``
    on the KC start command the BFF's live exchange returns
    ``HTTP 400 {"error":"unsupported_grant_type"}``. The internal
    client-to-client exchange authorization model additionally needs
    ``admin-fine-grained-authz`` (verified live against a throwaway
    Keycloak 24.0.5 container 2026-08-29 — see
    ``configmap-memory-scopes-script.yaml`` Step 4 for the full mechanism).

    ``templates/keycloak/deployment.yaml`` has TWO start-command branches
    (``vault.enabled`` exec-string vs. the plain ``args:`` list) that must
    stay byte-identical on this flag — a drift between them is exactly the
    kind of gap ``feedback_no_more_drifts`` exists to catch.

    Falsifiable: drop the ``--features=`` flag (or one of its two
    comma-joined values) from EITHER branch and the matching test goes RED.
    """

    _REQUIRED_FEATURES: tuple[str, ...] = ("token-exchange", "admin-fine-grained-authz")

    def test_vault_branch_enables_token_exchange_features(self) -> None:
        docs = self._render_with(vault_enabled=True)
        container = _keycloak_container(docs)
        args = container.get("args") or []
        assert len(args) == 1, (
            "vault.enabled=true branch: expected a single shell-string arg "
            f"(the vaultSecretFileGuard + exec block), got {len(args)}"
        )
        shell_cmd = args[0]
        for feature in self._REQUIRED_FEATURES:
            assert feature in shell_cmd, (
                f"vault.enabled=true branch: keycloak exec command is missing "
                f"'--features={feature}' — RFC 8693 token-exchange will 400 "
                f"unsupported_grant_type live (M3-WU-2b D1). Command:\n{shell_cmd}"
            )

    def test_plain_branch_enables_token_exchange_features(self) -> None:
        docs = self._render_with(vault_enabled=False)
        container = _keycloak_container(docs)
        args = container.get("args") or []
        joined = " ".join(str(a) for a in args)
        for feature in self._REQUIRED_FEATURES:
            assert feature in joined, (
                f"vault.enabled=false branch: keycloak args is missing "
                f"'--features={feature}' — RFC 8693 token-exchange will 400 "
                f"unsupported_grant_type live (M3-WU-2b D1). args={args!r}"
            )

    def test_both_branches_declare_identical_feature_flags(self) -> None:
        """The two start-arg branches must request the SAME feature set —
        not just each individually containing the substring, but the same
        comma-joined ``--features=`` value. Catches a future edit that adds
        a feature to one branch and forgets the other."""
        vault_container = _keycloak_container(self._render_with(vault_enabled=True))
        plain_container = _keycloak_container(self._render_with(vault_enabled=False))

        vault_match = re.search(
            r"--features=([\w,-]+)", vault_container.get("args", [""])[0]
        )
        plain_args = plain_container.get("args") or []
        plain_flag = next(
            (a for a in plain_args if str(a).startswith("--features=")), None
        )

        assert vault_match is not None, (
            "vault.enabled=true branch has no --features= flag"
        )
        assert plain_flag is not None, (
            "vault.enabled=false branch has no --features= flag"
        )

        vault_features = set(vault_match.group(1).split(","))
        plain_features = set(plain_flag.split("=", 1)[1].split(","))

        assert vault_features == plain_features, (
            "Drift: the vault.enabled=true and vault.enabled=false keycloak "
            f"start-arg branches request different feature sets. vault="
            f"{sorted(vault_features)} plain={sorted(plain_features)} — keep "
            "them in lockstep (deployment.yaml, both branches)."
        )
        missing = set(self._REQUIRED_FEATURES) - vault_features
        assert not missing, f"Both branches are missing required feature(s): {missing}"

    @staticmethod
    def _render_with(*, vault_enabled: bool) -> list[dict]:
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


class TestKeycloakTokenExchangePermission:
    """M3-WU-2b D2 — the fine-grained token-exchange permission that
    authorizes ONLY ``audittrace-librechat-bff``'s service account to
    exchange a caller's token via ``audittrace-librechat`` (the target
    client whose own protocol mapper + default-scope grant stamp
    ``aud=audittrace-server`` + ``scope=audittrace:query`` on the result —
    ``bff/exchange.py``).

    Three things must never drift apart, each individually falsifiable:

    1. the ``token-exchange.authorized-source-client`` attribute declared
       on ``audittrace-librechat`` in BOTH realm files (the static,
       reviewable "who is authorized" declaration — WU-2's
       ``TestLibrechatBffClient`` already covers the bff client's own
       shape: confidential, no committed secret, no scope grants of its
       own, description <=255 chars);
    2. the in-cluster ``ensure-memory-scopes`` Job's Step 4 kcadm logic
       (``configmap-memory-scopes-script.yaml``) that ACTUALLY provisions
       the Keycloak fine-grained admin permission at deploy time — the
       realm-JSON attribute above is not itself read by Keycloak; per the
       "realm.json only imports on first run" gap Steps 1-3 close for
       scope bindings, this Job is the mechanism that survives a
       fresh ``--import-realm`` AND every subsequent upgrade;
    3. ``scripts/setup-memory-scopes.sh``, the disaster-recovery backstop
       that must authorize the exact same (source, target) pair.

    The actual kcadm mechanism (enable ``management/permissions`` on the
    target client -> find-or-create a Client policy naming the source
    client -> attach the policy to the auto-created ``token-exchange``
    scope-permission) was verified live against a throwaway Keycloak
    24.0.5 container (2026-08-29): running Step 4's exact script text
    against a fresh realm turned a `access_denied: Client not allowed to
    exchange` response into a minted token carrying the caller's own
    `sub`, `aud` including `audittrace-server`, and `scope` including
    `audittrace:query`; re-running it was a clean no-op (idempotent).

    Falsifiable: rename/remove the realm attribute, the ConfigMap's
    ``TOKEN_EXCHANGE_*`` constants, or the provisioner script's mirror of
    them, and the matching test below goes RED.
    """

    _EXPECTED_TARGET_CLIENT = "audittrace-librechat"
    _EXPECTED_SOURCE_CLIENT = "audittrace-librechat-bff"
    _ATTR_KEY = "token-exchange.authorized-source-client"

    @staticmethod
    def _both_realms() -> list[tuple[str, dict]]:
        top_level = json.loads(
            (REPO_ROOT / "keycloak" / "realm-audittrace.json").read_text(
                encoding="utf-8"
            )
        )
        chart_rendered = _rendered_realm_json(_render())
        return [
            ("keycloak/realm-audittrace.json", top_level),
            (
                "charts/audittrace/files/realm-audittrace.json (rendered)",
                chart_rendered,
            ),
        ]

    @classmethod
    def _target_client(cls, realm: dict) -> dict:
        for c in realm.get("clients", []) or []:
            if c.get("clientId") == cls._EXPECTED_TARGET_CLIENT:
                return c
        raise AssertionError(
            f"{cls._EXPECTED_TARGET_CLIENT} is missing from the realm."
        )

    def test_target_client_declares_authorized_source_client(self) -> None:
        for label, realm in self._both_realms():
            c = self._target_client(realm)
            attrs = c.get("attributes") or {}
            assert attrs.get(self._ATTR_KEY) == self._EXPECTED_SOURCE_CLIENT, (
                f"{label}: {self._EXPECTED_TARGET_CLIENT}.attributes.'{self._ATTR_KEY}' "
                f"must equal {self._EXPECTED_SOURCE_CLIENT!r} — this is the "
                "declared, drift-guarded record of who Step 4's kcadm "
                "provisioning authorizes to exchange. Got: "
                f"{attrs.get(self._ATTR_KEY)!r}"
            )

    def test_declaration_identical_across_both_realm_files(self) -> None:
        values = {
            label: (self._target_client(realm).get("attributes") or {}).get(
                self._ATTR_KEY
            )
            for label, realm in self._both_realms()
        }
        distinct = set(values.values())
        assert len(distinct) == 1, (
            f"Drift: the '{self._ATTR_KEY}' attribute differs between the two "
            f"realm files: {values}"
        )

    @staticmethod
    def _configmap_script_text() -> str:
        return (
            CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
        ).read_text(encoding="utf-8")

    def _configmap_constants(self) -> dict[str, str]:
        text = self._configmap_script_text()
        out: dict[str, str] = {}
        for name in ("TOKEN_EXCHANGE_TARGET_CLIENT", "TOKEN_EXCHANGE_SOURCE_CLIENT"):
            m = re.search(rf'{name}="([^"]+)"', text)
            assert m is not None, (
                f"configmap-memory-scopes-script.yaml is missing the "
                f"{name} constant used by Step 4 (M3-WU-2b)."
            )
            out[name] = m.group(1)
        return out

    def test_configmap_step4_constants_match_realm_declaration(self) -> None:
        constants = self._configmap_constants()
        assert constants["TOKEN_EXCHANGE_TARGET_CLIENT"] == self._EXPECTED_TARGET_CLIENT
        assert constants["TOKEN_EXCHANGE_SOURCE_CLIENT"] == self._EXPECTED_SOURCE_CLIENT
        for label, realm in self._both_realms():
            declared = (self._target_client(realm).get("attributes") or {}).get(
                self._ATTR_KEY
            )
            assert declared == constants["TOKEN_EXCHANGE_SOURCE_CLIENT"], (
                f"{label}: the realm's declared authorized source client "
                f"({declared!r}) does not match the in-cluster Job's "
                f"TOKEN_EXCHANGE_SOURCE_CLIENT ({constants['TOKEN_EXCHANGE_SOURCE_CLIENT']!r})."
            )

    def test_provisioner_script_mirrors_in_cluster_job(self) -> None:
        """``scripts/setup-memory-scopes.sh`` (the disaster-recovery
        backstop, ``feedback_use_bruno_collection_not_curl``-adjacent
        precedent: a manual re-run must authorize the SAME pair the Job
        would have, or a DR re-run silently diverges from what a healthy
        cluster already has."""
        script_text = (REPO_ROOT / "scripts" / "setup-memory-scopes.sh").read_text(
            encoding="utf-8"
        )
        cm_constants = self._configmap_constants()
        for name, expected in cm_constants.items():
            m = re.search(rf'{name}="([^"]+)"', script_text)
            assert m is not None, (
                f"scripts/setup-memory-scopes.sh is missing the {name} "
                "constant — it must mirror the in-cluster Job's Step 4."
            )
            assert m.group(1) == expected, (
                f"Drift: scripts/setup-memory-scopes.sh's {name}={m.group(1)!r} "
                f"does not match configmap-memory-scopes-script.yaml's "
                f"{name}={expected!r}."
            )

    _SUCCESS_MARKER = "authorized to exchange"

    def _step4_snippet(self) -> str:
        """The FULL Step 4 block: from the ``find_policy_id`` helper (the
        first thing Step 4 defines) through the final "authorized to
        exchange" echo — i.e. everything the happy path needs to reach a
        genuine, successful exit 0, not just the client-lookup guards. A
        narrower window that stopped before the permission-id
        grep/sed/policy-attach machinery let a KCADM stub crash for an
        UNRELATED reason (see ``test_step4_...`` docstrings below) look
        indistinguishable from a guard correctly firing — this is the
        2026-08-29 independent-review fix for exactly that vacuous-test
        defect."""
        text = self._configmap_script_text()
        start = text.index("find_policy_id() {")
        end = text.index('echo "✅ memory-scopes provisioning complete."')
        snippet = text[start:end]
        assert "find_client_id" in snippet, "Step 4 no longer calls find_client_id"
        assert self._SUCCESS_MARKER in snippet, (
            "Step 4's final success echo text drifted — update _SUCCESS_MARKER"
        )
        return snippet

    def _run_step4(self, *, missing: str | None) -> subprocess.CompletedProcess:
        """Run the ACTUAL Step 4 snippet (verbatim, from the rendered
        ConfigMap source) in a real bash subprocess against a REALISTIC
        ``kcadm`` stub — ``fake_kcadm`` below returns plausible JSON/CSV
        for every call Step 4's happy path actually makes (the
        ``management/permissions`` enable + its ``token-exchange`` id via
        the same JSON shape a live Keycloak 24 returns — see the Step 4
        comment block's live-verification note — the policy list/attach
        calls), so the baseline (``missing=None``) genuinely reaches exit
        0 by SUCCEEDING, not by the KCADM stub being too dumb to fail.

        ``find_client_id`` is stubbed separately (as before) to fail for
        exactly one client name (``missing``) — everything else, real
        Step 4 logic, unmodified."""
        snippet = self._step4_snippet()
        missing_line = f'MISSING="{missing}"\n' if missing else 'MISSING=""\n'
        harness = (
            "set -euo pipefail\n"
            'REALM="audittrace"\n' + missing_line + "find_client_id() {\n"
            '  if [[ -n "${MISSING}" && "$1" == "${MISSING}" ]]; then return 1; fi\n'
            '  echo "fake-uuid-$1"\n'
            "}\n"
            "fake_kcadm() {\n"
            '  local sub="$1" path="$2"\n'
            '  case "${sub} ${path}" in\n'
            '    "update "*"/management/permissions")\n'
            "      return 0 ;;\n"
            '    "get "*"/management/permissions")\n'
            "      cat <<'JSON'\n"
            "{\n"
            '  "enabled" : true,\n'
            '  "resource" : "fake-resource-id",\n'
            '  "scopePermissions" : {\n'
            '    "view" : "fake-view-id",\n'
            '    "token-exchange" : "fake-permission-id"\n'
            "  }\n"
            "}\n"
            "JSON\n"
            "      ;;\n"
            '    "get "*"/authz/resource-server/policy")\n'
            '      echo "fake-policy-id,${TOKEN_EXCHANGE_POLICY_NAME}" ;;\n'
            '    "create "*"/authz/resource-server/policy/client")\n'
            "      return 0 ;;\n"
            '    "update "*"/authz/resource-server/permission/scope/"*)\n'
            "      return 0 ;;\n"
            "    *)\n"
            '      echo "fake_kcadm: unhandled call: $*" >&2\n'
            "      return 1 ;;\n"
            "  esac\n"
            "}\n"
            "KCADM=fake_kcadm\n"
            'TOKEN_EXCHANGE_TARGET_CLIENT="audittrace-librechat"\n'
            'TOKEN_EXCHANGE_SOURCE_CLIENT="audittrace-librechat-bff"\n'
            'TOKEN_EXCHANGE_POLICY_NAME="allow-${TOKEN_EXCHANGE_SOURCE_CLIENT}-token-exchange"\n'
            + snippet
        )
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)

    def test_step4_succeeds_when_all_clients_present(self) -> None:
        """The genuinely-passing BASELINE the fail-closed parametrizations
        below are measured against: with every client resolvable and a
        REALISTIC kcadm stub, Step 4 must reach exit 0 and print its final
        success line. If this test itself is not GREEN, the parametrized
        fail-closed tests below are meaningless (a script that never
        succeeds "fails closed" for every input vacuously)."""
        result = self._run_step4(missing=None)
        assert result.returncode == 0, (
            "Step 4's happy path (every client found, realistic kcadm "
            f"stub) must exit 0. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert self._SUCCESS_MARKER in result.stdout, (
            "Step 4's happy path did not reach its final success echo. "
            f"stdout={result.stdout!r}"
        )

    @pytest.mark.parametrize(
        "missing",
        ["audittrace-librechat", "audittrace-librechat-bff", "realm-management"],
    )
    def test_step4_fails_closed_when_one_client_is_missing(self, missing: str) -> None:
        """Falsifiable per-guard, against the genuinely-passing baseline
        proven above: neuter ONLY the ``if ! ...; then exit 1; fi`` block
        for ``missing`` (e.g. swallow it into a ``|| true``) and this
        specific parametrization goes RED — the script now reaches exit 0
        and prints the success line even though the client Step 4 relies
        on was never found — proving each of the three guards is
        independently load-bearing, not redundant coverage from a
        neighbour, and not a script that merely crashes for an unrelated
        reason regardless of which guard is intact."""
        result = self._run_step4(missing=missing)
        assert result.returncode != 0, (
            f"Step 4 must fail closed when {missing!r} is not found. It "
            f"exited 0. stdout={result.stdout!r}"
        )
        assert "not found" in (result.stdout + result.stderr), (
            f"Step 4's fail-closed path for {missing!r} should explain "
            f"which client was missing. Got stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert self._SUCCESS_MARKER not in result.stdout, (
            f"Step 4 printed its SUCCESS line even though {missing!r} was "
            "never found — the guard did not actually stop the script "
            f"(vacuous fail-closed check). stdout={result.stdout!r}"
        )
