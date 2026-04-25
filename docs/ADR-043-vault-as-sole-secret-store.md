# ADR-043: HashiCorp Vault as the sole production secret store

**Status:** Proposed
**Date:** 2026-04-25
**Deciders:** Luis Filipe de Sousa
**Related:** ADR-041 (product boundary — Secret Manager is one of the eight
dependencies), ADR-045 (laptop-first deployment posture), ADR-022
(Keycloak realm), ADR-027 (stateless memory-server), ADR-042 (BFF-first
OIDC for UIs — depends on this ADR for client-secret custody)

## Context

AuditTrace-AI today reads secrets from Kubernetes `Secret` resources
rendered by Helm at install time. The chart's `values.yaml` carries
the secret values directly as plaintext keys under `secrets.*`:

- `secrets.postgres.password`, `secrets.postgres.appPassword`
- `secrets.summariser.password`
- `secrets.chromadb.token`
- `secrets.redis.password`
- `secrets.minio.secretKey`, `secrets.minio.kmsKey`
- `secrets.keycloak.adminPassword`

Workloads consume these via `valueFrom.secretKeyRef`. The only
operator-visible safety net is the ADR-045 PM-1 `required` guard on
MinIO + summariser secrets. The Keycloak admin password has **no**
`required` guard today; it defaults to `admin` in dev and is silently
rendered into a `Secret` object that any pod with namespace read access
can decode.

Three forces converge to make this insufficient for a regulated-customer
deployment:

1. **ADR-041's product-boundary commitment.** The chart names "Secret
   Manager (Vault / SOPS / ESO)" as one of the eight dependencies the
   product expects to integrate against, not bundle. Today's plaintext
   `Secret` resources are the dev fallback, explicitly. The production
   integration has never shipped.
