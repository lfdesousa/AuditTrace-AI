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