2. **`feedback_keycloak_three_witnesses`** (2026-04-24). Rotating the
   Keycloak admin password requires touching three places (k8s Secret,
   `.env`, Keycloak's internal DB). Each is a separate rotation step
   today, each is fragile, and the env-and-Secret leg can silently
   diverge from the in-DB password without anyone noticing. A central
   secret store with an audited rotation API closes this.
3. **The POC milestone schedule.** M1 (this ADR) is the
   prerequisite for M2 (external IdP federation) and M3 (LibreChat
   confidential client + secret-in-Vault per ADR-042 §2). Ships before
   2026-05-09 or M3 cannot ship.

The decision below picks one production secret store and commits to it.

## Decision

### §1. HashiCorp Vault Community deployed in-cluster as a Helm sub-chart

Vault is bundled into `charts/audittrace/charts/vault/` as a Helm
dependency. The sub-chart is `hashicorp/vault` (the official chart), or
a thin wrapper around it, conditional on `vault.enabled`.

This **violates ADR-045 §Rule 1** ("sibling stacks remain independent
docker-compose; do not bundle product-shape dependencies"). The
violation is deliberate and recorded here as an intentional
per-deployment exception:

- Vault is product-shape enough to bundle for a single-operator POC
  deployment. Pulling Vault out into a sibling stack later is cheap
  (re-target the auth endpoint).
- regulated-tier production targets in-cluster Vault anyway (own
  cluster, own Vault, our chart consumes it).
- The laptop-first dev profile keeps `vault.enabled=false` and the
  legacy plain-`Secret` path; no laptop-first regression.

Operationally Vault is **single-node**, file backend on a local-path
PVC. This is acceptable for the POC tier. HA-Vault, integrated storage
(Raft), and auto-unseal are deferred to a follow-up.

### §2. Kubernetes auth method, not AppRole

Workloads authenticate to Vault via the Kubernetes auth method:
ServiceAccount token → Vault role → policy → secret read. This:

- Matches the k3s + Istio + SPIFFE ZTA trajectory
  (`project_k8s_zta_trajectory`). When SPIFFE lands later, the SVID
  becomes the auth token; the policy model does not change.
- Removes the need to bootstrap and rotate AppRole IDs/secrets — a
  recursive secret-management problem that AppRole has but K8s-auth
  does not.
- Is the path the upstream Vault chart and the Vault Agent Injector
  document as the default for in-cluster workloads.

AppRole is **not** used by AuditTrace-AI. CI/release tooling that
needs to write secrets uses a separate operator-token issuance flow
(out of scope for this ADR; admin-side concern, not workload-side).

### §3. KV v2 for application secrets; Transit reserved for envelope encryption

Two engines are enabled:

- **KV v2** mounted at `kv/`. All application secrets live under
  `kv/audittrace/<service>/<key>` paths (see §5).
- **Transit** mounted at `transit/`. Reserved for envelope encryption
  of at-rest data (Postgres column encryption, MinIO master key
  derivation). **No Transit-encrypted data ships in M1.** This is a
  follow-up; the engine is enabled now so the path is reserved.

Other engines (PKI, database dynamic creds, AWS dynamic creds) are
**not** enabled. Each is a follow-up considered on its own merits.
PKI for internal mTLS is the most likely first-add when SPIFFE lands.

### §4. Vault Agent Injector — file-based mount, never env vars

Workloads consume secrets through the Vault Agent Injector sidecar
pattern:

- Annotations on the workload spec (`vault.hashicorp.com/agent-inject*`)
  declare which secrets to fetch.
- The Vault Agent sidecar fetches them on pod start and renders them
  to **files** under `/vault/secrets/<name>` inside the workload's
  filesystem.
- The application reads the file. **Secrets never enter environment
  variables**, never appear in `kubectl describe pod` output, never
  end up in container manifests.

This replaces every current `valueFrom.secretKeyRef` env-var binding.
Where the application code expects an env var (e.g. PostgreSQL
connection string assembly), the Vault Agent template renders a small
shim file that is sourced at process start, OR the application is
updated to read the file path directly.

Per `feedback_openai_schema_inviolate`: this is purely a deployment
concern; no public API changes.

### §5. KV path conventions

`kv/audittrace/<service>/<key>` where:

- `<service>` ∈ `{ postgres, summariser, redis, chromadb, minio,
  keycloak, langfuse, librechat-webui, ... }` — one path prefix
  per dependency from ADR-041's eight.
- `<key>` is `password`, `token`, `secret_key`, `kms_master_key`, etc.
  — the application's term, not the dependency's. Keep stable across
  Vault and the application config.

Each ServiceAccount's role grants read-only access to the prefixes
its workload owns. Memory-server SA reads
`kv/audittrace/postgres/*`, `kv/audittrace/redis/*`,
`kv/audittrace/chromadb/*`, `kv/audittrace/minio/*`. Keycloak SA
reads `kv/audittrace/keycloak/*` (and the postgres app-user creds
for its own DB). MinIO SA reads its own key and KMS master key.

Wildcards in policies are exact-prefix only; no cross-service reads.

### §6. Dev-vs-prod gating: `vault.enabled` toggle preserves laptop-first

The chart's `values.yaml` gains a top-level `vault:` block:

```yaml
vault:
  enabled: false        # default off; laptop-first preserved
  injector:
    enabled: true       # vault.enabled is the master switch
  ...
```

When `vault.enabled=true`:

- Existing `secrets.*` plaintext keys are **ignored** (the chart
  raises a `required`-style error if both vault.enabled=true AND a
  legacy plaintext is set, to prevent silent override confusion).
- Workload manifests render Vault Agent annotations in place of
  `valueFrom.secretKeyRef`.
- Bitnami subcharts (postgres, redis) use `auth.existingSecret`
  pointing at a Secret that the Vault Agent renders at boot (see §9).

When `vault.enabled=false`:

- The chart renders identically to today. Laptop-first dev profile
  is not regressed.
- ADR-045 §Rule 1's spirit is preserved on the dev profile.

The `global.productionMode=true` guard from `_helpers.tpl:45–82` is
extended: if production mode is set AND vault.enabled is false, the
chart fails to render. Production mode requires Vault.

### §7. Manual unseal posture (POC-acceptable)

Vault auto-unseal via Transit-against-second-Vault is **out of scope**
for M1. The single-Vault POC accepts manual unseal at boot:

- Initial unseal: documented in
  `~/work/audittrace-private/runbooks/02-vault-unseal.md` (M1
  deliverable).
- Restart unseal: same runbook, ~2 min wall time.
- Break-glass: root-token recovery via Shamir-share reconstitution.
  Three of five shares held by the operator; for the POC the operator
  is Luis. Production deployments shamir-distribute to the customer's
  ops team.

**Auto-unseal is a follow-up** when the second Vault exists (could be
a sibling-cluster deployment, AWS KMS, or a customer's existing Vault
HA cluster).

### §8. Provisioning

A Helm post-install hook job seeds initial secrets into Vault:

- `secrets-bootstrap-job.yaml` — runs once after Vault is unsealed,
  creates the KV v2 mount, writes per-service initial values from
  the operator-supplied `vault.bootstrap.*` values (consumed and
  immediately discarded).
- The bootstrap values are required at first install; subsequent
  installs MUST NOT carry them (the chart errors out if they
  reappear after Vault is initialised).

**Operator workflow:**

1. `helm install ... --set vault.enabled=true` — chart deploys Vault
   in uninitialised state.
2. Operator runs `vault operator init` (in-pod or via local
   `vault` CLI), distributes Shamir shares.
3. Operator runs `vault operator unseal` with three shares.
4. Operator runs `helm upgrade ... -f bootstrap-secrets.yaml` once;
   the bootstrap job populates KV v2.
5. Subsequent operations use Vault's API for rotation; Helm never
   sees secret values again.

### §9. Bitnami subchart auth — deferred to M1+ (External Secrets Operator)

Postgres and Redis Bitnami subcharts continue to source their root
passwords from `values.yaml` (`postgresql.auth.password`,
`redis.auth.password`) in M1. The Bitnami StatefulSet renders its
own K8s `Secret` from those values at install time, and the
subchart's pod consumes it via the upstream chart's
`valueFrom.secretKeyRef` wiring.

This is a deliberate M1 scope reduction. Cleanly migrating Bitnami
subcharts to Vault requires either:

- **External Secrets Operator (ESO)** — a separate controller that
  syncs Vault → K8s Secret. The Bitnami subchart points at the
  synced Secret via `auth.existingSecret`. Robust, but adds another
  cluster-level dependency.
- **Pre-install Helm hook Job that fetches from Vault** — the Job
  runs as a Vault-Agent-annotated pod, reads the secret, creates the
  K8s Secret, completes. The Bitnami subchart points at it via
  `auth.existingSecret`. Brittle around upgrade ordering and
  Vault-availability windows.

Both alternatives are larger than M1 should bear. **Trade-off
accepted:** postgres + redis passwords remain in `values.yaml` /
K8s Secrets in M1; the AuditTrace-owned workloads (memory-server,
Keycloak, MinIO, summariser-job) move fully to Vault. The M1+
follow-up — likely ESO — closes the postgres + redis gap before
production deploy.

The KV path `kv/audittrace/postgres/superuser` (used by the
summariser-role Job's `PGPASSWORD`) and `kv/audittrace/postgres/app`
(used by memory-server's URL assembly) **are** managed by Vault in
M1; the Bitnami subchart's own perception of those passwords still
reads from `values.yaml`, so during M1 the operator must keep both
paths in sync (Vault and `values.yaml.postgresql.auth.password`).
Documented as an operational requirement in
`~/work/audittrace-private/runbooks/02-vault-unseal.md`.

## Consequences

### Positive

- **Secrets never enter env vars or container manifests.**
  `kubectl describe pod` output is sterile. `kubectl get secret` shows
  only short-lived rendered Secrets owned by Vault Agent.
- **Single source of truth for rotation.** Rotating the Keycloak admin
  password becomes a Vault API call + a workload-pod restart. Closes
  the three-witness fragility from `feedback_keycloak_three_witnesses`
  for the production path (the dev path keeps the three-witness
  procedure, documented in the runbook).
- **Audit trail on secret reads.** Vault's audit device logs every
  read; the operator now has a "who-fetched-what-when" log for
  privileged secret access. EU AI Act Art 12 alignment continues.
- **ADR-041 product-boundary commitment honoured.** The "Secret
  Manager" dependency in the eight-dependency framing now has a
  concrete production integration, not just a fallback.
- **Sequenced correctly for ADR-042 (M3).** LibreChat's
  `audittrace-webui` client secret lives in Vault from day one. No
  client-secret-in-values.yaml era to undo.
- **Trust-boundary diagram becomes truthful.** The "secrets" arrow
  no longer goes from operator → Helm values → k8s Secret → pod.
  It goes from operator → Vault → pod (file). Cleaner story for
  CISOs.

### Negative / caveats

- **Operational complexity.** Manual unseal at boot, Shamir-share
  custody, Vault upgrade procedures. POC-acceptable; a full HA Vault
  deployment is a follow-up.
- **ADR-045 §Rule 1 exception.** Vault runs in-cluster, not as a
  sibling stack. The exception is deliberate and bounded; future
  product-shape dependencies do not get the same exception by
  default.
- **Bootstrap dependency cycle.** First install requires operator
  to type unseal shares. Not automatable for the POC. Production
  HA + auto-unseal removes this.
- **Single-node Vault is a SPOF.** A Vault outage stops new pods
  from starting. Existing pods retain cached secrets via Vault Agent
  templates; some grace period exists. POC-acceptable, not
  production-acceptable.

### Architecture documentation impact

Per the private POC roadmap §"architecture documentation in lock-step"
rule, this ADR's PR includes:

- **`docs/architecture/workspace.dsl`** — new container `vault` in the
  audittrace namespace; new arrow from every dependency-consuming
  workload to Vault (Agent Injector path). Update the chat-server
  string from `Qwen3.5-35B-A3B` to `Qwen 3.6-27B-Q4_K_M` (drift item
  D1 from `DRIFT-20260425.md`).
- **`docs/architecture/product-and-dependencies.md`** — the
  eight-dependency table's "Secret Manager" row updates from
  "No Vault integration yet. *Addressed by roadmap Phase 1.1
  (ADR-040), target 2026-05-16.*" to a concrete integration
  description with this ADR's number. Drift items D2 (ADR-039/040
  → ADR-043/044) and D3 (Keycloak misclassified as Bitnami subchart)
  fixed in the same edit pass.
- **`docs/architecture/sequence-vault-injection.md`** (new) —
  ServiceAccount → Vault token request → KV read → Agent template
  render → file mount → application read. The sequence diagram
  Trust-boundary view will reference.
- **Trust-boundary overlay (in `workspace.dsl` views)** — the
  "secrets-never-in-env" boundary line.

## Follow-ups

- **Transit envelope encryption** for Postgres column encryption
  and MinIO KMS master-key derivation. Deferred from M1 to keep
  the milestone bounded; the engine ships enabled, the data path
  ships next.
- **Auto-unseal** via Transit-against-second-Vault, AWS KMS, or
  customer's existing Vault. Sequenced behind a second Vault
  existing.
- **Dynamic Postgres credentials.** Vault's database secret engine
  can mint short-lived Postgres credentials per workload connection.
  Strong story for regulated-tier ops; defer to a separate ADR.
- **Vault Agent template caching** of memory-server-relevant secrets,
  so a Vault outage does not immediately stop new pods. Low-effort
  hardening worth doing in the next milestone after M1.
- **Secret rotation cadence + audit cadence** documented in the
  runbook. Operational discipline, not architectural.
- **Vault HA + Raft integrated storage** when the deployment graduates
  past POC scale. Separate ADR.
- **ADR-045 §Rule 1 amendment** — note the Vault exception in
  ADR-045 itself so the rule remains internally consistent. Follow-up
  edit on ADR-045's PR or a small standalone PR.

## Postmortem — first live install (2026-04-25)

Seven distinct quiet bugs surfaced during the first end-to-end
`helm upgrade --set vault.enabled=true` on the laptop k3s cluster.
Each is recorded as a durable rule so the next operator install (and
any future `audittrace.com`-scale hardening) does not re-walk the
same minefield. Pattern matches ADR-045's PM-1..PM-4 — discoveries
that only happen under live conditions become rules in the chart.

### PM-1 — `kubectl exec` has no `--env` flag

`scripts/setup-vault.sh` v1 used `kubectl exec --env=VAULT_TOKEN=…
POD -- vault …`. That flag does not exist. The kubectl invocation
failed silently (the script's `vault status` check redirected stderr
to /dev/null and returned an empty status; the script then bailed
with a misleading "Vault not reachable" message).

**Rule:** to pass an env var into a `kubectl exec` invocation, use
the busybox `env` builtin inside the pod:
```
kubectl exec POD -- env VAR=value command …
```
The token appears briefly in the pod's process table; acceptable for
vault-0 (no other tenants).

### PM-2 — overriding `vault.server.serviceAccount.name` breaks auth-delegator

The upstream `hashicorp/vault` chart creates the Vault server's
ClusterRoleBinding `<release>-vault-server-binding → system:auth-
delegator` with the binding subject hardcoded to a release-prefixed
SA name. Setting `vault.server.serviceAccount.name: vault` (an
override I introduced for "tidiness") created the SA as `vault`, but
the binding still targeted `audittrace-vault`. The mismatch silently
disabled TokenReview, and every kubernetes-auth login 403'd.

**Rule:** never override `vault.server.serviceAccount.name`. Use the
upstream-default release-prefixed name. Same trap likely lurks for
other chart values whose names are referenced indirectly by sibling
templates — when in doubt, keep the upstream defaults.

### PM-3 — Vault Agent template trim markers eat newlines

The Helm helper for Vault Agent annotations rendered Vault template
syntax via `{{ "{{- with secret \"…\" -}}" }} … {{ "{{- end -}}" }}`.
The `{{-` and `-}}` trim markers strip leading and trailing whitespace
respectively. The result: chained `{{- end -}}{{- with -}}` blocks
produced a single line of output:
```
export A='…'export B='…'export C='…'
```
The shell parsed `export A='…'export …` as a single variable
assignment with a malformed value. First visible symptom: memory-
server tried to connect to a database called `audittraceexport`.

**Rule:** in Vault Agent templates rendered through Helm, do not
use trim markers on the `with`/`end` pairs. Plain `{{ with secret
"…" }} … {{ end }}` preserves the surrounding newlines so each
`export` lands on its own line. The shell-source contract of the
agent's env file requires newlines.

### PM-4 — Vault Agent does not bake operator-supplied prefixes

The pre-Vault `templates/secrets/secret-minio.yaml` baked the
`audittrace-key:` prefix into the rendered Secret value via
`printf "audittrace-key:%s"`. When MinIO moved to Vault Agent
file-mount sourcing, the agent template emitted `{{ .Data.data.
kms_master_key }}` raw. MinIO got the key without prefix and
fatal-ed with `kms: invalid secret key format`.

**Rule:** when migrating a value from a `printf`-decorated K8s
Secret to a Vault Agent template, re-apply the same string
decoration in the template. Test the rendered output against the
consumer's expected format before a workload sees it. The Vault
KV path stores the raw value; the call-site formatting belongs in
the template.

### PM-5 — workload-on-Vault triggers AuthorizationPolicy gaps

Vault-enabled deploys give Keycloak (and MinIO) their own
ServiceAccounts so the Vault Agent can fetch their KV paths. Those
new SAs are not in pre-existing AuthorizationPolicies. In the
audittrace namespace's STRICT mTLS posture, Postgres rejected
Keycloak's JDBC connection because `audittrace-keycloak` was not
in the postgres AP allow-list.

**Rule:** when introducing a new ServiceAccount for a workload that
talks to in-mesh dependencies, audit every AuthorizationPolicy that
selects on the dependencies and add the new SA principal under the
appropriate `{{- if .Values.vault.enabled }}` gate. Workload SAs
gained by enabling a feature flag must be reflected in mesh policy
in the same flag.

### PM-6 — never disable a security control to fix a connectivity error

The Vault Agent Injector's mutating webhook on :8080 is invoked by
the K8s API server, which is not in the mesh and does not speak
Istio mTLS. With STRICT mTLS, the inbound TLS handshake to the
injector pod failed (`127.0.0.6 EOF` in the injector logs), and
the upstream `MutatingWebhookConfiguration`'s `failurePolicy: Ignore`
caused the admission to silently no-op — workloads came up with
no Vault Agent sidecar.

The wrong fix: I disabled Istio sidecar injection wholesale on both
Vault server AND injector via `sidecar.istio.io/inject: "false"`.
That made the symptom go away. It also dropped Vault server out of
the mesh entirely, which silently downgraded every workload→Vault
secret read to plaintext (Istio's no-DestinationRule outbound
default to a non-mesh target).

The right fix: keep both Vault pods in the mesh; add
`traffic.sidecar.istio.io/excludeInboundPorts: "8080"` ONLY on the
injector pod. The webhook port skips Istio interception; everything
else (Vault server :8200, workload→Vault, Vault→K8s API) keeps full
mTLS. Plus a new `AuthorizationPolicy` on Vault server :8200 limits
mesh peers that can reach it to the four workload SAs.

**Rule:** never disable mTLS / AuthorizationPolicy / NetworkPolicy /
RBAC to fix a connectivity error. Find the minimum-scope bypass.
Captured durably as `feedback_no_security_control_shortcuts`.

### PM-7 — `token_reviewer_jwt` goes stale across vault-0 restarts

`scripts/setup-vault.sh` v1 wrote
`token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token`
to `auth/kubernetes/config`. That captures a snapshot of the SA
token at script-execution time. Vault stores it and uses it for
every TokenReview call. When `audittrace-vault-0` is recreated
(any helm upgrade that touches the StatefulSet, any node failure,
any rollout restart), the stored JWT belongs to a previous pod
identity. K8s rejects it on the next TokenReview. Every fresh
kubernetes-auth login then 403s with "permission denied" — even
though cluster RBAC, PeerAuthentication, and Vault role bindings
are all correct.

The failure is invisible until a new workload pod tries to log in:
existing workload Vault tokens keep working (renewal goes through
`auth/token/renew-self`, no TokenReview needed).

**Rule:** omit `token_reviewer_jwt` from
`vault write auth/kubernetes/config`. Vault's kubernetes auth
backend then reads `/var/run/secrets/kubernetes.io/serviceaccount/
token` from the Vault pod on every TokenReview call. K8s auto-
rotates that file. setup-vault.sh becomes a true one-time install
step — no re-run needed when vault-0 rolls.

### Architecture documentation impact (postmortem update)

This postmortem section's PR also updates
`docs/architecture/sequence-vault-injection.md` with a new
"In-mesh discipline" subsection capturing the PM-6 + PM-7 rules
visually: Vault stays in the mesh; only the injector's :8080 port
bypasses interception; an AuthorizationPolicy enforces the four-SA
allow-list on :8200; the kubernetes auth backend reads its
TokenReviewer JWT live, never from cache.
